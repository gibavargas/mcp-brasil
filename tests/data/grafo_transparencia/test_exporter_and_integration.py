"""Tests for exporters (Markdown/JSON/Mermaid/HTML), MCP tools, and FeatureRegistry discovery."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from mcp_brasil._shared.feature import FeatureRegistry
from mcp_brasil.data.grafo_transparencia.exporter import (
    export_graph_html,
    export_graph_json,
    export_graph_mermaid,
    format_dossier_markdown,
)
from mcp_brasil.data.grafo_transparencia.schemas import (
    DossierResult,
    GraphEdge,
    GraphNode,
    RiskAlert,
)
from mcp_brasil.data.grafo_transparencia.server import mcp as grafo_mcp
from mcp_brasil.data.grafo_transparencia.store import make_empresa_id, make_municipio_id
from mcp_brasil.data.grafo_transparencia.tools import (
    encontrar_conexoes,
    exportar_grafo,
    importar_harvest_grafo,
)


def test_exporters_markdown_json_mermaid_html() -> None:
    n1 = GraphNode(
        node_id="municipio:3157807",
        tipo="municipio",
        chave_canonica="3157807",
        nome="Santa Luzia (MG)",
        uf="MG",
    )
    n2 = GraphNode(
        node_id="empresa:11222333000144",
        tipo="empresa",
        chave_canonica="11222333000144",
        nome="ACME LTDA",
        uf="MG",
    )
    e1 = GraphEdge(
        edge_id="e1",
        src_id=n1.node_id,
        dst_id=n2.node_id,
        tipo_relacao="CONTRATOU",
        valor=150_000.0,
        fonte="PNCP",
    )
    a1 = RiskAlert(
        alert_id="a1",
        regra="EMPRESA_SANCIONADA",
        severidade="ALTA",
        score=96.0,
        entidade_id=n2.node_id,
        entidade_nome=n2.nome,
        titulo="Empresa sancionada",
        explicacao="Apontamento no CEIS.",
        evidencias=[{"url": "https://certidoes-apf.apps.tcu.gov.br/"}],
    )
    dossier = DossierResult(
        alvo_id=n1.node_id,
        alvo_tipo="municipio",
        alvo_nome=n1.nome,
        nodes=[n1, n2],
        edges=[e1],
        alerts=[a1],
        fontes_consultadas=["PNCP", "TCU"],
    )

    md = format_dossier_markdown(dossier)
    assert "# Dossiê 360° — Santa Luzia (MG)" in md
    assert "EMPRESA_SANCIONADA" in md
    assert "LGPD" in md

    js = export_graph_json([n1, n2], [e1], [a1])
    parsed = json.loads(js)
    assert len(parsed["nodes"]) == 2
    assert len(parsed["edges"]) == 1
    assert len(parsed["alerts"]) == 1

    mm = export_graph_mermaid([n1, n2], [e1])
    assert mm.startswith("flowchart LR")
    assert "CONTRATOU" in mm

    html_out = export_graph_html("Teste Grafo", [n1, n2], [e1], [a1])
    assert "<!DOCTYPE html>" in html_out
    assert '<svg id="graph">' in html_out
    assert "ACME LTDA" in html_out


def test_feature_registry_discovers_grafo_transparencia() -> None:
    registry = FeatureRegistry()
    registry.discover("mcp_brasil.data")
    assert "grafo_transparencia" in registry.features
    feat = registry.features["grafo_transparencia"]
    assert feat.meta.requires_auth is False
    assert feat.meta.name == "grafo_transparencia"


@pytest.mark.asyncio
async def test_mcp_tools_registered_with_docstrings_and_tags() -> None:
    tools = await grafo_mcp.list_tools()
    names = {t.name for t in tools}
    expected = {
        "investigar_municipio_360",
        "investigar_fornecedor_360",
        "investigar_pessoa_360",
        "seguir_emenda_parlamentar",
        "encontrar_conexoes",
        "exportar_grafo",
        "importar_harvest_grafo",
    }
    assert expected.issubset(names)
    for t in tools:
        assert t.description and len(t.description) > 20
        assert t.tags


@pytest.mark.asyncio
async def test_tools_encontrar_conexoes_exportar_e_importar_harvest(tmp_path: Path) -> None:
    db_path = tmp_path / "tools_test.duckdb"
    ndjson = tmp_path / "sample.ndjson"
    ndjson.write_text(
        json.dumps(
            {
                "codigo_ibge": "3157807",
                "nome": "Santa Luzia",
                "uf": "MG",
                "contratos": [
                    {
                        "numero": "CT-9/2025",
                        "fornecedor": "TECH PUBLICA LTDA",
                        "documento_fornecedor": "44.555.666/0001-77",
                        "valor": 220_000.0,
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    imp_out = await importar_harvest_grafo(str(ndjson), db_path=str(db_path))
    assert "Importação de Harvest Concluída" in imp_out
    assert "Municípios lidos**: 1" in imp_out

    conn_out = await encontrar_conexoes("3157807", "44.555.666/0001-77", db_path=str(db_path))
    assert "CONTRATOU" in conn_out
    assert "TECH PUBLICA LTDA" in conn_out

    html_file = tmp_path / "rede.html"
    exp_msg = await exportar_grafo(
        "3157807", formato="html", salvar_em=str(html_file), db_path=str(db_path)
    )
    assert html_file.exists()
    assert "<!DOCTYPE html>" in html_file.read_text(encoding="utf-8")
    assert "exportado em `html`" in exp_msg

    # Também valida exportação direta em Mermaid
    mm = await exportar_grafo(
        make_empresa_id("44555666000177"), formato="mermaid", db_path=str(db_path)
    )
    assert "flowchart LR" in mm
    assert make_municipio_id("3157807").replace(":", "_") in mm
