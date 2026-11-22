"""Tests for DuckDB GraphStore, entity resolution, TTL cache, BFS paths, and harvest ingestion."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_brasil.data.grafo_transparencia.schemas import GraphEdge, GraphNode
from mcp_brasil.data.grafo_transparencia.store import (
    GraphStore,
    compute_person_match_score,
    haversine_km,
    make_empresa_id,
    make_municipio_id,
    make_pessoa_id,
    make_politico_id,
    make_stable_id,
    mask_cpf,
    normalize_cnpj,
)


def test_mask_cpf_and_normalize_cnpj() -> None:
    assert mask_cpf("123.456.789-01") == "***.456.789-**"
    assert mask_cpf("12345678901") == "***.456.789-**"
    assert mask_cpf("***.456.789-**") == "***.456.789-**"
    # CNPJs remain unmasked (public legal entity identifier)
    assert mask_cpf("11222333000144") == "11.222.333/0001-44"
    assert normalize_cnpj("11.222.333/0001-44") == "11222333000144"
    assert normalize_cnpj("123") == ""


def test_haversine_and_person_match_score() -> None:
    # São Paulo -> Rio de Janeiro (~357 km)
    dist = haversine_km(-23.5505, -46.6333, -22.9068, -43.1729)
    assert 340.0 <= dist <= 375.0

    score_match, reasons = compute_person_match_score(
        "CLODOVIL ZAMBONI KOWALSKI",
        "123.456.789-01",
        "999.456.789-88",
        cargo_a="Médico Clínico Geral",
        cargo_b="Médico Pediatra",
        uf_a="MG",
        uf_b="MG",
        dist_km=25.0,
    )
    assert score_match >= 90.0
    assert any("CPF parcial coincidente" in r for r in reasons)

    score_mismatch, _ = compute_person_match_score(
        "MARIA SILVA",
        "111.222.333-44",
        "111.999.888-44",
    )
    assert score_mismatch < 40.0


def test_store_upsert_ttl_cache_and_bfs_paths(tmp_path: Path) -> None:
    db_file = tmp_path / "grafo.duckdb"
    with GraphStore(db_file) as store:
        # TTL cache hit & expiration
        store.set_cache("k1", "brasilapi", {"razao_social": "ACME LTDA"}, ttl_seconds=3600)
        assert store.get_cache("k1") == {"razao_social": "ACME LTDA"}

        # Force expire in DuckDB
        old = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=2)
        store.conn.execute(
            "UPDATE cache_fetch SET fetched_at = ?, ttl_seconds = 60 WHERE cache_key = 'k1'", [old]
        )
        assert store.get_cache("k1") is None

        # Build 3-hop chain: Politico -> Municipio -> Empresa -> Pessoa (Sócio)
        pol = make_politico_id("DEP CARLOS ALMEIDA", "MG")
        mun = make_municipio_id("3157807")
        emp = make_empresa_id("11.222.333/0001-44")
        pes = make_pessoa_id("ROBERTO FERRAZ KOWALSKI")

        store.upsert_node(
            GraphNode(
                node_id=pol,
                tipo="politico",
                chave_canonica="DEP CARLOS ALMEIDA",
                nome="Dep. Carlos Almeida",
                uf="MG",
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=mun,
                tipo="municipio",
                chave_canonica="3157807",
                nome="Santa Luzia (MG)",
                uf="MG",
                codigo_ibge="3157807",
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=emp,
                tipo="empresa",
                chave_canonica="11222333000144",
                nome="Construtora Alfa LTDA",
                uf="MG",
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=pes,
                tipo="pessoa",
                chave_canonica="ROBERTO FERRAZ KOWALSKI",
                nome="Roberto Ferraz Kowalski",
                uf="MG",
            )
        )

        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", pol, mun),
                src_id=pol,
                dst_id=mun,
                tipo_relacao="DESTINOU_EMENDA",
                valor=500_000.0,
                fonte="TransfereGov",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", mun, emp),
                src_id=mun,
                dst_id=emp,
                tipo_relacao="CONTRATOU",
                valor=480_000.0,
                fonte="Portal Municipal",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", pes, emp),
                src_id=pes,
                dst_id=emp,
                tipo_relacao="SOCIO_DE",
                fonte="BrasilAPI",
            )
        )

        paths = store.find_shortest_paths(pol, pes, max_hops=4)
        assert len(paths) == 1
        assert paths[0].saltos == 3
        assert [s.tipo_relacao for s in paths[0].passos] == [
            "DESTINOU_EMENDA",
            "CONTRATOU",
            "SOCIO_DE",
        ]

        # find_nodes by IBGE, CNPJ and name
        assert store.find_nodes("3157807")[0].node_id == mun
        assert store.find_nodes("11.222.333/0001-44")[0].node_id == emp
        assert store.find_nodes("Kowalski")[0].node_id == pes

        nodes, edges = store.query_neighborhood(mun, max_hops=2)
        assert len(nodes) == 4
        assert len(edges) == 3


def test_import_harvest_ndjson(tmp_path: Path) -> None:
    ndjson = tmp_path / "harvest.ndjson"
    record = {
        "codigo_ibge": "3157807",
        "nome": "Santa Luzia",
        "uf": "MG",
        "latitude": -19.7697,
        "longitude": -43.8514,
        "populacao": 219132,
        "plataforma": "cms_wordpress_html",
        "portal_url": "https://www.santaluzia.mg.gov.br/",
        "servidores": [
            {
                "nome": "JOAO CARLOS ZAMBONI",
                "cpf_parcial": "123.456.789-01",
                "cargo": "Engenheiro Civil",
                "salario_bruto": 9500.0,
                "competencia": "03/2025",
            }
        ],
        "contratos": [
            {
                "numero": "CT-01/2025",
                "ano": 2025,
                "data_assinatura": "2025-02-10",
                "fornecedor": "CONSTRUTORA ZAMBONI LTDA",
                "documento_fornecedor": "22.333.444/0001-55",
                "objeto": "Pavimentação urbana",
                "valor": 850000.0,
            }
        ],
    }
    ndjson.write_text(json.dumps(record) + "\nnot-json\n", encoding="utf-8")

    with GraphStore(":memory:") as store:
        summary = store.import_harvest_ndjson(ndjson)
        assert summary.municipios_lidos == 1
        assert summary.erros_leitura == 1
        assert summary.nodes_inseridos >= 3
        assert summary.edges_inseridas == 2
        st = store.stats()
        assert st["nodes_por_tipo"]["municipio"] == 1
        assert st["nodes_por_tipo"]["empresa"] == 1
        assert st["nodes_por_tipo"]["pessoa"] == 1
        # Verifica mascaramento LGPD automático
        p = store.get_node(make_pessoa_id("JOAO CARLOS ZAMBONI"))
        assert p is not None
        assert p.metadata["cpf_parcial"] == "***.456.789-**"
