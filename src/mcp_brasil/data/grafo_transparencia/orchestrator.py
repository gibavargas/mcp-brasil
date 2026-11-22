"""Async On-Demand 360° orchestrator with controlled fan-out, DuckDB TTL cache, and degradation."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import defaultdict
from typing import Any

from mcp_brasil._shared.http_client import http_get
from mcp_brasil.data.brasilapi.constants import CNPJ_URL as BRASILAPI_CNPJ_URL
from mcp_brasil.data.tcu import client as tcu_client
from mcp_brasil.data.transferegov import client as transferegov_client
from mcp_brasil.data.transparencia import client as transparencia_fed_client
from mcp_brasil.data.transparencia_municipios import client as mun_client
from mcp_brasil.data.transparencia_municipios.homonimos_mvp import normalize_name
from mcp_brasil.data.tse import client as tse_client

from .constants import (
    DEFAULT_CACHE_TTL_SECONDS,
    DEFAULT_MAX_CONCURRENCY,
    DEFAULT_MAX_FORNECEDORES,
    DEFAULT_ONDEMAND_TIMEOUT_SECONDS,
)
from .rules import run_all_detectors
from .schemas import DossierResult, GraphEdge, GraphNode
from .store import (
    GraphStore,
    cnpj_root,
    make_empresa_id,
    make_municipio_id,
    make_pessoa_id,
    make_politico_id,
    make_stable_id,
    mask_cpf,
    normalize_cnpj,
)

logger = logging.getLogger(__name__)
_TRANSFEREGOV_EMENDA_URL = (
    "https://api.transferegov.gestao.gov.br/transferenciasespeciais/plano_acao_especial"
)


def has_federal_transparencia_key() -> bool:
    """Indica se a chave opcional do Portal da Transparência Federal está configurada."""
    return bool(os.environ.get("TRANSPARENCIA_API_KEY", "").strip())


# ---------------------------------------------------------------------------
# Fetchers individuais com cache DuckDB (TTL) e degradação graciosa
# ---------------------------------------------------------------------------


async def fetch_cnpj_qsa(
    store: GraphStore,
    cnpj: str,
    forcar_atualizacao: bool = False,
) -> dict[str, Any] | None:
    """Consulta CNPJ completo (QSA e data_inicio_atividade) na BrasilAPI com cache DuckDB."""
    norm = normalize_cnpj(cnpj)
    if not norm:
        return None
    cache_key = f"brasilapi:cnpj:{norm}"
    if not forcar_atualizacao:
        cached = store.get_cache(cache_key)
        if isinstance(cached, dict):
            return cached
    try:
        url = BRASILAPI_CNPJ_URL.format(cnpj=norm)
        raw = await http_get(url)
        if isinstance(raw, dict):
            store.set_cache(cache_key, "brasilapi", raw, DEFAULT_CACHE_TTL_SECONDS)
            return raw
    except Exception as exc:
        logger.debug("BrasilAPI CNPJ %s indisponível: %s", norm, exc)
    return None


async def fetch_tcu_certidoes(
    store: GraphStore,
    cnpj: str,
    forcar_atualizacao: bool = False,
) -> dict[str, Any] | None:
    """Consulta Certidão Consolidada APF do TCU (TCU Inidôneos + CNJ + CEIS + CNEP — sem chave)."""
    norm = normalize_cnpj(cnpj)
    if not norm:
        return None
    cache_key = f"tcu:certidoes:{norm}"
    if not forcar_atualizacao:
        cached = store.get_cache(cache_key)
        if isinstance(cached, dict):
            return cached
    try:
        res = await tcu_client.consultar_certidoes(norm)
        data = res.model_dump()
        store.set_cache(cache_key, "tcu", data, DEFAULT_CACHE_TTL_SECONDS)
        return data
    except Exception as exc:
        logger.debug("TCU certidões %s indisponível: %s", norm, exc)
    return None


async def fetch_emendas_municipio(
    store: GraphStore,
    nome_municipio: str,
    uf: str,
    ano: int | None = None,
    forcar_atualizacao: bool = False,
) -> list[dict[str, Any]]:
    """Consulta emendas Pix / transferências especiais no TransfereGov (sem chave)."""
    cache_key = f"transferegov:mun:{normalize_name(nome_municipio)}:{uf.upper()}:{ano or 'all'}"
    if not forcar_atualizacao:
        cached = store.get_cache(cache_key)
        if isinstance(cached, list):
            return cached
    try:
        items = await transferegov_client.emendas_por_municipio(nome_municipio, ano=ano)
        filtered = [
            it.model_dump()
            for it in items
            if not uf or not it.uf_beneficiario or it.uf_beneficiario.upper() == uf.upper()
        ]
        store.set_cache(cache_key, "transferegov", filtered, DEFAULT_CACHE_TTL_SECONDS)
        return filtered
    except Exception as exc:
        logger.debug("Transferegov emendas (%s/%s) indisponível: %s", nome_municipio, uf, exc)
    return []


async def fetch_emendas_autor(
    store: GraphStore,
    nome_autor: str,
    ano: int | None = None,
    forcar_atualizacao: bool = False,
) -> list[dict[str, Any]]:
    """Consulta emendas Pix por nome do parlamentar no TransfereGov."""
    cache_key = f"transferegov:autor:{normalize_name(nome_autor)}:{ano or 'all'}"
    if not forcar_atualizacao:
        cached = store.get_cache(cache_key)
        if isinstance(cached, list):
            return cached
    try:
        items = await transferegov_client.buscar_emenda_por_autor(nome_autor, ano=ano)
        data = [it.model_dump() for it in items]
        store.set_cache(cache_key, "transferegov", data, DEFAULT_CACHE_TTL_SECONDS)
        return data
    except Exception as exc:
        logger.debug("Transferegov autor (%s) indisponível: %s", nome_autor, exc)
    return []


async def fetch_tse_candidato(
    store: GraphStore,
    nome: str,
    uf: str | None = None,
    ano: int = 2024,
    municipio_nome: str | None = None,
    forcar_atualizacao: bool = False,
) -> list[dict[str, Any]]:
    """Consulta candidatos municipais no TSE (DivulgaCandContas) por nome, UF e município."""
    norm_nome = normalize_name(nome)
    uf_norm = (uf or "").strip().upper()
    mun_norm = normalize_name(municipio_nome or "")
    if not norm_nome or not uf_norm or not mun_norm:
        return []

    cache_key = f"tse:cand:{norm_nome}:{uf_norm}:{mun_norm}:{ano}"
    if not forcar_atualizacao:
        cached = store.get_cache(cache_key)
        if isinstance(cached, list):
            return cached
    try:
        eleicoes = await tse_client.listar_eleicoes()
        eleicao = next(
            (e for e in eleicoes if e.ano == ano and (e.turno in (None, 1, "1")) and e.id),
            None,
        )
        if not eleicao or not eleicao.id:
            return []

        muns = await tse_client.listar_municipios_eleitorais(ano, uf_norm)
        mun_tse = next(
            (m for m in muns if normalize_name(m.nome or "") == mun_norm and m.codigo_tse),
            None,
        )
        codigo_tse = mun_tse.codigo_tse if mun_tse else None
        if not mun_tse or not codigo_tse or not codigo_tse.isdigit():
            return []

        mun_code = int(codigo_tse)
        cargo_labels = {11: "Prefeito", 12: "Vice-Prefeito", 13: "Vereador"}
        results = await asyncio.gather(
            *(
                tse_client.listar_candidatos(ano, mun_code, int(eleicao.id), cargo_code)
                for cargo_code in cargo_labels
            ),
            return_exceptions=True,
        )

        target_tokens = [t for t in norm_nome.split() if len(t) > 2]
        matches: list[dict[str, Any]] = []
        for (cargo_code, cargo_label), res in zip(cargo_labels.items(), results, strict=True):
            if not isinstance(res, list):
                continue
            for cand in res:
                urna_norm = normalize_name(cand.nome_urna or "")
                if not urna_norm:
                    continue
                if urna_norm == norm_nome or (
                    len(target_tokens) >= 2 and all(tok in urna_norm for tok in target_tokens[:2])
                ):
                    matches.append(
                        {
                            "id": cand.id,
                            "nome": cand.nome_urna or nome,
                            "uf": uf_norm,
                            "municipio": mun_tse.nome,
                            "cargo": cargo_label,
                            "cargo_codigo": cargo_code,
                            "partido": cand.partido or "",
                            "situacao": cand.situacao or "",
                            "ano": ano,
                        }
                    )
        store.set_cache(cache_key, "tse", matches, DEFAULT_CACHE_TTL_SECONDS)
        return matches
    except Exception as exc:
        logger.debug("TSE consulta (%s/%s) indisponível: %s", nome, uf_norm, exc)
    return []


async def fetch_federal_sancoes_e_contratos(
    store: GraphStore,
    cnpj: str,
    forcar_atualizacao: bool = False,
) -> dict[str, Any]:
    """Enriquecimento opcional no Portal da Transparência Federal (usa TRANSPARENCIA_API_KEY)."""
    if not has_federal_transparencia_key():
        return {"disponivel": False, "sancoes": [], "contratos": []}
    norm = normalize_cnpj(cnpj)
    if not norm:
        return {"disponivel": False, "sancoes": [], "contratos": []}
    cache_key = f"transparencia_fed:cnpj:{norm}"
    if not forcar_atualizacao:
        cached = store.get_cache(cache_key)
        if isinstance(cached, dict):
            return cached

    sancoes: list[dict[str, Any]] = []
    contratos: list[dict[str, Any]] = []
    try:
        s_res, c_res = await asyncio.gather(
            transparencia_fed_client.buscar_sancoes(consulta=norm, bases=["ceis", "cnep"]),
            transparencia_fed_client.buscar_contratos(cpf_cnpj=norm),
            return_exceptions=True,
        )
        if isinstance(s_res, list):
            sancoes = [x.model_dump() for x in s_res]
        if isinstance(c_res, list):
            contratos = [x.model_dump() for x in c_res]
    except Exception as exc:
        logger.debug("Portal Transparência Federal %s falhou: %s", norm, exc)

    out = {"disponivel": True, "sancoes": sancoes, "contratos": contratos}
    store.set_cache(cache_key, "transparencia_federal", out, DEFAULT_CACHE_TTL_SECONDS)
    return out


# ---------------------------------------------------------------------------
# Ingestão de entidades enriquecidas no GraphStore
# ---------------------------------------------------------------------------


def _ingest_cnpj_enrichment(
    store: GraphStore,
    cnpj: str,
    cnpj_data: dict[str, Any] | None,
    tcu_data: dict[str, Any] | None,
    fed_data: dict[str, Any] | None = None,
) -> tuple[str, list[str]]:
    """Persiste empresa, sócios (QSA) e sanções no grafo; retorna (empresa_id, list[socio_ids])."""
    norm = normalize_cnpj(cnpj)
    if not norm:
        return "", []
    eid = make_empresa_id(norm)
    socio_ids: list[str] = []

    meta: dict[str, Any] = {"cnpj": norm, "cnpj_raiz": cnpj_root(norm)}
    nome = norm
    uf = ""

    if isinstance(cnpj_data, dict):
        nome = str(cnpj_data.get("razao_social") or cnpj_data.get("nome_fantasia") or norm).strip()
        uf = str(cnpj_data.get("uf") or "").strip().upper()
        meta.update(
            {
                "nome_fantasia": cnpj_data.get("nome_fantasia") or "",
                "descricao_situacao_cadastral": cnpj_data.get("descricao_situacao_cadastral")
                or "",
                "data_inicio_atividade": cnpj_data.get("data_inicio_atividade") or "",
                "capital_social": float(cnpj_data.get("capital_social") or 0.0),
                "cnae_fiscal": cnpj_data.get("cnae_fiscal") or "",
                "cnae_fiscal_descricao": cnpj_data.get("cnae_fiscal_descricao") or "",
                "porte": cnpj_data.get("porte") or "",
                "cep": cnpj_data.get("cep") or "",
                "logradouro": cnpj_data.get("logradouro") or "",
                "numero": cnpj_data.get("numero") or "",
                "municipio_sede": cnpj_data.get("municipio") or "",
                "ddd_telefone_1": cnpj_data.get("ddd_telefone_1") or "",
                "email": cnpj_data.get("email") or "",
            }
        )

    store.upsert_node(
        GraphNode(
            node_id=eid,
            tipo="empresa",
            chave_canonica=norm,
            nome=nome,
            uf=uf,
            metadata=meta,
        )
    )

    # Sócios (QSA) -> pessoa --[SOCIO_DE]--> empresa
    qsa = cnpj_data.get("qsa") if isinstance(cnpj_data, dict) else None
    if isinstance(qsa, list):
        url_cnpj = BRASILAPI_CNPJ_URL.format(cnpj=norm)
        for socio in qsa:
            if not isinstance(socio, dict):
                continue
            s_nome = str(socio.get("nome_socio") or socio.get("nome") or "").strip()
            if not s_nome:
                continue
            s_doc = str(socio.get("cnpj_cpf_do_socio") or "")
            s_cnpj = normalize_cnpj(s_doc)
            if s_cnpj:
                # Sócio pessoa jurídica (holding / empresa controladora)
                sid = make_empresa_id(s_cnpj)
                store.upsert_node(
                    GraphNode(
                        node_id=sid,
                        tipo="empresa",
                        chave_canonica=s_cnpj,
                        nome=s_nome,
                        metadata={"cnpj": s_cnpj, "cnpj_raiz": cnpj_root(s_cnpj)},
                    )
                )
            else:
                sid = make_pessoa_id(s_nome)
                if not sid:
                    continue
                store.upsert_node(
                    GraphNode(
                        node_id=sid,
                        tipo="pessoa",
                        chave_canonica=normalize_name(s_nome),
                        nome=s_nome,
                        uf=uf,
                        metadata={"cpf_parcial": mask_cpf(s_doc)},
                    )
                )
            socio_ids.append(sid)
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id("edge", sid, "SOCIO_DE", eid),
                    src_id=sid,
                    dst_id=eid,
                    tipo_relacao="SOCIO_DE",
                    data_ref=str(socio.get("data_entrada_sociedade") or ""),
                    fonte="BrasilAPI (Receita Federal QSA)",
                    url_evidencia=url_cnpj,
                    metadata={
                        "qualificacao": str(socio.get("qualificacao_socio") or ""),
                        "cpf_parcial": mask_cpf(s_doc),
                    },
                )
            )

    # Certidão Consolidada TCU (TCU Inidôneos, CNJ Improbidade, CEIS, CNEP)
    if isinstance(tcu_data, dict):
        tcu_url = f"https://certidoes-apf.apps.tcu.gov.br/api/rest/publico/certidoes/{norm}"
        for cert in tcu_data.get("certidoes") or []:
            if not isinstance(cert, dict):
                continue
            sit = str(cert.get("situacao") or "").strip().upper()
            if not sit or "NADA CONSTA" in sit or sit in ("REGULAR", "NEGATIVA"):
                continue
            emissor = str(cert.get("emissor") or "TCU")
            tipo_c = str(cert.get("tipo") or "Certidão")
            sanc_id = make_stable_id("sancao", norm, emissor, tipo_c)
            store.upsert_node(
                GraphNode(
                    node_id=sanc_id,
                    tipo="sancao",
                    chave_canonica=f"{emissor}:{tipo_c}",
                    nome=f"{emissor} — {tipo_c} ({sit})",
                    metadata={"emissor": emissor, "tipo": tipo_c, "situacao": sit},
                )
            )
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id("edge", eid, "SANCIONADA_EM", sanc_id),
                    src_id=eid,
                    dst_id=sanc_id,
                    tipo_relacao="SANCIONADA_EM",
                    data_ref=str(cert.get("data_hora_emissao") or ""),
                    fonte=f"TCU Certidão Consolidada ({emissor})",
                    url_evidencia=tcu_url,
                    metadata={"emissor": emissor, "tipo": tipo_c, "situacao": sit},
                )
            )

    # Enriquecimento opcional: Portal da Transparência Federal
    if isinstance(fed_data, dict) and fed_data.get("disponivel"):
        for s in fed_data.get("sancoes") or []:
            tipo_s = str(s.get("tipo_sancao") or s.get("cadastro") or "CEIS")
            sanc_id = make_stable_id("sancao", norm, "CGU", tipo_s)
            store.upsert_node(
                GraphNode(
                    node_id=sanc_id,
                    tipo="sancao",
                    chave_canonica=f"CGU:{tipo_s}",
                    nome=f"Portal Transparência Federal — {tipo_s}",
                    metadata=s,
                )
            )
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id("edge", eid, "SANCIONADA_EM", sanc_id),
                    src_id=eid,
                    dst_id=sanc_id,
                    tipo_relacao="SANCIONADA_EM",
                    data_ref=str(s.get("data_inicio_sancao") or ""),
                    fonte="Portal da Transparência Federal (CEIS/CNEP)",
                    url_evidencia="https://portaldatransparencia.gov.br/sancoes",
                    metadata={"emissor": "CGU", "tipo": tipo_s, "situacao": "ATIVA"},
                )
            )
        for ct in fed_data.get("contratos") or []:
            org = str(ct.get("orgao") or "Órgão Federal").strip()
            org_id = make_stable_id("orgao", org)
            store.upsert_node(
                GraphNode(
                    node_id=org_id,
                    tipo="municipio",
                    chave_canonica=normalize_name(org),
                    nome=org,
                    metadata={"esfera": "federal"},
                )
            )
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id(
                        "edge", org_id, "CONTRATOU", eid, str(ct.get("numero") or "")
                    ),
                    src_id=org_id,
                    dst_id=eid,
                    tipo_relacao="CONTRATOU",
                    valor=float(ct.get("valor_inicial") or ct.get("valor_final") or 0.0),
                    data_ref=str(ct.get("data_assinatura") or ""),
                    fonte="Portal da Transparência Federal",
                    url_evidencia="https://portaldatransparencia.gov.br/contratos",
                    metadata={"objeto": str(ct.get("objeto") or "")[:300]},
                )
            )

    return eid, socio_ids


# ---------------------------------------------------------------------------
# Investigações 360° de Alto Nível
# ---------------------------------------------------------------------------


async def investigar_municipio_360(
    store: GraphStore,
    municipio: str,
    uf: str | None = None,
    ano: int | None = None,
    max_fornecedores: int = DEFAULT_MAX_FORNECEDORES,
    forcar_atualizacao: bool = False,
    timeout_seconds: float = DEFAULT_ONDEMAND_TIMEOUT_SECONDS,
) -> DossierResult:
    """Investigação 360° de município cruzando portal municipal, QSA, TCU e Transferegov."""
    t0 = time.monotonic()
    fontes: list[str] = []
    ausentes: list[str] = []
    if not has_federal_transparencia_key():
        ausentes.append("Portal da Transparência Federal (TRANSPARENCIA_API_KEY não configurada)")

    # 1. Resolve o município no catálogo nacional (5.571 cidades)
    busca_res = await mun_client.buscar_municipios(municipio, uf=uf, limit=1)
    if not busca_res.municipios:
        raise ValueError(
            f"Município '{municipio}' (UF={uf or '*'}) não encontrado no cadastro IBGE."
        )
    m = busca_res.municipios[0]
    mun_id = make_municipio_id(m.codigo_ibge)
    fontes.append("IBGE/Cadastro Nacional (5.571 municípios)")

    store.upsert_node(
        GraphNode(
            node_id=mun_id,
            tipo="municipio",
            chave_canonica=m.codigo_ibge,
            nome=f"{m.nome} ({m.uf})",
            uf=m.uf,
            codigo_ibge=m.codigo_ibge,
            metadata={
                "cnpj": normalize_cnpj(m.cnpj),
                "latitude": m.latitude,
                "longitude": m.longitude,
                "populacao": m.populacao,
            },
        )
    )

    # 2. Coleta paralela: portal municipal (servidores, contratos, despesas) + Transferegov emendas
    ano_int = ano or 0

    async def _collect_phase1() -> tuple[Any, Any, Any, list[dict[str, Any]]]:
        r_serv, r_ct, r_desp, r_em = await asyncio.gather(
            mun_client.consultar_servidores_municipio(
                m.codigo_ibge, uf=m.uf, ano=ano_int, limit=100
            ),
            mun_client.consultar_contratos_municipio(
                m.codigo_ibge, uf=m.uf, ano=ano_int, limit=50
            ),
            mun_client.consultar_despesas_municipio(
                m.codigo_ibge, uf=m.uf, ano=ano_int, limit=50, ordem_valor_desc=True
            ),
            fetch_emendas_municipio(
                store, m.nome, m.uf, ano=ano, forcar_atualizacao=forcar_atualizacao
            ),
            return_exceptions=True,
        )
        return (
            None if isinstance(r_serv, BaseException) else r_serv,
            None if isinstance(r_ct, BaseException) else r_ct,
            None if isinstance(r_desp, BaseException) else r_desp,
            [] if isinstance(r_em, BaseException) else r_em,
        )

    try:
        serv_res, ct_res, desp_res, emendas_list = await asyncio.wait_for(
            _collect_phase1(),
            timeout=max(10.0, timeout_seconds * 0.55),
        )
    except asyncio.TimeoutError:
        serv_res, ct_res, desp_res, emendas_list = None, None, None, []

    # Persiste servidores, contratos e despesas via ingest_harvest_record (reuso total!)
    harvest_payload: dict[str, Any] = {
        "codigo_ibge": m.codigo_ibge,
        "nome": m.nome,
        "uf": m.uf,
        "cnpj": m.cnpj,
        "latitude": m.latitude,
        "longitude": m.longitude,
        "populacao": m.populacao,
        "servidores": [],
        "contratos": [],
        "despesas": [],
    }
    if serv_res and serv_res.disponivel:
        harvest_payload["portal_url"] = serv_res.portal_url
        harvest_payload["plataforma"] = serv_res.plataforma
        harvest_payload["servidores"] = [s.model_dump() for s in serv_res.servidores]
        fontes.append(f"Folha Municipal ({serv_res.fonte})")
    if ct_res and ct_res.disponivel:
        harvest_payload.setdefault("portal_url", ct_res.portal_url)
        harvest_payload["contratos"] = [c.model_dump() for c in ct_res.contratos]
        fontes.append(f"Contratos ({ct_res.fonte})")
    if desp_res and desp_res.disponivel:
        harvest_payload.setdefault("portal_url", desp_res.portal_url)
        harvest_payload["despesas"] = [d.model_dump() for d in desp_res.despesas]
        fontes.append(f"Despesas ({desp_res.fonte})")

    store.ingest_harvest_record(harvest_payload)

    # Persiste emendas parlamentares (Transferegov)
    if emendas_list:
        fontes.append("TransfereGov (Emendas Pix / Especiais)")
        for em in emendas_list:
            autor = str(em.get("nome_parlamentar") or "").strip()
            pol_id = make_politico_id(autor, m.uf)
            if not pol_id:
                continue
            val = float(em.get("valor_custeio") or 0.0) + float(
                em.get("valor_investimento") or 0.0
            )
            store.upsert_node(
                GraphNode(
                    node_id=pol_id,
                    tipo="politico",
                    chave_canonica=normalize_name(autor),
                    nome=autor,
                    uf=m.uf,
                    metadata={"papel": "Autor de Emenda Parlamentar"},
                )
            )
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id(
                        "edge",
                        pol_id,
                        "DESTINOU_EMENDA",
                        mun_id,
                        str(em.get("codigo_plano_acao") or em.get("numero_emenda") or ""),
                    ),
                    src_id=pol_id,
                    dst_id=mun_id,
                    tipo_relacao="DESTINOU_EMENDA",
                    valor=val,
                    data_ref=str(em.get("ano_emenda") or em.get("ano") or ""),
                    fonte="TransfereGov (Emendas Especiais)",
                    url_evidencia=_TRANSFEREGOV_EMENDA_URL,
                    metadata={
                        "numero_emenda": em.get("numero_emenda") or "",
                        "area": em.get("area_politica_publica") or "",
                        "situacao": em.get("situacao") or "",
                    },
                )
            )

    # 3. Seleciona top-N fornecedores (CNPJs) por volume e enriquece em paralelo (BrasilAPI + TCU)
    cnpj_totals: dict[str, float] = defaultdict(float)
    for ct in harvest_payload["contratos"]:
        c = normalize_cnpj(ct.get("documento_fornecedor"))
        if c:
            cnpj_totals[c] += float(ct.get("valor") or 0.0)
    for desp in harvest_payload["despesas"]:
        c = normalize_cnpj(desp.get("documento_fornecedor"))
        if c:
            cnpj_totals[c] += float(desp.get("valor_pago") or desp.get("valor_empenhado") or 0.0)

    # Também inclui CNPJs já ligados a este município no DuckDB
    existing_nodes, existing_edges = store.query_neighborhood(mun_id, max_hops=1, max_nodes=200)
    existing_map = {n.node_id: n for n in existing_nodes}
    for e in existing_edges:
        if e.src_id == mun_id and e.dst_id.startswith("empresa:"):
            enode = existing_map.get(e.dst_id)
            if enode and normalize_cnpj(enode.chave_canonica):
                cnpj_totals[enode.chave_canonica] += e.valor

    top_cnpjs = [
        c
        for c, _ in sorted(cnpj_totals.items(), key=lambda kv: -kv[1])[
            : max(1, min(max_fornecedores, 25))
        ]
    ]

    sem = asyncio.Semaphore(DEFAULT_MAX_CONCURRENCY)

    async def _enrich_one(
        cnpj: str,
    ) -> tuple[str, dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None]:
        async with sem:
            c_data, t_data, f_data = await asyncio.gather(
                fetch_cnpj_qsa(store, cnpj, forcar_atualizacao=forcar_atualizacao),
                fetch_tcu_certidoes(store, cnpj, forcar_atualizacao=forcar_atualizacao),
                fetch_federal_sancoes_e_contratos(
                    store, cnpj, forcar_atualizacao=forcar_atualizacao
                ),
                return_exceptions=True,
            )
            return (
                cnpj,
                c_data if isinstance(c_data, dict) else None,
                t_data if isinstance(t_data, dict) else None,
                f_data if isinstance(f_data, dict) else None,
            )

    if top_cnpjs:
        rem_timeout = max(5.0, timeout_seconds - (time.monotonic() - t0))
        try:
            enriched = await asyncio.wait_for(
                asyncio.gather(*(_enrich_one(c) for c in top_cnpjs)),
                timeout=rem_timeout,
            )
        except asyncio.TimeoutError:
            enriched = []

        got_br = False
        got_tcu = False
        got_fed = False
        for cnpj, c_data, t_data, f_data in enriched:
            if c_data:
                got_br = True
            if t_data:
                got_tcu = True
            if f_data and f_data.get("disponivel"):
                got_fed = True
            _ingest_cnpj_enrichment(store, cnpj, c_data, t_data, f_data)
        if got_br:
            fontes.append("BrasilAPI (CNPJ/QSA Receita Federal)")
        if got_tcu:
            fontes.append("TCU Certidão Consolidada (TCU/CNJ/CEIS/CNEP)")
        if got_fed:
            fontes.append("Portal da Transparência Federal")

    # 4. Monta subgrafo (2 saltos: município -> empresas/emendas/servidores -> sócios/sanções)
    sub_nodes, sub_edges = store.query_neighborhood(mun_id, max_hops=2, max_nodes=250)
    scope_ids = {n.node_id for n in sub_nodes}
    alerts = run_all_detectors(store, scope_node_ids=scope_ids, persist=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return DossierResult(
        alvo_id=mun_id,
        alvo_tipo="municipio",
        alvo_nome=f"{m.nome} ({m.uf})",
        resumo=(
            f"Foram analisados **{len(top_cnpjs)}** fornecedores prioritários (CNPJ/QSA/TCU), "
            f"**{len(harvest_payload['servidores'])}** vínculos de servidores e "
            f"**{len(emendas_list)}** emenda(s) parlamentar(es) em {m.nome}/{m.uf}."
        ),
        nodes=sub_nodes,
        edges=sub_edges,
        alerts=alerts,
        fontes_consultadas=list(dict.fromkeys(fontes)),
        fontes_opcionais_ausentes=ausentes,
        estatisticas={
            "fornecedores_enriquecidos": len(top_cnpjs),
            "servidores_coletados": len(harvest_payload["servidores"]),
            "contratos_coletados": len(harvest_payload["contratos"]),
            "despesas_coletadas": len(harvest_payload["despesas"]),
            "emendas_coletadas": len(emendas_list),
        },
        tempo_ms=elapsed_ms,
    )


async def investigar_fornecedor_360(
    store: GraphStore,
    cnpj: str,
    forcar_atualizacao: bool = False,
    timeout_seconds: float = DEFAULT_ONDEMAND_TIMEOUT_SECONDS,
) -> DossierResult:
    """Raio-X 360° de fornecedor (CNPJ): QSA, sanções TCU/CEIS/CNEP, contratos e sócios."""
    t0 = time.monotonic()
    norm = normalize_cnpj(cnpj)
    if not norm:
        raise ValueError(f"CNPJ inválido: '{cnpj}'. Informe 14 dígitos.")

    fontes: list[str] = []
    ausentes: list[str] = []
    if not has_federal_transparencia_key():
        ausentes.append("Portal da Transparência Federal (TRANSPARENCIA_API_KEY não configurada)")

    try:
        c_data, t_data, f_data = await asyncio.wait_for(
            asyncio.gather(
                fetch_cnpj_qsa(store, norm, forcar_atualizacao=forcar_atualizacao),
                fetch_tcu_certidoes(store, norm, forcar_atualizacao=forcar_atualizacao),
                fetch_federal_sancoes_e_contratos(
                    store, norm, forcar_atualizacao=forcar_atualizacao
                ),
                return_exceptions=True,
            ),
            timeout=timeout_seconds,
        )
    except asyncio.TimeoutError:
        c_data, t_data, f_data = None, None, None

    c_dict = c_data if isinstance(c_data, dict) else None
    t_dict = t_data if isinstance(t_data, dict) else None
    f_dict = f_data if isinstance(f_data, dict) else None

    if c_dict:
        fontes.append("BrasilAPI (CNPJ/QSA Receita Federal)")
    if t_dict:
        fontes.append("TCU Certidão Consolidada (TCU/CNJ/CEIS/CNEP)")
    if f_dict and f_dict.get("disponivel"):
        fontes.append("Portal da Transparência Federal")

    eid, socio_ids = _ingest_cnpj_enrichment(store, norm, c_dict, t_dict, f_dict)
    emp_node = store.get_node(eid)
    emp_nome = emp_node.nome if emp_node else norm

    # Consulta TSE para os primeiros 3 sócios pessoas físicas (enriquecimento eleitoral leve)
    uf_sede = emp_node.uf if emp_node else ""
    mun_sede = str(emp_node.metadata.get("municipio") or "") if emp_node else ""
    for sid in socio_ids[:3]:
        if not sid.startswith("pessoa:"):
            continue
        snode = store.get_node(sid)
        if not snode:
            continue
        cands = await fetch_tse_candidato(
            store,
            snode.nome,
            uf=uf_sede or None,
            municipio_nome=mun_sede or None,
            forcar_atualizacao=forcar_atualizacao,
        )
        if cands:
            if "TSE (Candidaturas)" not in fontes:
                fontes.append("TSE (Candidaturas)")
            for c in cands[:3]:
                c_uf = str(c.get("uf") or uf_sede).upper()
                pol_id = make_politico_id(str(c.get("nome") or snode.nome), c_uf)
                if not pol_id:
                    continue
                store.upsert_node(
                    GraphNode(
                        node_id=pol_id,
                        tipo="politico",
                        chave_canonica=normalize_name(str(c.get("nome") or snode.nome)),
                        nome=str(c.get("nome") or snode.nome),
                        uf=c_uf,
                        metadata={
                            "cargo": c.get("cargo") or "",
                            "partido": c.get("partido") or "",
                        },
                    )
                )
                store.upsert_edge(
                    GraphEdge(
                        edge_id=make_stable_id(
                            "edge", sid, "CANDIDATOU_EM", pol_id, str(c.get("ano") or "2024")
                        ),
                        src_id=sid,
                        dst_id=pol_id,
                        tipo_relacao="CANDIDATOU_EM",
                        data_ref=str(c.get("ano") or "2024"),
                        fonte="TSE DivulgaCandContas",
                        url_evidencia="https://divulgacandcontas.tse.jus.br/",
                        metadata={
                            "uf": c_uf,
                            "cargo": c.get("cargo") or "",
                            "partido": c.get("partido") or "",
                        },
                    )
                )

    sub_nodes, sub_edges = store.query_neighborhood(eid, max_hops=2, max_nodes=200)
    scope_ids = {n.node_id for n in sub_nodes}
    alerts = run_all_detectors(store, scope_node_ids=scope_ids, persist=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return DossierResult(
        alvo_id=eid,
        alvo_tipo="empresa",
        alvo_nome=emp_nome,
        resumo=(
            f"CNPJ `{norm}` enriquecido com **{len(socio_ids)}** sócio(s) no QSA e "
            f"**{len(sub_edges)}** conexão(ões) mapeadas no grafo DuckDB."
        ),
        nodes=sub_nodes,
        edges=sub_edges,
        alerts=alerts,
        fontes_consultadas=list(dict.fromkeys(fontes)),
        fontes_opcionais_ausentes=ausentes,
        estatisticas={"cnpj": norm, "socios_qsa": len(socio_ids)},
        tempo_ms=elapsed_ms,
    )


async def investigar_pessoa_360(
    store: GraphStore,
    nome: str,
    uf: str | None = None,
    ano_eleicao: int = 2024,
    forcar_atualizacao: bool = False,
) -> DossierResult:
    """Mapeia vínculos públicos, societários (QSA) e eleitorais (TSE) de uma pessoa (LGPD-safe)."""
    t0 = time.monotonic()
    pid = make_pessoa_id(nome)
    if not pid:
        raise ValueError("Informe um nome válido para investigação.")

    fontes = ["Grafo Local DuckDB (Vínculos Municipais e QSA)"]
    ausentes: list[str] = []
    if not has_federal_transparencia_key():
        ausentes.append("Portal da Transparência Federal (TRANSPARENCIA_API_KEY não configurada)")

    store.upsert_node(
        GraphNode(
            node_id=pid,
            tipo="pessoa",
            chave_canonica=normalize_name(nome),
            nome=nome.strip(),
            uf=(uf or "").upper(),
        )
    )

    cands = await fetch_tse_candidato(
        store, nome, uf=uf, ano=ano_eleicao, forcar_atualizacao=forcar_atualizacao
    )
    if cands:
        fontes.append("TSE DivulgaCandContas")
        for c in cands[:5]:
            c_uf = str(c.get("uf") or uf or "").upper()
            pol_id = make_politico_id(str(c.get("nome") or nome), c_uf)
            if not pol_id:
                continue
            store.upsert_node(
                GraphNode(
                    node_id=pol_id,
                    tipo="politico",
                    chave_canonica=normalize_name(str(c.get("nome") or nome)),
                    nome=str(c.get("nome") or nome),
                    uf=c_uf,
                    metadata={"cargo": c.get("cargo") or "", "partido": c.get("partido") or ""},
                )
            )
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id("edge", pid, "CANDIDATOU_EM", pol_id, str(ano_eleicao)),
                    src_id=pid,
                    dst_id=pol_id,
                    tipo_relacao="CANDIDATOU_EM",
                    data_ref=str(ano_eleicao),
                    fonte="TSE DivulgaCandContas",
                    url_evidencia="https://divulgacandcontas.tse.jus.br/",
                    metadata={
                        "uf": c_uf,
                        "cargo": c.get("cargo") or "",
                        "partido": c.get("partido") or "",
                    },
                )
            )

    sub_nodes, sub_edges = store.query_neighborhood(pid, max_hops=2, max_nodes=150)
    scope_ids = {n.node_id for n in sub_nodes}
    alerts = run_all_detectors(store, scope_node_ids=scope_ids, persist=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return DossierResult(
        alvo_id=pid,
        alvo_tipo="pessoa",
        alvo_nome=nome.strip(),
        resumo=(
            f"Mapeados **{len(sub_edges)}** vínculo(s) (serviço público, QSA e/ou TSE) "
            f"e **{len(alerts)}** indício(s) explicável(is) para `{nome.strip()}`."
        ),
        nodes=sub_nodes,
        edges=sub_edges,
        alerts=alerts,
        fontes_consultadas=list(dict.fromkeys(fontes)),
        fontes_opcionais_ausentes=ausentes,
        estatisticas={"candidaturas_tse": len(cands)},
        tempo_ms=elapsed_ms,
    )


async def seguir_emenda_parlamentar(
    store: GraphStore,
    autor_ou_municipio: str,
    uf: str | None = None,
    ano: int | None = None,
    max_municipios: int = 3,
    forcar_atualizacao: bool = False,
) -> DossierResult:
    """Rastreia recurso: Parlamentar -> Emenda Pix (TransfereGov) -> Município -> Contratos."""
    t0 = time.monotonic()
    fontes: list[str] = ["TransfereGov (Emendas Especiais)"]
    ausentes: list[str] = []
    if not has_federal_transparencia_key():
        ausentes.append("Portal da Transparência Federal (TRANSPARENCIA_API_KEY não configurada)")

    emendas = await fetch_emendas_autor(
        store, autor_ou_municipio, ano=ano, forcar_atualizacao=forcar_atualizacao
    )
    is_autor_mode = bool(emendas)
    if not emendas:
        emendas = await fetch_emendas_municipio(
            store, autor_ou_municipio, uf=uf or "", ano=ano, forcar_atualizacao=forcar_atualizacao
        )

    inferred_uf = (uf or "").strip().upper()
    if is_autor_mode and not inferred_uf:
        ufs = {
            str(e.get("uf_beneficiario") or "").strip().upper()
            for e in emendas
            if e.get("uf_beneficiario")
        }
        if len(ufs) == 1:
            inferred_uf = next(iter(ufs))

    root_id = (
        make_politico_id(autor_ou_municipio, inferred_uf)
        if is_autor_mode
        else make_stable_id("busca_emenda", autor_ou_municipio)
    )
    if is_autor_mode:
        store.upsert_node(
            GraphNode(
                node_id=root_id,
                tipo="politico",
                chave_canonica=normalize_name(autor_ou_municipio),
                nome=autor_ou_municipio.strip(),
                uf=inferred_uf,
                metadata={"papel": "Autor de Emenda Parlamentar"},
            )
        )

    # Resolve municípios beneficiários e persiste arestas DESTINOU_EMENDA
    beneficiarios_valor: dict[tuple[str, str], float] = defaultdict(float)
    for em in emendas:
        benef = str(em.get("nome_beneficiario") or "").strip()
        b_uf = str(em.get("uf_beneficiario") or uf or "").strip().upper()
        autor = str(em.get("nome_parlamentar") or autor_ou_municipio).strip()
        val = float(em.get("valor_custeio") or 0.0) + float(em.get("valor_investimento") or 0.0)
        if benef:
            beneficiarios_valor[(benef, b_uf)] += val
        if not benef:
            continue
        mun_search = await mun_client.buscar_municipios(benef, uf=b_uf or None, limit=1)
        if not mun_search.municipios:
            continue
        m = mun_search.municipios[0]
        mun_id = make_municipio_id(m.codigo_ibge)
        if not is_autor_mode:
            root_id = mun_id
        store.upsert_node(
            GraphNode(
                node_id=mun_id,
                tipo="municipio",
                chave_canonica=m.codigo_ibge,
                nome=f"{m.nome} ({m.uf})",
                uf=m.uf,
                codigo_ibge=m.codigo_ibge,
                metadata={
                    "latitude": m.latitude,
                    "longitude": m.longitude,
                    "populacao": m.populacao,
                },
            )
        )
        pol_id = (
            root_id
            if (is_autor_mode and normalize_name(autor) == normalize_name(autor_ou_municipio))
            else make_politico_id(autor, m.uf)
        )
        if pol_id:
            store.upsert_node(
                GraphNode(
                    node_id=pol_id,
                    tipo="politico",
                    chave_canonica=normalize_name(autor),
                    nome=autor,
                    uf=inferred_uf or m.uf,
                )
            )
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id(
                        "edge",
                        pol_id,
                        "DESTINOU_EMENDA",
                        mun_id,
                        str(em.get("codigo_plano_acao") or em.get("numero_emenda") or ""),
                    ),
                    src_id=pol_id,
                    dst_id=mun_id,
                    tipo_relacao="DESTINOU_EMENDA",
                    valor=val,
                    data_ref=str(em.get("ano_emenda") or em.get("ano") or ""),
                    fonte="TransfereGov (Emendas Especiais)",
                    url_evidencia=_TRANSFEREGOV_EMENDA_URL,
                )
            )

    # Enriquece os top municípios beneficiários (contratos + top 5 fornecedores)
    top_benefs = sorted(beneficiarios_valor.items(), key=lambda kv: -kv[1])[
        : max(1, min(max_municipios, 5))
    ]
    for (b_nome, b_uf), _ in top_benefs:
        try:
            sub_res = await investigar_municipio_360(
                store,
                b_nome,
                uf=b_uf or None,
                ano=ano,
                max_fornecedores=5,
                forcar_atualizacao=forcar_atualizacao,
                timeout_seconds=18.0,
            )
            for f in sub_res.fontes_consultadas:
                if f not in fontes:
                    fontes.append(f)
        except Exception as exc:
            logger.debug(
                "Enriquecimento de município beneficiário %s/%s falhou: %s", b_nome, b_uf, exc
            )

    sub_nodes, sub_edges = store.query_neighborhood(root_id, max_hops=3, max_nodes=220)
    scope_ids = {n.node_id for n in sub_nodes}
    alerts = run_all_detectors(store, scope_node_ids=scope_ids, persist=True)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return DossierResult(
        alvo_id=root_id,
        alvo_tipo="politico" if is_autor_mode else "municipio",
        alvo_nome=autor_ou_municipio.strip(),
        resumo=(
            f"Rastreadas **{len(emendas)}** emenda(s) parlamentar(es) em "
            f"**{len(beneficiarios_valor)}** município(s) beneficiário(s), com cruzamento de "
            "contratos, QSA e indícios."
        ),
        nodes=sub_nodes,
        edges=sub_edges,
        alerts=alerts,
        fontes_consultadas=list(dict.fromkeys(fontes)),
        fontes_opcionais_ausentes=ausentes,
        estatisticas={
            "emendas": len(emendas),
            "municipios_beneficiarios": len(beneficiarios_valor),
        },
        tempo_ms=elapsed_ms,
    )
