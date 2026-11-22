"""DuckDB persistence layer, entity resolution helpers, BFS pathfinding, and harvest ingestion."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import duckdb

from mcp_brasil.data.transparencia_municipios.homonimos_mvp import (
    COMMON_TOKENS,
    compute_name_rarity,
    detect_profession_family,
    normalize_name,
)

from .constants import DEFAULT_CACHE_TTL_SECONDS, resolve_db_path
from .schemas import (
    GraphEdge,
    GraphNode,
    GraphPath,
    GraphPathStep,
    HarvestImportSummary,
    RiskAlert,
)

_DIGITS_RE = re.compile(r"\D+")


def clean_digits(raw: str | None) -> str:
    """Retorna apenas os dígitos de um documento."""
    return _DIGITS_RE.sub("", raw or "")


def normalize_cnpj(raw: str | None) -> str:
    """Normaliza um CNPJ para 14 dígitos ou retorna vazio se inválido."""
    d = clean_digits(raw)
    if len(d) == 14:
        return d
    return ""


def cnpj_root(cnpj: str) -> str:
    """Retorna a raiz de 8 dígitos de um CNPJ (agrupa matriz e filiais)."""
    norm = normalize_cnpj(cnpj)
    return norm[:8] if norm else ""


def mask_cpf(raw: str | None) -> str:
    """Aplica mascaramento LGPD de CPF (`***.456.789-**`), preservando CNPJs intactos."""
    text = (raw or "").strip()
    if not text:
        return ""
    if "***" in text:
        return text
    d = clean_digits(text)
    if len(d) == 11:
        return f"***.{d[3:6]}.{d[6:9]}-**"
    if len(d) == 14:
        return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}"
    return text


def mask_document_if_cpf(raw: str | None) -> str:
    """Mascara apenas quando o documento é CPF (11 dígitos), mantendo CNPJ formatado."""
    return mask_cpf(raw)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância geodésica em quilômetros entre duas coordenadas WGS84."""
    if (lat1 == 0.0 and lon1 == 0.0) or (lat2 == 0.0 and lon2 == 0.0):
        return 0.0
    r = 6371.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2.0) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2.0) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def make_municipio_id(codigo_ibge: str) -> str:
    """Chave canônica de nó de município."""
    return f"municipio:{clean_digits(codigo_ibge)}"


def make_empresa_id(cnpj: str) -> str:
    """Chave canônica de nó de empresa (CNPJ 14 dígitos)."""
    return f"empresa:{normalize_cnpj(cnpj)}"


def make_pessoa_id(nome: str) -> str:
    """Chave canônica LGPD-safe de nó de pessoa (baseada em nome normalizado)."""
    norm = normalize_name(nome)
    return f"pessoa:{norm}" if norm else ""


def make_politico_id(nome: str, uf: str = "") -> str:
    """Chave canônica de nó de agente político / parlamentar / candidato."""
    norm = normalize_name(nome)
    uf_norm = (uf or "").strip().upper()
    if not norm:
        return ""
    return f"politico:{norm}:{uf_norm}" if uf_norm else f"politico:{norm}"


