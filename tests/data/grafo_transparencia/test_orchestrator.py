"""Mocked E2E tests for On-Demand 360° orchestrator and graceful degradation without API keys."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from mcp_brasil.data.grafo_transparencia import orchestrator
from mcp_brasil.data.grafo_transparencia.store import GraphStore
from mcp_brasil.data.transparencia_municipios.schemas import (
    ContratoItem,
    ContratosResult,
    DespesaItem,
    DespesasResult,
    Municipio,
    SearchResult,
    ServidoresResult,
    ServidorItem,
)


@pytest.mark.asyncio
async def test_investigar_municipio_360_end_to_end_graceful_without_federal_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)

    mock_mun = Municipio(
        codigo_ibge="3157807",
        nome="Santa Luzia",
        uf="MG",
        cnpj="18715409000150",
        latitude=-19.7697,
        longitude=-43.8514,
        capital=False,
        codigo_uf=31,
        regiao="Sudeste",
        siafi_id="5155",
        ddd="31",
        fuso_horario="America/Sao_Paulo",
        populacao=219132,
        slug="santa-luzia-mg",
    )
    monkeypatch.setattr(
        orchestrator.mun_client,
        "buscar_municipios",
        AsyncMock(
            return_value=SearchResult(
                query="Santa Luzia", uf="MG", total_encontrado=1, municipios=[mock_mun]
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator.mun_client,
        "consultar_servidores_municipio",
        AsyncMock(
            return_value=ServidoresResult(
                municipio=mock_mun,
                fonte="Portal Municipal HTML",
                plataforma="cms_wordpress_html",
                portal_url="https://www.santaluzia.mg.gov.br/",
                total_registros=1,
                disponivel=True,
                servidores=[
                    ServidorItem(
                        nome="CARLOS ALBERTO ZAMBONI",
                        cpf_parcial="***.456.789-**",
                        cargo="Diretor de Obras",
                        salario_bruto=11500.0,
                        competencia="03/2025",
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator.mun_client,
        "consultar_contratos_municipio",
        AsyncMock(
            return_value=ContratosResult(
                municipio=mock_mun,
                fonte="PNCP",
                plataforma="pncp",
                portal_url="https://pncp.gov.br/",
                total_registros=1,
                disponivel=True,
                contratos=[
                    ContratoItem(
                        numero="CT-10/2025",
                        ano=2025,
                        fornecedor="ZAMBONI ENGENHARIA LTDA",
                        documento_fornecedor="12.345.678/0001-99",
                        objeto="Reforma de escolas municipais",
                        data_assinatura="2025-02-15",
                        valor=600000.0,
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator.mun_client,
        "consultar_despesas_municipio",
        AsyncMock(
            return_value=DespesasResult(
                municipio=mock_mun,
                fonte="Portal Municipal",
                plataforma="html_portal",
                portal_url="https://www.santaluzia.mg.gov.br/",
                total_registros=1,
                disponivel=True,
                despesas=[
                    DespesaItem(
                        numero_empenho="EMP-01",
                        ano=2025,
                        fornecedor="ZAMBONI ENGENHARIA LTDA",
                        documento_fornecedor="12.345.678/0001-99",
                        valor_pago=250000.0,
                    )
                ],
            )
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_emendas_municipio",
        AsyncMock(
            return_value=[
                {
                    "codigo_plano_acao": "PA-2025-1",
                    "nome_parlamentar": "DEP JOAO PAULO",
                    "numero_emenda": "12340001",
                    "ano_emenda": "2025",
                    "valor_custeio": 300000.0,
                    "valor_investimento": 200000.0,
                    "uf_beneficiario": "MG",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_cnpj_qsa",
        AsyncMock(
            return_value={
                "cnpj": "12345678000199",
                "razao_social": "ZAMBONI ENGENHARIA LTDA",
                "descricao_situacao_cadastral": "ATIVA",
                "data_inicio_atividade": "2025-01-05",
                "capital_social": 20000.0,
                "uf": "MG",
                "qsa": [
                    {
                        "nome_socio": "CARLOS ALBERTO ZAMBONI",
                        "qualificacao_socio": "Sócio-Administrador",
                        "cnpj_cpf_do_socio": "***456789**",
                    }
                ],
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_tcu_certidoes",
        AsyncMock(
            return_value={
                "cnpj": "12345678000199",
                "certidoes": [
                    {
                        "emissor": "CGU",
                        "tipo": "CEIS",
                        "situacao": "CONSTAM",
                        "data_hora_emissao": "2025-03-01",
                    }
                ],
            }
        ),
    )

    with GraphStore(":memory:") as store:
        dossier = await orchestrator.investigar_municipio_360(
            store=store,
            municipio="Santa Luzia",
            uf="MG",
            ano=2025,
            max_fornecedores=5,
        )
        assert dossier.alvo_id == "municipio:3157807"
        assert any("TRANSPARENCIA_API_KEY" in s for s in dossier.fontes_opcionais_ausentes)
        assert "BrasilAPI (CNPJ/QSA Receita Federal)" in dossier.fontes_consultadas
        assert "TCU Certidão Consolidada (TCU/CNJ/CEIS/CNEP)" in dossier.fontes_consultadas
        rules = {a.regra for a in dossier.alerts}
        assert "CONFLITO_SOCIO_SERVIDOR" in rules
        assert "EMPRESA_SANCIONADA" in rules
        assert "EMPRESA_RECEM_CRIADA" in rules
        assert "CAPITAL_SOCIAL_DESPROPORCIONAL" in rules


@pytest.mark.asyncio
async def test_investigar_fornecedor_and_pessoa_360(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)

    monkeypatch.setattr(
        orchestrator,
        "fetch_cnpj_qsa",
        AsyncMock(
            return_value={
                "cnpj": "99888777000166",
                "razao_social": "OMEGA CONSULTORIA LTDA",
                "descricao_situacao_cadastral": "ATIVA",
                "uf": "SP",
                "qsa": [{"nome_socio": "ANA PAULA MENDONCA", "qualificacao_socio": "Sócia"}],
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_tcu_certidoes",
        AsyncMock(
            return_value={
                "cnpj": "99888777000166",
                "certidoes": [{"emissor": "TCU", "tipo": "Inidôneos", "situacao": "NADA CONSTA"}],
            }
        ),
    )
    monkeypatch.setattr(
        orchestrator,
        "fetch_tse_candidato",
        AsyncMock(
            return_value=[
                {
                    "nome": "ANA PAULA MENDONCA",
                    "uf": "SP",
                    "cargo": "Vereador",
                    "partido": "XYZ",
                    "ano": 2024,
                }
            ]
        ),
    )

    with GraphStore(":memory:") as store:
        forn_dossier = await orchestrator.investigar_fornecedor_360(store, "99.888.777/0001-66")
        assert forn_dossier.alvo_id == "empresa:99888777000166"
        assert forn_dossier.estatisticas["socios_qsa"] == 1
        assert any(e.tipo_relacao == "SOCIO_DE" for e in forn_dossier.edges)
        assert any(e.tipo_relacao == "CANDIDATOU_EM" for e in forn_dossier.edges)

        pes_dossier = await orchestrator.investigar_pessoa_360(
            store, "ANA PAULA MENDONCA", uf="SP"
        )
        assert pes_dossier.alvo_id == "pessoa:ANA PAULA MENDONCA"
        assert len(pes_dossier.edges) >= 2


@pytest.mark.asyncio
async def test_seguir_emenda_parlamentar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRANSPARENCIA_API_KEY", raising=False)
    mock_mun = Municipio(
        codigo_ibge="3157807",
        nome="Santa Luzia",
        uf="MG",
        latitude=-19.7697,
        longitude=-43.8514,
        capital=False,
        codigo_uf=31,
        regiao="Sudeste",
        siafi_id="5155",
        ddd="31",
        fuso_horario="America/Sao_Paulo",
        populacao=219132,
        slug="santa-luzia-mg",
    )

    async def fake_inv_mun(store: GraphStore, municipio: str, **kwargs: Any) -> Any:
        return orchestrator.DossierResult(
            alvo_id="municipio:3157807",
            alvo_tipo="municipio",
            alvo_nome="Santa Luzia (MG)",
            fontes_consultadas=["Portal Municipal"],
        )

    monkeypatch.setattr(
        orchestrator,
        "fetch_emendas_autor",
        AsyncMock(
            return_value=[
                {
                    "codigo_plano_acao": "PA-99",
                    "nome_parlamentar": "SENADORA MARIA",
                    "nome_beneficiario": "Santa Luzia",
                    "uf_beneficiario": "MG",
                    "valor_custeio": 400000.0,
                    "valor_investimento": 100000.0,
                    "ano_emenda": "2024",
                }
            ]
        ),
    )
    monkeypatch.setattr(
        orchestrator.mun_client,
        "buscar_municipios",
        AsyncMock(
            return_value=SearchResult(
                query="Santa Luzia", uf="MG", total_encontrado=1, municipios=[mock_mun]
            )
        ),
    )
    monkeypatch.setattr(orchestrator, "investigar_municipio_360", fake_inv_mun)

    with GraphStore(":memory:") as store:
        res = await orchestrator.seguir_emenda_parlamentar(store, "SENADORA MARIA", ano=2024)
        assert res.alvo_tipo == "politico"
        assert res.estatisticas["emendas"] == 1
        assert any(e.tipo_relacao == "DESTINOU_EMENDA" for e in res.edges)
