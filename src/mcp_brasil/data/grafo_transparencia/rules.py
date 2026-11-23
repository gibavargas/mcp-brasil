"""Explainable cross-source red-flag detectors (5 audit rules, LGPD-safe)."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Any

from mcp_brasil.data.transparencia_municipios.homonimos_mvp import normalize_name

from .constants import (
    CAPITAL_RATIO_THRESHOLD,
    LONG_DISTANCE_PAYROLL_KM,
    MIN_CONTRACT_VALUE_FOR_CAPITAL_CHECK,
    MIN_PERSON_MATCH_SCORE,
    YOUNG_COMPANY_DAYS_THRESHOLD,
)
from .schemas import GraphEdge, GraphNode, RiskAlert, SeverityLevel
from .store import GraphStore, compute_person_match_score, haversine_km, make_stable_id


def _parse_date(raw: str) -> datetime | None:
    s = (raw or "").strip()
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%Y/%m/%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(
                s[
                    : len(
                        fmt.replace("%Y", "2000")
                        .replace("%m", "01")
                        .replace("%d", "01")
                        .replace("%H", "00")
                        .replace("%M", "00")
                        .replace("%S", "00")
                    )
                ],
                fmt,
            )
        except ValueError:
            continue
    # Fallback ano isolado (ex: "2025")
    if len(s) == 4 and s.isdigit():
        try:
            return datetime(int(s), 7, 1)
        except ValueError:
            return None
    return None


def _fmt_brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def run_all_detectors(
    store: GraphStore,
    scope_node_ids: set[str] | None = None,
    persist: bool = True,
) -> list[RiskAlert]:
    """Executa os 5 detectores explicáveis sobre o grafo DuckDB e persiste alertas."""
    nodes = {n.node_id: n for n in store.get_all_nodes()}
    edges = store.get_all_edges()

    alerts: list[RiskAlert] = []
    alerts.extend(detect_conflito_interesses(nodes, edges, scope_node_ids))
    alerts.extend(detect_sancoes_e_fachada(nodes, edges, scope_node_ids))
    alerts.extend(detect_rede_empresas(nodes, edges, scope_node_ids))
    alerts.extend(detect_follow_the_money(nodes, edges, scope_node_ids))
    alerts.extend(detect_vinculos_intermunicipais(nodes, edges, scope_node_ids))

    # Deduplica por alert_id mantendo maior score
    by_id: dict[str, RiskAlert] = {}
    for a in alerts:
        prev = by_id.get(a.alert_id)
        if prev is None or a.score > prev.score:
            by_id[a.alert_id] = a

    final_alerts = sorted(by_id.values(), key=lambda x: (-x.score, x.regra, x.entidade_id))
    if persist:
        for alert in final_alerts:
            store.upsert_alert(alert)
    return final_alerts


def _in_scope(scope: set[str] | None, *node_ids: str) -> bool:
    if scope is None:
        return True
    return any(nid in scope for nid in node_ids if nid)


# ---------------------------------------------------------------------------
# Detector 1 — Conflito de Interesses (Fornecedor <-> QSA <-> Servidor / TSE)
# ---------------------------------------------------------------------------


def detect_conflito_interesses(
    nodes: dict[str, GraphNode],
    edges: list[GraphEdge],
    scope: set[str] | None = None,
) -> list[RiskAlert]:
    """Cruza sócios (QSA) de fornecedores com servidores do município e candidatos/doadores TSE."""
    alerts: list[RiskAlert] = []

    # Mapas auxiliares
    # empresa -> set[(municipio_id, total_valor, sample_edge)]
    emp_mun_contracts: dict[str, dict[str, list[GraphEdge]]] = defaultdict(
        lambda: defaultdict(list)
    )
    # pessoa -> set[empresa_id]
    pessoa_empresas: dict[str, list[GraphEdge]] = defaultdict(list)
    # pessoa -> dict[municipio_id, list[GraphEdge]]
    pessoa_empregos: dict[str, dict[str, list[GraphEdge]]] = defaultdict(lambda: defaultdict(list))
    # pessoa -> list[GraphEdge] (vínculos TSE: CANDIDATOU_EM, DOOU_PARA, FORNECEU_CAMPANHA)
    pessoa_tse: dict[str, list[GraphEdge]] = defaultdict(list)
    # empresa -> list[GraphEdge] (vínculos TSE)
    empresa_tse: dict[str, list[GraphEdge]] = defaultdict(list)

    for e in edges:
        if e.tipo_relacao in ("CONTRATOU", "PAGOU_DESPESA"):
            if e.src_id.startswith("municipio:"):
                emp_mun_contracts[e.dst_id][e.src_id].append(e)
        elif e.tipo_relacao == "SOCIO_DE":
            pessoa_empresas[e.src_id].append(e)
        elif e.tipo_relacao == "TRABALHA_EM":
            pessoa_empregos[e.src_id][e.dst_id].append(e)
        elif e.tipo_relacao in ("CANDIDATOU_EM", "DOOU_PARA", "FORNECEU_CAMPANHA"):
            if e.src_id.startswith("pessoa:"):
                pessoa_tse[e.src_id].append(e)
            elif e.src_id.startswith("empresa:"):
                empresa_tse[e.src_id].append(e)

    # 1A) Sócio de empresa contratada que também aparece na folha do mesmo município
    for pid, soc_edges in pessoa_empresas.items():
        p_node = nodes.get(pid)
        p_nome = p_node.nome if p_node else pid
        for se in soc_edges:
            eid = se.dst_id
            e_node = nodes.get(eid)
            e_nome = e_node.nome if e_node else eid
            for mun_id, c_edges in emp_mun_contracts.get(eid, {}).items():
                if mun_id not in pessoa_empregos.get(pid, {}):
                    continue
                if not _in_scope(scope, pid, eid, mun_id):
                    continue
                m_node = nodes.get(mun_id)
                m_nome = m_node.nome if m_node else mun_id
                emp_edges = pessoa_empregos[pid][mun_id]
                total_val = sum(c.valor for c in c_edges)
                cargos = sorted(
                    {
                        str(x.metadata.get("cargo", ""))
                        for x in emp_edges
                        if x.metadata.get("cargo")
                    }
                )
                cpf_soc = str(se.metadata.get("cpf_parcial", ""))
                cpf_serv = str(emp_edges[0].metadata.get("cpf_parcial", ""))
                match_score, reasons = compute_person_match_score(
                    p_nome,
                    cpf_soc,
                    cpf_serv,
                    uf_a=m_node.uf if m_node else "",
                    uf_b=m_node.uf if m_node else "",
                )
                score = min(98.0, max(75.0, match_score + 15.0))
                alerts.append(
                    RiskAlert(
                        alert_id=make_stable_id(
                            "alerta", "CONFLITO_SOCIO_SERVIDOR", pid, eid, mun_id
                        ),
                        regra="CONFLITO_SOCIO_SERVIDOR",
                        severidade="ALTA" if score >= 80.0 else "MEDIA",
                        score=round(score, 1),
                        entidade_id=eid,
                        entidade_nome=e_nome,
                        titulo=f"Possível sócio-servidor: {p_nome} em {e_nome} ↔ {m_nome}",
                        explicacao=(
                            "Indício de sobreposição entre quadro societário "
                            f"({se.metadata.get('qualificacao') or 'Sócio'}) "
                            f"de {e_nome} e folha de pagamento de {m_nome} "
                            f"(cargo: {', '.join(cargos) or 'não informado'}). "
                            f"Volume associado no município: {_fmt_brl(total_val)} "
                            f"({len(c_edges)} registro(s)). "
                            "Sinais: "
                            f"{'; '.join(reasons) or 'coincidência exata de nome normalizado'}."
                        ),
                        evidencias=[
                            {
                                "tipo": "qsa",
                                "fonte": se.fonte,
                                "url": se.url_evidencia,
                                "socio": p_nome,
                                "empresa": e_nome,
                            },
                            {
                                "tipo": "folha_municipal",
                                "fonte": emp_edges[0].fonte,
                                "url": emp_edges[0].url_evidencia,
                                "municipio": m_nome,
                                "cargos": cargos,
                            },
                            {
                                "tipo": "contratos_despesas",
                                "quantidade": len(c_edges),
                                "valor_total": total_val,
                                "url": c_edges[0].url_evidencia,
                            },
                        ],
                    )
                )

    # 1B) Pessoa física contratada/paga que também aparece na folha do mesmo município
    for pid, mun_map in emp_mun_contracts.items():
        if not pid.startswith("pessoa:"):
            continue
        p_node = nodes.get(pid)
        p_nome = p_node.nome if p_node else pid
        for mun_id, c_edges in mun_map.items():
            if mun_id not in pessoa_empregos.get(pid, {}):
                continue
            if not _in_scope(scope, pid, mun_id):
                continue
            m_node = nodes.get(mun_id)
            m_nome = m_node.nome if m_node else mun_id
            total_val = sum(c.valor for c in c_edges)
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id("alerta", "CONFLITO_PF_SERVIDOR", pid, mun_id),
                    regra="CONFLITO_PF_SERVIDOR",
                    severidade="ALTA",
                    score=84.0,
                    entidade_id=pid,
                    entidade_nome=p_nome,
                    titulo=f"Servidor aparece como fornecedor/credor direto em {m_nome}",
                    explicacao=(
                        f"{p_nome} consta simultaneamente na folha de {m_nome} e como "
                        f"credor/contratado direto ({len(c_edges)} registro(s), total "
                        f"{_fmt_brl(total_val)}). Verificar se trata de diária/ressarcimento "
                        "regular ou contratação direta."
                    ),
                    evidencias=[
                        {
                            "tipo": "contratos_despesas",
                            "valor_total": total_val,
                            "url": c_edges[0].url_evidencia,
                        }
                    ],
                )
            )

    # 1C) Sócio de fornecedor com vínculo eleitoral (TSE: candidato ou doador) na mesma UF
    for pid, soc_edges in pessoa_empresas.items():
        tse_edges = pessoa_tse.get(pid, [])
        if not tse_edges:
            continue
        p_node = nodes.get(pid)
        p_nome = p_node.nome if p_node else pid
        for se in soc_edges:
            eid = se.dst_id
            e_node = nodes.get(eid)
            e_nome = e_node.nome if e_node else eid
            muns = emp_mun_contracts.get(eid, {})
            if not muns:
                continue
            for mun_id, c_edges in muns.items():
                if not _in_scope(scope, pid, eid, mun_id):
                    continue
                m_node = nodes.get(mun_id)
                m_nome = m_node.nome if m_node else mun_id
                m_uf = m_node.uf if m_node else ""
                relevant_tse = [
                    te
                    for te in tse_edges
                    if te.dst_id == mun_id
                    or not m_uf
                    or str(te.metadata.get("uf", "")).upper() == m_uf.upper()
                    or (
                        nodes.get(te.dst_id) is not None
                        and nodes[te.dst_id].uf.upper() == m_uf.upper()
                    )
                ]
                if not relevant_tse:
                    continue
                total_val = sum(c.valor for c in c_edges)
                rel_types = sorted({te.tipo_relacao for te in relevant_tse})
                alerts.append(
                    RiskAlert(
                        alert_id=make_stable_id("alerta", "CONFLITO_SOCIO_TSE", pid, eid, mun_id),
                        regra="CONFLITO_SOCIO_TSE",
                        severidade="ALTA" if "DOOU_PARA" in rel_types else "MEDIA",
                        score=82.0 if "DOOU_PARA" in rel_types else 74.0,
                        entidade_id=eid,
                        entidade_nome=e_nome,
                        titulo=(
                            f"Sócio de fornecedor com vínculo eleitoral (TSE): {p_nome} ↔ {e_nome}"
                        ),
                        explicacao=(
                            f"{p_nome} (sócio de {e_nome}, que recebeu {_fmt_brl(total_val)} "
                            f"em {m_nome}) possui registro eleitoral no TSE "
                            f"({', '.join(rel_types)}) na mesma jurisdição."
                        ),
                        evidencias=[
                            {
                                "tipo": "tse",
                                "relacao": te.tipo_relacao,
                                "destino": te.dst_id,
                                "valor": te.valor,
                                "url": te.url_evidencia,
                            }
                            for te in relevant_tse[:3]
                        ],
                    )
                )

    # 1D) Empresa fornecedora de campanha que também contrata com prefeitura
    for eid, tse_edges in empresa_tse.items():
        muns = emp_mun_contracts.get(eid, {})
        if not muns:
            continue
        e_node = nodes.get(eid)
        e_nome = e_node.nome if e_node else eid
        for mun_id, c_edges in muns.items():
            if not _in_scope(scope, eid, mun_id):
                continue
            m_node = nodes.get(mun_id)
            m_nome = m_node.nome if m_node else mun_id
            total_val = sum(c.valor for c in c_edges)
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id(
                        "alerta", "FORNECEDOR_CAMPANHA_CONTRATADO", eid, mun_id
                    ),
                    regra="FORNECEDOR_CAMPANHA_CONTRATADO",
                    severidade="MEDIA",
                    score=76.0,
                    entidade_id=eid,
                    entidade_nome=e_nome,
                    titulo=f"Fornecedor de campanha (TSE) contratado por {m_nome}: {e_nome}",
                    explicacao=(
                        f"{e_nome} aparece em prestação de contas eleitorais "
                        f"({len(tse_edges)} registro(s)) e possui contratos/pagamentos "
                        f"de {_fmt_brl(total_val)} em {m_nome}."
                    ),
                    evidencias=[
                        {"tipo": "tse", "valor": te.valor, "url": te.url_evidencia}
                        for te in tse_edges[:3]
                    ],
                )
            )

    return alerts


# ---------------------------------------------------------------------------
# Detector 2 — Sanções Ativas & Sinais de Empresa de Fachada
# ---------------------------------------------------------------------------


def detect_sancoes_e_fachada(
    nodes: dict[str, GraphNode],
    edges: list[GraphEdge],
    scope: set[str] | None = None,
) -> list[RiskAlert]:
    """Detecta sanções (TCU/CEIS/CNEP/CNJ), situação irregular, empresa jovem e capital baixo."""
    alerts: list[RiskAlert] = []

    emp_contracts: dict[str, list[GraphEdge]] = defaultdict(list)
    emp_sanctions: dict[str, list[GraphEdge]] = defaultdict(list)

    for e in edges:
        if e.tipo_relacao in ("CONTRATOU", "PAGOU_DESPESA") and e.dst_id.startswith("empresa:"):
            emp_contracts[e.dst_id].append(e)
        elif e.tipo_relacao == "SANCIONADA_EM":
            emp_sanctions[e.src_id].append(e)

    for eid, node in nodes.items():
        if node.tipo != "empresa":
            continue
        c_edges = emp_contracts.get(eid, [])
        s_edges = emp_sanctions.get(eid, [])
        if not _in_scope(scope, eid, *(c.src_id for c in c_edges)):
            continue

        total_val = sum(c.valor for c in c_edges)
        meta = node.metadata

        # 2A) Sanção ativa (CEIS / CNEP / TCU Inidôneos / CNJ Improbidade)
        if s_edges:
            fontes = sorted({str(se.metadata.get("emissor") or se.fonte) for se in s_edges})
            tipos = sorted({str(se.metadata.get("tipo") or "Sanção") for se in s_edges})
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id("alerta", "EMPRESA_SANCIONADA", eid),
                    regra="EMPRESA_SANCIONADA",
                    severidade="ALTA",
                    score=96.0 if c_edges else 85.0,
                    entidade_id=eid,
                    entidade_nome=node.nome,
                    titulo=f"Empresa com apontamento/sanção oficial: {node.nome}",
                    explicacao=(
                        f"{node.nome} (CNPJ {node.chave_canonica}) apresenta {len(s_edges)} "
                        f"apontamento(s) em cadastros de sanções "
                        f"({', '.join(tipos)} — {', '.join(fontes)})."
                        + (
                            f" Possui {len(c_edges)} contrato(s)/despesa(s) somando "
                            f"{_fmt_brl(total_val)}."
                            if c_edges
                            else ""
                        )
                    ),
                    evidencias=[
                        {
                            "emissor": se.metadata.get("emissor", se.fonte),
                            "tipo": se.metadata.get("tipo", ""),
                            "situacao": se.metadata.get("situacao", ""),
                            "url": se.url_evidencia,
                        }
                        for se in s_edges
                    ],
                )
            )

        # 2B) Situação cadastral diferente de ATIVA em empresa contratada
        sit = str(meta.get("descricao_situacao_cadastral") or "").strip().upper()
        if sit and sit != "ATIVA" and c_edges:
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id("alerta", "SITUACAO_CADASTRAL_IRREGULAR", eid),
                    regra="SITUACAO_CADASTRAL_IRREGULAR",
                    severidade="ALTA",
                    score=90.0,
                    entidade_id=eid,
                    entidade_nome=node.nome,
                    titulo=f"Fornecedor com situação cadastral '{sit}': {node.nome}",
                    explicacao=(
                        f"{node.nome} (CNPJ {node.chave_canonica}) consta na Receita Federal "
                        f"como '{sit}', mas possui {len(c_edges)} contrato(s)/pagamento(s) "
                        f"somando {_fmt_brl(total_val)}."
                    ),
                    evidencias=[
                        {
                            "situacao_cadastral": sit,
                            "valor_total": total_val,
                            "cnpj": node.chave_canonica,
                        }
                    ],
                )
            )

        # 2C) Empresa aberta há menos de 180 dias antes da data do contrato/pagamento
        abertura_raw = str(meta.get("data_inicio_atividade") or "").strip()
        dt_abertura = _parse_date(abertura_raw)
        if dt_abertura is not None and c_edges:
            young_hits: list[dict[str, Any]] = []
            for ce in c_edges:
                dt_c = _parse_date(ce.data_ref)
                if dt_c is None:
                    continue
                delta_days = (dt_c - dt_abertura).days
                if 0 <= delta_days <= YOUNG_COMPANY_DAYS_THRESHOLD:
                    young_hits.append(
                        {
                            "municipio": ce.src_id,
                            "data_contrato": ce.data_ref,
                            "dias_desde_abertura": delta_days,
                            "valor": ce.valor,
                            "url": ce.url_evidencia,
                        }
                    )
            if young_hits:
                min_days = min(h["dias_desde_abertura"] for h in young_hits)
                alerts.append(
                    RiskAlert(
                        alert_id=make_stable_id("alerta", "EMPRESA_RECEM_CRIADA", eid),
                        regra="EMPRESA_RECEM_CRIADA",
                        severidade="MEDIA",
                        score=75.0 if min_days <= 60 else 68.0,
                        entidade_id=eid,
                        entidade_nome=node.nome,
                        titulo=f"Empresa contratada {min_days} dias após abertura: {node.nome}",
                        explicacao=(
                            f"{node.nome} (aberta em {abertura_raw}) firmou contrato/recebeu "
                            f"pagamento apenas {min_days} dia(s) após o início de atividade "
                            f"(limiar de triagem: {YOUNG_COMPANY_DAYS_THRESHOLD} dias)."
                        ),
                        evidencias=young_hits[:5],
                    )
                )

        # 2D) Desproporção entre capital social e volume contratado
        capital = float(meta.get("capital_social") or 0.0)
        if (
            total_val >= MIN_CONTRACT_VALUE_FOR_CAPITAL_CHECK
            and capital > 0.0
            and (total_val / capital) >= CAPITAL_RATIO_THRESHOLD
        ):
            ratio = total_val / capital
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id("alerta", "CAPITAL_SOCIAL_DESPROPORCIONAL", eid),
                    regra="CAPITAL_SOCIAL_DESPROPORCIONAL",
                    severidade="MEDIA" if ratio < 50.0 else "ALTA",
                    score=min(88.0, round(60.0 + min(28.0, ratio / 2.0), 1)),
                    entidade_id=eid,
                    entidade_nome=node.nome,
                    titulo=f"Contratos {ratio:.1f}x maiores que o capital social: {node.nome}",
                    explicacao=(
                        f"{node.nome} (capital social declarado de {_fmt_brl(capital)}) acumula "
                        f"{_fmt_brl(total_val)} em contratos/despesas "
                        f"({ratio:.1f}x o capital social)."
                    ),
                    evidencias=[
                        {
                            "cnpj": node.chave_canonica,
                            "capital_social": capital,
                            "valor_total": total_val,
                            "razao": round(ratio, 2),
                        }
                    ],
                )
            )

    return alerts


# ---------------------------------------------------------------------------
# Detector 3 — Rede de Empresas Compartilhadas & Concentração Regional
# ---------------------------------------------------------------------------


def detect_rede_empresas(
    nodes: dict[str, GraphNode],
    edges: list[GraphEdge],
    scope: set[str] | None = None,
) -> list[RiskAlert]:
    """Identifica sócios comuns, endereços/contatos compartilhados e atuação multi-municipal."""
    alerts: list[RiskAlert] = []

    emp_muns: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    socio_emps: dict[str, set[str]] = defaultdict(set)

    for e in edges:
        if e.tipo_relacao in ("CONTRATOU", "PAGOU_DESPESA") and e.dst_id.startswith("empresa:"):
            emp_muns[e.dst_id][e.src_id] += e.valor
        elif e.tipo_relacao == "SOCIO_DE" and e.dst_id.startswith("empresa:"):
            socio_emps[e.src_id].add(e.dst_id)

    # 3A) Mesmo sócio em >= 2 CNPJs distintos presentes no grafo
    for pid, eids in socio_emps.items():
        if len(eids) < 2:
            continue
        if not _in_scope(scope, pid, *eids):
            continue
        p_node = nodes.get(pid)
        p_nome = p_node.nome if p_node else pid
        # Verifica se essas empresas compartilham algum município contratante
        muns_per_emp = {eid: set(emp_muns.get(eid, {}).keys()) for eid in eids}
        shared_muns: set[str] = set()
        eid_list = sorted(eids)
        for i in range(len(eid_list)):
            for j in range(i + 1, len(eid_list)):
                shared_muns |= muns_per_emp[eid_list[i]] & muns_per_emp[eid_list[j]]

        total_val = sum(sum(emp_muns.get(eid, {}).values()) for eid in eids)
        emp_names = [nodes[eid].nome if eid in nodes else eid for eid in eid_list]
        sev: SeverityLevel = "ALTA" if shared_muns else "MEDIA"
        score = 84.0 if shared_muns else 68.0
        alerts.append(
            RiskAlert(
                alert_id=make_stable_id("alerta", "SOCIO_MULTIPLAS_EMPRESAS", pid, *eid_list),
                regra="SOCIO_MULTIPLAS_EMPRESAS",
                severidade=sev,
                score=score,
                entidade_id=pid,
                entidade_nome=p_nome,
                titulo=f"Sócio comum em {len(eids)} empresas: {p_nome}",
                explicacao=(
                    f"{p_nome} figura no QSA de {len(eids)} CNPJs distintos "
                    f"({', '.join(emp_names[:4])}), somando {_fmt_brl(total_val)}."
                    + (
                        f" {len(shared_muns)} município(s) contrataram mais de uma empresa "
                        "ligada ao mesmo sócio."
                        if shared_muns
                        else ""
                    )
                ),
                evidencias=[
                    {
                        "empresas": emp_names,
                        "municipios_em_comum": sorted(shared_muns),
                        "valor_total": total_val,
                    }
                ],
            )
        )

    # 3B) Empresas distintas compartilhando mesmo endereço (CEP + número) ou telefone ou e-mail
    by_addr: dict[str, list[GraphNode]] = defaultdict(list)
    by_phone: dict[str, list[GraphNode]] = defaultdict(list)
    by_email: dict[str, list[GraphNode]] = defaultdict(list)

    for node in nodes.values():
        if node.tipo != "empresa":
            continue
        meta = node.metadata
        cep = "".join(ch for ch in str(meta.get("cep") or "") if ch.isdigit())
        num = normalize_name(str(meta.get("numero") or ""))
        if len(cep) == 8 and num and num not in ("SN", "S N", "0"):
            by_addr[f"{cep}|{num}"].append(node)
        phone = "".join(ch for ch in str(meta.get("ddd_telefone_1") or "") if ch.isdigit())
        if len(phone) >= 10:
            by_phone[phone].append(node)
        email = str(meta.get("email") or "").strip().lower()
        if "@" in email and not any(
            generic in email for generic in ("contabil", "escritorio", "gmail.com", "hotmail.com")
        ):
            by_email[email].append(node)

    for label, bucket in (
        ("ENDERECO_COMPARTILHADO", by_addr),
        ("TELEFONE_COMPARTILHADO", by_phone),
        ("EMAIL_COMPARTILHADO", by_email),
    ):
        for key, group in bucket.items():
            # Ignora filiais da mesma raiz CNPJ (8 dígitos iguais)
            roots = {str(n.metadata.get("cnpj_raiz") or n.chave_canonica[:8]) for n in group}
            if len(roots) < 2:
                continue
            if not _in_scope(scope, *(n.node_id for n in group)):
                continue
            names = [f"{n.nome} ({n.chave_canonica})" for n in group]
            kind = label.split("_")[0].lower()
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id("alerta", label, key),
                    regra=label,
                    severidade="MEDIA",
                    score=74.0,
                    entidade_id=group[0].node_id,
                    entidade_nome=group[0].nome,
                    titulo=f"{len(group)} CNPJs de grupos distintos compartilham {kind}",
                    explicacao=(
                        "Empresas com raízes de CNPJ diferentes compartilham o mesmo "
                        f"identificador ({key}): {'; '.join(names[:5])}."
                    ),
                    evidencias=[{"chave": key, "empresas": names}],
                )
            )

    # 3C) Concentração multi-municipal (mesmo CNPJ faturando em >= 3 municípios)
    for eid, m_map in emp_muns.items():
        if len(m_map) < 3:
            continue
        if not _in_scope(scope, eid, *m_map.keys()):
            continue
        e_node = nodes.get(eid)
        e_nome = e_node.nome if e_node else eid
        total_val = sum(m_map.values())
        mun_nodes = [nodes[mid] for mid in m_map if mid in nodes]
        max_dist = 0.0
        for i in range(len(mun_nodes)):
            for j in range(i + 1, len(mun_nodes)):
                d = haversine_km(
                    float(mun_nodes[i].metadata.get("latitude") or 0.0),
                    float(mun_nodes[i].metadata.get("longitude") or 0.0),
                    float(mun_nodes[j].metadata.get("latitude") or 0.0),
                    float(mun_nodes[j].metadata.get("longitude") or 0.0),
                )
                max_dist = max(max_dist, d)
        alerts.append(
            RiskAlert(
                alert_id=make_stable_id("alerta", "FORNECEDOR_MULTI_MUNICIPAL", eid),
                regra="FORNECEDOR_MULTI_MUNICIPAL",
                severidade="INFO" if len(m_map) < 5 else "BAIXA",
                score=52.0 if len(m_map) < 5 else 64.0,
                entidade_id=eid,
                entidade_nome=e_nome,
                titulo=f"Fornecedor presente em {len(m_map)} municípios: {e_nome}",
                explicacao=(
                    f"{e_nome} possui contratos/pagamentos em {len(m_map)} municípios distintos "
                    f"somando {_fmt_brl(total_val)}"
                    + (
                        f" (raio máximo entre municípios: {max_dist:.0f} km)."
                        if max_dist > 0
                        else "."
                    )
                ),
                evidencias=[
                    {
                        "municipio": nodes[mid].nome if mid in nodes else mid,
                        "valor": val,
                    }
                    for mid, val in sorted(m_map.items(), key=lambda kv: -kv[1])[:8]
                ],
            )
        )

    return alerts


# ---------------------------------------------------------------------------
# Detector 4 — Follow the Money (Emenda -> Município -> Fornecedores/Sócios)
# ---------------------------------------------------------------------------


def detect_follow_the_money(
    nodes: dict[str, GraphNode],
    edges: list[GraphEdge],
    scope: set[str] | None = None,
) -> list[RiskAlert]:
    """Rastreia o fluxo de emendas parlamentares até o município e seus maiores contratados."""
    alerts: list[RiskAlert] = []

    mun_emendas: dict[str, list[GraphEdge]] = defaultdict(list)
    mun_contracts: dict[str, list[GraphEdge]] = defaultdict(list)
    politico_doadores: dict[str, set[str]] = defaultdict(set)
    emp_socios: dict[str, set[str]] = defaultdict(set)

    for e in edges:
        if e.tipo_relacao == "DESTINOU_EMENDA":
            mun_emendas[e.dst_id].append(e)
        elif e.tipo_relacao in ("CONTRATOU", "PAGOU_DESPESA") and e.src_id.startswith(
            "municipio:"
        ):
            mun_contracts[e.src_id].append(e)
        elif e.tipo_relacao in ("DOOU_PARA", "FORNECEU_CAMPANHA"):
            politico_doadores[e.dst_id].add(e.src_id)
        elif e.tipo_relacao == "SOCIO_DE":
            emp_socios[e.dst_id].add(e.src_id)

    for mun_id, em_edges in mun_emendas.items():
        c_edges = mun_contracts.get(mun_id, [])
        if not c_edges:
            continue
        if not _in_scope(scope, mun_id, *(e.src_id for e in em_edges)):
            continue
        m_node = nodes.get(mun_id)
        m_nome = m_node.nome if m_node else mun_id
        total_emendas = sum(e.valor for e in em_edges)

        # Verifica fechamento de ciclo: autor recebeu doação de empresa ou sócio contratado
        for em in em_edges:
            pol_id = em.src_id
            pol_nome = nodes[pol_id].nome if pol_id in nodes else pol_id
            donors = politico_doadores.get(pol_id, set())
            if not donors:
                continue
            for ce in c_edges:
                emp_id = ce.dst_id
                linked = (emp_id in donors) or bool(emp_socios.get(emp_id, set()) & donors)
                if linked:
                    emp_nome = nodes[emp_id].nome if emp_id in nodes else emp_id
                    alerts.append(
                        RiskAlert(
                            alert_id=make_stable_id(
                                "alerta", "CICLO_EMENDA_DOADOR_CONTRATO", pol_id, mun_id, emp_id
                            ),
                            regra="CICLO_EMENDA_DOADOR_CONTRATO",
                            severidade="ALTA",
                            score=92.0,
                            entidade_id=emp_id,
                            entidade_nome=emp_nome,
                            titulo=(
                                "Ciclo Emenda → Município → Fornecedor/Doador: "
                                f"{pol_nome} ↔ {emp_nome}"
                            ),
                            explicacao=(
                                f"{pol_nome} destinou {_fmt_brl(em.valor)} em emenda para "
                                f"{m_nome}, município que contratou/pagou {emp_nome} "
                                f"({_fmt_brl(ce.valor)}), entidade ligada a doador/fornecedor "
                                "de campanha do mesmo parlamentar."
                            ),
                            evidencias=[
                                {
                                    "etapa": "emenda",
                                    "autor": pol_nome,
                                    "valor": em.valor,
                                    "url": em.url_evidencia,
                                },
                                {
                                    "etapa": "contrato",
                                    "municipio": m_nome,
                                    "empresa": emp_nome,
                                    "valor": ce.valor,
                                    "url": ce.url_evidencia,
                                },
                            ],
                        )
                    )

        # Sinal informativo de rastreabilidade de emendas vs. maiores fornecedores do município
        if total_emendas > 0:
            by_forn: dict[str, float] = defaultdict(float)
            for ce in c_edges:
                by_forn[ce.dst_id] += ce.valor
            top_forns = sorted(by_forn.items(), key=lambda kv: -kv[1])[:3]
            top_str = ", ".join(
                f"{nodes[fid].nome if fid in nodes else fid} ({_fmt_brl(val)})"
                for fid, val in top_forns
            )
            alerts.append(
                RiskAlert(
                    alert_id=make_stable_id("alerta", "RASTREIO_EMENDAS_MUNICIPIO", mun_id),
                    regra="RASTREIO_EMENDAS_MUNICIPIO",
                    severidade="INFO",
                    score=45.0,
                    entidade_id=mun_id,
                    entidade_nome=m_nome,
                    titulo=(
                        f"Fluxo de {len(em_edges)} emenda(s) ({_fmt_brl(total_emendas)}) "
                        f"conectado a contratos em {m_nome}"
                    ),
                    explicacao=(
                        f"{m_nome} recebeu {len(em_edges)} emenda(s) parlamentar(es) totalizando "
                        f"{_fmt_brl(total_emendas)}. Principais credores/fornecedores mapeados "
                        f"no município: {top_str}."
                    ),
                    evidencias=[
                        {
                            "autor": nodes[e.src_id].nome if e.src_id in nodes else e.src_id,
                            "valor": e.valor,
                            "url": e.url_evidencia,
                        }
                        for e in em_edges[:5]
                    ],
                )
            )

    return alerts


# ---------------------------------------------------------------------------
# Detector 5 — Vínculos Intermunicipais de Pessoas (Generalização do homonimos_mvp)
# ---------------------------------------------------------------------------


def detect_vinculos_intermunicipais(
    nodes: dict[str, GraphNode],
    edges: list[GraphEdge],
    scope: set[str] | None = None,
) -> list[RiskAlert]:
    """Detecta vínculos de folha em >= 2 municípios com score explicável e distância geodésica."""
    alerts: list[RiskAlert] = []
    person_muns: dict[str, dict[str, list[GraphEdge]]] = defaultdict(lambda: defaultdict(list))

    for e in edges:
        if e.tipo_relacao == "TRABALHA_EM" and e.src_id.startswith("pessoa:"):
            person_muns[e.src_id][e.dst_id].append(e)

    # Detecta pares espelhados (mesma portal_url ou >=10 sobreposições idênticas a >120 km)
    pair_overlaps: dict[tuple[str, str], int] = defaultdict(int)
    for m_map in person_muns.values():
        if len(m_map) < 2:
            continue
        m_list = sorted(m_map.keys())
        for i in range(len(m_list)):
            for j in range(i + 1, len(m_list)):
                pair_overlaps[(m_list[i], m_list[j])] += 1

    def _is_mirrored_pair(mid_a: str, mid_b: str, dist_km: float) -> bool:
        pair = (mid_a, mid_b) if mid_a < mid_b else (mid_b, mid_a)
        na, nb = nodes.get(mid_a), nodes.get(mid_b)
        ua = str((na.metadata if na else {}).get("portal_url") or "").strip().lower().rstrip("/")
        ub = str((nb.metadata if nb else {}).get("portal_url") or "").strip().lower().rstrip("/")
        if ua and ub and ua == ub:
            return True
        return dist_km > 120.0 and pair_overlaps.get(pair, 0) >= 10

    for pid, m_map in person_muns.items():
        if len(m_map) < 2:
            continue
        if not _in_scope(scope, pid, *m_map.keys()):
            continue
        p_node = nodes.get(pid)
        p_nome = p_node.nome if p_node else pid

        mun_ids = sorted(m_map.keys())
        best_score = 0.0
        max_dist = 0.0
        best_reasons: list[str] = []
        valid_mun_ids: set[str] = set()

        for i in range(len(mun_ids)):
            for j in range(i + 1, len(mun_ids)):
                m1 = nodes.get(mun_ids[i])
                m2 = nodes.get(mun_ids[j])
                e1 = m_map[mun_ids[i]][0]
                e2 = m_map[mun_ids[j]][0]
                u1 = (e1.url_evidencia or "").strip().lower().rstrip("/")
                u2 = (e2.url_evidencia or "").strip().lower().rstrip("/")
                if u1 and u2 and u1 == u2:
                    continue
                dist = 0.0
                if m1 and m2:
                    dist = haversine_km(
                        float(m1.metadata.get("latitude") or 0.0),
                        float(m1.metadata.get("longitude") or 0.0),
                        float(m2.metadata.get("latitude") or 0.0),
                        float(m2.metadata.get("longitude") or 0.0),
                    )
                if _is_mirrored_pair(mun_ids[i], mun_ids[j], dist):
                    continue
                valid_mun_ids.add(mun_ids[i])
                valid_mun_ids.add(mun_ids[j])
                sc, reasons = compute_person_match_score(
                    nome=p_nome,
                    cpf_a=str(e1.metadata.get("cpf_parcial", "")),
                    cpf_b=str(e2.metadata.get("cpf_parcial", "")),
                    cargo_a=str(e1.metadata.get("cargo", "")),
                    cargo_b=str(e2.metadata.get("cargo", "")),
                    uf_a=m1.uf if m1 else "",
                    uf_b=m2.uf if m2 else "",
                    dist_km=dist if dist > 0 else None,
                )
                if sc > best_score:
                    best_score = sc
                    best_reasons = reasons
                max_dist = max(max_dist, dist)

        if len(valid_mun_ids) < 2 or best_score < MIN_PERSON_MATCH_SCORE:
            continue

        valid_sorted = sorted(valid_mun_ids)
        total_sal = sum(max((x.valor for x in m_map[mid]), default=0.0) for mid in valid_sorted)
        mun_labels = [nodes[mid].nome if mid in nodes else mid for mid in valid_sorted]
        sev: SeverityLevel = (
            "ALTA"
            if (len(valid_sorted) >= 3 or max_dist >= LONG_DISTANCE_PAYROLL_KM)
            and best_score >= 75.0
            else "MEDIA"
        )

        alerts.append(
            RiskAlert(
                alert_id=make_stable_id("alerta", "VINCULO_INTERMUNICIPAL", pid, *valid_sorted),
                regra="VINCULO_INTERMUNICIPAL",
                severidade=sev,
                score=best_score,
                entidade_id=pid,
                entidade_nome=p_nome,
                titulo=f"Indício de vínculo em {len(valid_sorted)} órgãos/municípios: {p_nome}",
                explicacao=(
                    f"{p_nome} aparece em folhas distintas ({', '.join(mun_labels[:5])}), "
                    f"soma estimada de remunerações: {_fmt_brl(total_sal)}"
                    + (f", distância máxima de {max_dist:.0f} km" if max_dist > 0 else "")
                    + f". Sinais: {'; '.join(best_reasons)}."
                ),
                evidencias=[
                    {
                        "municipio": nodes[mid].nome if mid in nodes else mid,
                        "cargo": m_map[mid][0].metadata.get("cargo", ""),
                        "salario": max((x.valor for x in m_map[mid]), default=0.0),
                        "cpf_parcial": m_map[mid][0].metadata.get("cpf_parcial", ""),
                        "url": m_map[mid][0].url_evidencia,
                    }
                    for mid in valid_sorted
                ],
            )
        )

    return alerts