def make_stable_id(prefix: str, *parts: str) -> str:
    """Gera identificador determinístico curto para arestas, contratos, emendas e alertas."""
    raw = "|".join((p or "").strip().upper() for p in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{prefix}:{digest}"


def compute_person_match_score(
    nome: str,
    cpf_a: str,
    cpf_b: str,
    cargo_a: str = "",
    cargo_b: str = "",
    uf_a: str = "",
    uf_b: str = "",
    dist_km: float | None = None,
) -> tuple[float, list[str]]:
    """Calcula score explicável (0-100) de probabilidade de mesma pessoa entre dois registros.

    Evolui a heurística de `homonimos_mvp.py` incorporando distância geodésica e CPF parcial.
    """
    score = 35.0
    reasons: list[str] = []

    norm_nome = normalize_name(nome)
    norm_tokens = [t for t in norm_nome.split() if len(t) > 1]
    token_count = len(norm_tokens)
    rare_tokens = sum(1 for t in norm_tokens if t not in COMMON_TOKENS)
    rarity = compute_name_rarity(norm_nome, total_occurrences=2)

    if token_count >= 3:
        score += 10.0
        reasons.append(f"nome composto ({token_count} termos)")
    if rare_tokens >= 2:
        score += 15.0
        reasons.append(f"sobrenome raro ({rare_tokens} termos incomuns)")
    elif rarity >= 0.35:
        score += 8.0
        reasons.append("raridade moderada de sobrenome")

    m_a = mask_cpf(cpf_a)
    m_b = mask_cpf(cpf_b)
    if m_a and m_b and "***" in m_a and "***" in m_b:
        if m_a == m_b:
            score += 35.0
            reasons.append(f"CPF parcial coincidente ({m_a})")
        else:
            score -= 40.0
            reasons.append(f"CPF parcial divergente ({m_a} vs {m_b})")

    prof_a = detect_profession_family(cargo_a)
    prof_b = detect_profession_family(cargo_b)
    if prof_a and prof_b and prof_a == prof_b:
        score += 10.0
        reasons.append(f"mesma família profissional ({prof_a})")

    if uf_a and uf_b and uf_a.upper() == uf_b.upper():
        score += 5.0
        reasons.append(f"mesma UF ({uf_a.upper()})")

    if dist_km is not None and dist_km > 0:
        if dist_km <= 80.0:
            score += 5.0
            reasons.append(f"municípios próximos ({dist_km:.0f} km)")
        else:
            reasons.append(f"distância intermunicipal de {dist_km:.0f} km")

    if rare_tokens == 0:
        score -= 15.0
        reasons.append("nome composto apenas por termos muito comuns")

    return max(0.0, min(100.0, round(score, 1))), reasons


class GraphStore:
    """Camada relacional de grafo persistida em DuckDB."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._in_memory = str(db_path) == ":memory:"
        self.db_path = Path(":memory:") if self._in_memory else resolve_db_path(db_path)
        if not self._in_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.db_path))
        self._init_schema()

    def close(self) -> None:
        """Fecha a conexão DuckDB."""
        self.conn.close()

    def __enter__(self) -> GraphStore:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _init_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nodes (
                node_id VARCHAR PRIMARY KEY,
                tipo VARCHAR NOT NULL,
                chave_canonica VARCHAR NOT NULL,
                nome VARCHAR NOT NULL,
                uf VARCHAR DEFAULT '',
                codigo_ibge VARCHAR DEFAULT '',
                metadata_json VARCHAR DEFAULT '{}',
                updated_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS edges (
                edge_id VARCHAR PRIMARY KEY,
                src_id VARCHAR NOT NULL,
                dst_id VARCHAR NOT NULL,
                tipo_relacao VARCHAR NOT NULL,
                valor DOUBLE DEFAULT 0.0,
                data_ref VARCHAR DEFAULT '',
                confianca DOUBLE DEFAULT 1.0,
                fonte VARCHAR DEFAULT '',
                url_evidencia VARCHAR DEFAULT '',
                metadata_json VARCHAR DEFAULT '{}',
                updated_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS alerts (
                alert_id VARCHAR PRIMARY KEY,
                regra VARCHAR NOT NULL,
                severidade VARCHAR NOT NULL,
                score DOUBLE NOT NULL,
                entidade_id VARCHAR NOT NULL,
                entidade_nome VARCHAR DEFAULT '',
                titulo VARCHAR NOT NULL,
                explicacao VARCHAR NOT NULL,
                evidencias_json VARCHAR DEFAULT '[]',
                created_at TIMESTAMP NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cache_fetch (
                cache_key VARCHAR PRIMARY KEY,
                fonte VARCHAR NOT NULL,
                payload_json VARCHAR NOT NULL,
                fetched_at TIMESTAMP NOT NULL,
                ttl_seconds INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_nodes_tipo ON nodes(tipo);
            CREATE INDEX IF NOT EXISTS idx_nodes_chave ON nodes(chave_canonica);
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src_id);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst_id);
            CREATE INDEX IF NOT EXISTS idx_edges_tipo ON edges(tipo_relacao);
            CREATE INDEX IF NOT EXISTS idx_alerts_entidade ON alerts(entidade_id);
            """
        )

    # ------------------------------------------------------------------
    # Cache com TTL
    # ------------------------------------------------------------------

    def get_cache(self, cache_key: str) -> Any | None:
        """Retorna payload JSON do cache se ainda dentro do TTL."""
        row = self.conn.execute(
            "SELECT payload_json, fetched_at, ttl_seconds FROM cache_fetch WHERE cache_key = ?",
            [cache_key],
        ).fetchone()
        if not row:
            return None
        payload_json, fetched_at, ttl_seconds = row
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if (
            isinstance(fetched_at, datetime)
            and fetched_at + timedelta(seconds=int(ttl_seconds)) < now
        ):
            return None
        try:
            return json.loads(payload_json)
        except json.JSONDecodeError:
            return None

    def set_cache(
        self,
        cache_key: str,
        fonte: str,
        payload: Any,
        ttl_seconds: int = DEFAULT_CACHE_TTL_SECONDS,
    ) -> None:
        """Persiste payload JSON no cache com TTL."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO cache_fetch
                (cache_key, fonte, payload_json, fetched_at, ttl_seconds)
            VALUES (?, ?, ?, ?, ?)
            """,
            [cache_key, fonte, serialized, now, int(ttl_seconds)],
        )

    # ------------------------------------------------------------------
    # Upserts de Nós, Arestas e Alertas
    # ------------------------------------------------------------------

    def upsert_node(self, node: GraphNode) -> GraphNode:
        """Insere ou atualiza um nó, mesclando metadados existentes."""
        existing = self.get_node(node.node_id)
        merged_meta: dict[str, Any] = {}
        if existing is not None:
            merged_meta.update(existing.metadata)
        merged_meta.update({k: v for k, v in node.metadata.items() if v not in (None, "", [])})

        nome = node.nome or (existing.nome if existing else node.chave_canonica)
        uf = node.uf or (existing.uf if existing else "")
        ibge = node.codigo_ibge or (existing.codigo_ibge if existing else "")
        now = datetime.now(timezone.utc).replace(tzinfo=None)

        self.conn.execute(
            """
            INSERT OR REPLACE INTO nodes
                (node_id, tipo, chave_canonica, nome, uf, codigo_ibge, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                node.node_id,
                node.tipo,
                node.chave_canonica,
                nome,
                uf,
                ibge,
                json.dumps(merged_meta, ensure_ascii=False, default=str),
                now,
            ],
        )
        return GraphNode(
            node_id=node.node_id,
            tipo=node.tipo,
            chave_canonica=node.chave_canonica,
            nome=nome,
            uf=uf,
            codigo_ibge=ibge,
            metadata=merged_meta,
        )

    def upsert_edge(self, edge: GraphEdge) -> GraphEdge:
        """Insere ou atualiza uma aresta no grafo."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO edges
                (edge_id, src_id, dst_id, tipo_relacao, valor, data_ref, confianca,
                 fonte, url_evidencia, metadata_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                edge.edge_id,
                edge.src_id,
                edge.dst_id,
                edge.tipo_relacao,
                float(edge.valor),
                edge.data_ref,
                float(edge.confianca),
                edge.fonte,
                edge.url_evidencia,
                json.dumps(edge.metadata, ensure_ascii=False, default=str),
                now,
            ],
        )
        return edge

    def upsert_alert(self, alert: RiskAlert) -> RiskAlert:
        """Insere ou atualiza um alerta explicável."""
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        self.conn.execute(
            """
            INSERT OR REPLACE INTO alerts
                (alert_id, regra, severidade, score, entidade_id, entidade_nome,
                 titulo, explicacao, evidencias_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                alert.alert_id,
                alert.regra,
                alert.severidade,
                float(alert.score),
                alert.entidade_id,
                alert.entidade_nome,
                alert.titulo,
                alert.explicacao,
                json.dumps(alert.evidencias, ensure_ascii=False, default=str),
                now,
            ],
        )
        return alert

    # ------------------------------------------------------------------
    # Consultas de Nós, Vizinhança e BFS
    # ------------------------------------------------------------------

    def get_node(self, node_id: str) -> GraphNode | None:
        """Recupera um nó por ID exato."""
        row = self.conn.execute(
            """
            SELECT node_id, tipo, chave_canonica, nome, uf, codigo_ibge, metadata_json
            FROM nodes WHERE node_id = ?
            """,
            [node_id],
        ).fetchone()
        if not row:
            return None
        return self._row_to_node(row)

    def find_nodes(self, query: str, tipo: str | None = None, limit: int = 20) -> list[GraphNode]:
        """Busca nós por ID exato, CNPJ/IBGE ou correspondência normalizada de nome."""
        q = (query or "").strip()
        if not q:
            return []

        exact = self.get_node(q)
        if exact is not None and (tipo is None or exact.tipo == tipo):
            return [exact]

        digits = clean_digits(q)
        if len(digits) == 14:
            emp = self.get_node(make_empresa_id(digits))
            if emp is not None:
                return [emp]
        if len(digits) == 7:
            mun = self.get_node(make_municipio_id(digits))
            if mun is not None:
                return [mun]

        norm_q = normalize_name(q)
        sql = """
            SELECT node_id, tipo, chave_canonica, nome, uf, codigo_ibge, metadata_json
            FROM nodes
            WHERE (UPPER(chave_canonica) LIKE ? OR UPPER(nome) LIKE ? OR node_id LIKE ?)
        """
        params: list[Any] = [f"%{norm_q}%", f"%{q.upper()}%", f"%{q}%"]
        if tipo:
            sql += " AND tipo = ?"
            params.append(tipo)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(int(limit))
        rows = self.conn.execute(sql, params).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_all_nodes(self, tipo: str | None = None) -> list[GraphNode]:
        """Lista todos os nós (opcionalmente filtrados por tipo)."""
        if tipo:
            rows = self.conn.execute(
                """
                SELECT node_id, tipo, chave_canonica, nome, uf, codigo_ibge, metadata_json
                FROM nodes WHERE tipo = ?
                """,
                [tipo],
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT node_id, tipo, chave_canonica, nome, uf, codigo_ibge, metadata_json
                FROM nodes
                """
            ).fetchall()
        return [self._row_to_node(r) for r in rows]

    def get_all_edges(self, tipo_relacao: str | None = None) -> list[GraphEdge]:
        """Lista todas as arestas (opcionalmente filtradas por tipo)."""
        if tipo_relacao:
            rows = self.conn.execute(
                """
                SELECT edge_id, src_id, dst_id, tipo_relacao, valor, data_ref, confianca,
                       fonte, url_evidencia, metadata_json
                FROM edges WHERE tipo_relacao = ?
                """,
                [tipo_relacao],
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT edge_id, src_id, dst_id, tipo_relacao, valor, data_ref, confianca,
                       fonte, url_evidencia, metadata_json
                FROM edges
                """
            ).fetchall()
        return [self._row_to_edge(r) for r in rows]

    def get_alerts(
        self,
        entidade_id: str | None = None,
        entidade_ids: set[str] | None = None,
        limit: int = 100,
    ) -> list[RiskAlert]:
        """Recupera alertas ordenados por score decrescente."""
        if entidade_id:
            rows = self.conn.execute(
                """
                SELECT alert_id, regra, severidade, score, entidade_id, entidade_nome,
                       titulo, explicacao, evidencias_json
                FROM alerts WHERE entidade_id = ?
                ORDER BY score DESC LIMIT ?
                """,
                [entidade_id, int(limit)],
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT alert_id, regra, severidade, score, entidade_id, entidade_nome,
                       titulo, explicacao, evidencias_json
                FROM alerts
                ORDER BY score DESC LIMIT ?
                """,
                [int(limit) * 5 if entidade_ids else int(limit)],
            ).fetchall()

        out = [self._row_to_alert(r) for r in rows]
        if entidade_ids is not None:
            out = [a for a in out if a.entidade_id in entidade_ids][:limit]
        return out

    def query_neighborhood(
        self,
        root_id: str,
        max_hops: int = 2,
        max_nodes: int = 150,
    ) -> tuple[list[GraphNode], list[GraphEdge]]:
        """Retorna subgrafo (nós + arestas) alcançável em até `max_hops` saltos."""
        root = self.get_node(root_id)
        if root is None:
            return [], []

        visited_nodes: dict[str, GraphNode] = {root.node_id: root}
        collected_edges: dict[str, GraphEdge] = {}
        frontier: set[str] = {root.node_id}

        for _ in range(max(1, max_hops)):
            if not frontier or len(visited_nodes) >= max_nodes:
                break
            placeholders = ",".join("?" for _ in frontier)
            f_list = list(frontier)
            rows = self.conn.execute(
                f"""
                SELECT edge_id, src_id, dst_id, tipo_relacao, valor, data_ref, confianca,
                       fonte, url_evidencia, metadata_json
                FROM edges
                WHERE src_id IN ({placeholders}) OR dst_id IN ({placeholders})
                """,
                f_list + f_list,
            ).fetchall()

            next_frontier: set[str] = set()
            for row in rows:
                edge = self._row_to_edge(row)
                collected_edges[edge.edge_id] = edge
                for cand_id in (edge.src_id, edge.dst_id):
                    if cand_id not in visited_nodes and len(visited_nodes) < max_nodes:
                        node = self.get_node(cand_id)
                        if node is not None:
                            visited_nodes[cand_id] = node
                            next_frontier.add(cand_id)
            frontier = next_frontier

        # Mantém apenas arestas cujas duas extremidades estão no subgrafo coletado
        final_edges = [
            e
            for e in collected_edges.values()
            if e.src_id in visited_nodes and e.dst_id in visited_nodes
        ]
        return list(visited_nodes.values()), final_edges

    def find_shortest_paths(
        self,
        src_id: str,
        dst_id: str,
        max_hops: int = 4,
        max_paths: int = 5,
    ) -> list[GraphPath]:
        """Busca em largura (BFS) pelos menores caminhos conectando `src_id` a `dst_id`."""
        if not src_id or not dst_id or src_id == dst_id:
            return []
        if self.get_node(src_id) is None or self.get_node(dst_id) is None:
            return []

        all_edges = self.get_all_edges()
        adj: dict[str, list[tuple[str, GraphEdge]]] = {}
        for e in all_edges:
            adj.setdefault(e.src_id, []).append((e.dst_id, e))
            adj.setdefault(e.dst_id, []).append((e.src_id, e))

        node_names: dict[str, str] = {n.node_id: n.nome for n in self.get_all_nodes()}
        found: list[GraphPath] = []
        best_len: int | None = None

        queue: deque[tuple[str, list[tuple[str, str, GraphEdge]]]] = deque([(src_id, [])])
        visited_depth: dict[str, int] = {src_id: 0}

        while queue:
            curr, trail = queue.popleft()
            depth = len(trail)
            if depth >= max_hops:
                continue
            if best_len is not None and depth >= best_len:
                continue

            for nxt, edge in adj.get(curr, []):
                if any(step_from == nxt or step_to == nxt for step_from, step_to, _ in trail):
                    continue
                new_trail = [*trail, (curr, nxt, edge)]
                if nxt == dst_id:
                    best_len = len(new_trail)
                    steps = [
                        GraphPathStep(
                            from_id=s_from,
                            from_nome=node_names.get(s_from, s_from),
                            to_id=s_to,
                            to_nome=node_names.get(s_to, s_to),
                            tipo_relacao=s_edge.tipo_relacao,
                            valor=s_edge.valor,
                            fonte=s_edge.fonte,
                            url_evidencia=s_edge.url_evidencia,
                        )
                        for s_from, s_to, s_edge in new_trail
                    ]
                    found.append(
                        GraphPath(
                            origem_id=src_id,
                            destino_id=dst_id,
                            saltos=len(steps),
                            passos=steps,
                        )
                    )
                    if len(found) >= max_paths:
                        return found
                    continue

                prev_d = visited_depth.get(nxt)
                if prev_d is None or depth + 1 <= prev_d:
                    visited_depth[nxt] = depth + 1
                    queue.append((nxt, new_trail))

        return found

    def stats(self) -> dict[str, Any]:
        """Retorna estatísticas globais do grafo persistido em DuckDB."""
        nodes_by_type = dict(
            self.conn.execute("SELECT tipo, COUNT(*) FROM nodes GROUP BY tipo").fetchall()
        )
        edges_by_type = dict(
            self.conn.execute(
                "SELECT tipo_relacao, COUNT(*) FROM edges GROUP BY tipo_relacao"
            ).fetchall()
        )
        alerts_by_sev = dict(
            self.conn.execute(
                "SELECT severidade, COUNT(*) FROM alerts GROUP BY severidade"
            ).fetchall()
        )
        return {
            "db_path": str(self.db_path),
            "total_nodes": sum(int(v) for v in nodes_by_type.values()),
            "total_edges": sum(int(v) for v in edges_by_type.values()),
            "total_alerts": sum(int(v) for v in alerts_by_sev.values()),
            "nodes_por_tipo": nodes_by_type,
            "edges_por_tipo": edges_by_type,
            "alerts_por_severidade": alerts_by_sev,
        }

    # ------------------------------------------------------------------
    # Ingestão de arquivos NDJSON do `transparencia-go harvest`
    # ------------------------------------------------------------------

    def import_harvest_ndjson(self, path: str | Path) -> HarvestImportSummary:
        """Ingere um arquivo NDJSON produzido por `transparencia-go harvest` no grafo DuckDB."""
        p = Path(path).expanduser()
        summary = HarvestImportSummary(arquivo=str(p))
        if not p.exists():
            raise FileNotFoundError(f"Arquivo NDJSON não encontrado: {p}")

        with p.open("r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    summary.erros_leitura += 1
                    continue
                if not isinstance(rec, dict) or not rec.get("codigo_ibge"):
                    summary.erros_leitura += 1
                    continue

                n_nodes, n_edges = self.ingest_harvest_record(rec)
                summary.municipios_lidos += 1
                summary.nodes_inseridos += n_nodes
                summary.edges_inseridas += n_edges

        return summary

    def ingest_harvest_record(self, rec: dict[str, Any]) -> tuple[int, int]:
        """Ingere um único registro municipal (formato HarvestRecord) em nós e arestas."""
        ibge = clean_digits(str(rec.get("codigo_ibge", "")))
        nome_mun = str(rec.get("nome", "")).strip()
        uf = str(rec.get("uf", "")).strip().upper()
        portal_url = str(rec.get("portal_url", "")).strip()
        if not ibge:
            return 0, 0

        nodes_added = 0
        edges_added = 0

        mun_id = make_municipio_id(ibge)
        self.upsert_node(
            GraphNode(
                node_id=mun_id,
                tipo="municipio",
                chave_canonica=ibge,
                nome=f"{nome_mun} ({uf})" if uf else nome_mun,
                uf=uf,
                codigo_ibge=ibge,
                metadata={
                    "cnpj": normalize_cnpj(rec.get("cnpj")),
                    "latitude": float(rec.get("latitude") or 0.0),
                    "longitude": float(rec.get("longitude") or 0.0),
                    "populacao": int(rec.get("populacao") or 0),
                    "plataforma": str(rec.get("plataforma", "")),
                    "portal_url": portal_url,
                },
            )
        )
        nodes_added += 1

        # 1. Servidores -> pessoa --[TRABALHA_EM]--> municipio
        for serv in rec.get("servidores") or []:
            s_nome = str(serv.get("nome", "")).strip()
            pid = make_pessoa_id(s_nome)
            if not pid:
                continue
            cpf_masked = mask_cpf(serv.get("cpf_parcial") or serv.get("cpf"))
            cargo = str(serv.get("cargo", "")).strip()
            salario = float(serv.get("salario_bruto") or serv.get("salario_liquido") or 0.0)
            comp = str(serv.get("competencia", "")).strip()
            self.upsert_node(
                GraphNode(
                    node_id=pid,
                    tipo="pessoa",
                    chave_canonica=normalize_name(s_nome),
                    nome=s_nome,
                    uf=uf,
                    codigo_ibge=ibge,
                    metadata={"cpf_parcial": cpf_masked, "cargo": cargo},
                )
            )
            nodes_added += 1
            self.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id("edge", pid, "TRABALHA_EM", mun_id, cargo, comp),
                    src_id=pid,
                    dst_id=mun_id,
                    tipo_relacao="TRABALHA_EM",
                    valor=salario,
                    data_ref=comp,
                    fonte=str(serv.get("fonte") or "transparencia_municipios"),
                    url_evidencia=str(serv.get("portal_url") or portal_url),
                    metadata={
                        "cargo": cargo,
                        "lotacao": str(serv.get("lotacao", "")),
                        "vinculo": str(serv.get("vinculo", "")),
                        "cpf_parcial": cpf_masked,
                    },
                )
            )
            edges_added += 1

        # 2. Contratos -> municipio --[CONTRATOU]--> empresa (ou pessoa se CPF)
        for ct in rec.get("contratos") or []:
            forn = str(ct.get("fornecedor", "")).strip()
            doc = str(ct.get("documento_fornecedor", "")).strip()
            valor = float(ct.get("valor") or 0.0)
            numero = str(ct.get("numero", "")).strip()
            data_ass = str(ct.get("data_assinatura", "")).strip()
            objeto = str(ct.get("objeto", "")).strip()
            target_id = self._upsert_supplier_node(forn, doc, uf)
            if not target_id:
                continue
            nodes_added += 1
            self.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id(
                        "edge", mun_id, "CONTRATOU", target_id, numero, data_ass
                    ),
                    src_id=mun_id,
                    dst_id=target_id,
                    tipo_relacao="CONTRATOU",
                    valor=valor,
                    data_ref=data_ass or str(ct.get("ano") or ""),
                    fonte=str(ct.get("fonte") or "transparencia_municipios"),
                    url_evidencia=portal_url,
                    metadata={"numero": numero, "objeto": objeto[:300]},
                )
            )
            edges_added += 1

        # 3. Despesas -> municipio --[PAGOU_DESPESA]--> empresa (ou pessoa)
        for desp in rec.get("despesas") or []:
            forn = str(desp.get("fornecedor", "")).strip()
            doc = str(desp.get("documento_fornecedor", "")).strip()
            valor = float(
                desp.get("valor_pago")
                or desp.get("valor_liquidado")
                or desp.get("valor_empenhado")
                or 0.0
            )
            num_emp = str(desp.get("numero_empenho", "")).strip()
            data_d = str(desp.get("data", "")).strip()
            hist = str(desp.get("historico", "")).strip()
            target_id = self._upsert_supplier_node(forn, doc, uf)
            if not target_id:
                continue
            nodes_added += 1
            self.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id(
                        "edge", mun_id, "PAGOU_DESPESA", target_id, num_emp, data_d
                    ),
                    src_id=mun_id,
                    dst_id=target_id,
                    tipo_relacao="PAGOU_DESPESA",
                    valor=valor,
                    data_ref=data_d or str(desp.get("ano") or ""),
                    fonte=str(desp.get("fonte") or "transparencia_municipios"),
                    url_evidencia=portal_url,
                    metadata={"numero_empenho": num_emp, "historico": hist[:300]},
                )
            )
            edges_added += 1

        return nodes_added, edges_added

    def _upsert_supplier_node(self, nome: str, doc: str, uf: str) -> str:
        cnpj = normalize_cnpj(doc)
        if cnpj:
            eid = make_empresa_id(cnpj)
            self.upsert_node(
                GraphNode(
                    node_id=eid,
                    tipo="empresa",
                    chave_canonica=cnpj,
                    nome=nome or cnpj,
                    uf=uf,
                    metadata={"cnpj": cnpj, "cnpj_raiz": cnpj_root(cnpj)},
                )
            )
            return eid
        if nome:
            pid = make_pessoa_id(nome)
            if pid:
                self.upsert_node(
                    GraphNode(
                        node_id=pid,
                        tipo="pessoa",
                        chave_canonica=normalize_name(nome),
                        nome=nome,
                        uf=uf,
                        metadata={"cpf_parcial": mask_document_if_cpf(doc)},
                    )
                )
                return pid
        return ""

    @staticmethod
    def _row_to_node(row: tuple[Any, ...]) -> GraphNode:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(row[6] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return GraphNode(
            node_id=str(row[0]),
            tipo=str(row[1]),
            chave_canonica=str(row[2]),
            nome=str(row[3]),
            uf=str(row[4] or ""),
            codigo_ibge=str(row[5] or ""),
            metadata=meta,
        )

    @staticmethod
    def _row_to_edge(row: tuple[Any, ...]) -> GraphEdge:
        meta: dict[str, Any] = {}
        try:
            meta = json.loads(row[9] or "{}")
        except json.JSONDecodeError:
            meta = {}
        return GraphEdge(
            edge_id=str(row[0]),
            src_id=str(row[1]),
            dst_id=str(row[2]),
            tipo_relacao=str(row[3]),
            valor=float(row[4] or 0.0),
            data_ref=str(row[5] or ""),
            confianca=float(row[6] or 1.0),
            fonte=str(row[7] or ""),
            url_evidencia=str(row[8] or ""),
            metadata=meta,
        )

    @staticmethod
    def _row_to_alert(row: tuple[Any, ...]) -> RiskAlert:
        evids: list[dict[str, Any]] = []
        try:
            evids = json.loads(row[8] or "[]")
        except json.JSONDecodeError:
            evids = []
        sev = str(row[2])
        if sev not in ("ALTA", "MEDIA", "BAIXA", "INFO"):
            sev = "INFO"
        return RiskAlert(
            alert_id=str(row[0]),
            regra=str(row[1]),
            severidade=sev,  # type: ignore[arg-type]
            score=float(row[3] or 0.0),
            entidade_id=str(row[4]),
            entidade_nome=str(row[5] or ""),
            titulo=str(row[6]),
            explicacao=str(row[7]),
            evidencias=evids,
        )
