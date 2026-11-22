"""Unit tests covering all 5 explainable red-flag detectors in grafo_transparencia.rules."""

from __future__ import annotations

from mcp_brasil.data.grafo_transparencia.rules import run_all_detectors
from mcp_brasil.data.grafo_transparencia.schemas import GraphEdge, GraphNode
from mcp_brasil.data.grafo_transparencia.store import (
    GraphStore,
    make_empresa_id,
    make_municipio_id,
    make_pessoa_id,
    make_politico_id,
    make_stable_id,
)


def test_all_five_detectors_fire_with_explainable_alerts() -> None:
    with GraphStore(":memory:") as store:
        # Municípios distantes (> 350 km)
        m1 = make_municipio_id("3106200")  # BH
        m2 = make_municipio_id("3550308")  # SP
        m3 = make_municipio_id("3304557")  # RJ
        store.upsert_node(
            GraphNode(
                node_id=m1,
                tipo="municipio",
                chave_canonica="3106200",
                nome="Belo Horizonte (MG)",
                uf="MG",
                codigo_ibge="3106200",
                metadata={"latitude": -19.9167, "longitude": -43.9345},
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=m2,
                tipo="municipio",
                chave_canonica="3550308",
                nome="São Paulo (SP)",
                uf="SP",
                codigo_ibge="3550308",
                metadata={"latitude": -23.5505, "longitude": -46.6333},
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=m3,
                tipo="municipio",
                chave_canonica="3304557",
                nome="Rio de Janeiro (RJ)",
                uf="RJ",
                codigo_ibge="3304557",
                metadata={"latitude": -22.9068, "longitude": -43.1729},
            )
        )

        # Empresas 1 e 2: mesmo endereço, mesmo sócio, recém-aberta, capital baixo, sancionada
        e1 = make_empresa_id("11.111.111/0001-11")
        e2 = make_empresa_id("22.222.222/0001-22")
        store.upsert_node(
            GraphNode(
                node_id=e1,
                tipo="empresa",
                chave_canonica="11111111000111",
                nome="ALFA OBRAS LTDA",
                uf="MG",
                metadata={
                    "cnpj_raiz": "11111111",
                    "descricao_situacao_cadastral": "ATIVA",
                    "data_inicio_atividade": "2025-01-10",
                    "capital_social": 10_000.0,
                    "cep": "30110000",
                    "numero": "500",
                },
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=e2,
                tipo="empresa",
                chave_canonica="22222222000122",
                nome="BETA SERVICOS LTDA",
                uf="MG",
                metadata={
                    "cnpj_raiz": "22222222",
                    "descricao_situacao_cadastral": "INAPTA",
                    "capital_social": 5_000.0,
                    "cep": "30110000",
                    "numero": "500",
                },
            )
        )

        # Pessoa sócia das duas empresas + servidora em BH e em SP (~490 km) + doadora de político
        p1 = make_pessoa_id("FULANO ZAMBONI KOWALSKI")
        pol = make_politico_id("DEP MARCOS MOURA", "MG")
        sanc = "sancao:ceis:11111111000111"
        store.upsert_node(
            GraphNode(
                node_id=p1,
                tipo="pessoa",
                chave_canonica="FULANO ZAMBONI KOWALSKI",
                nome="Fulano Zamboni Kowalski",
                uf="MG",
                metadata={"cpf_parcial": "***.456.789-**"},
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=pol,
                tipo="politico",
                chave_canonica="DEP MARCOS MOURA",
                nome="Dep. Marcos Moura",
                uf="MG",
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=sanc, tipo="sancao", chave_canonica="CGU:CEIS", nome="CGU — CEIS (ATIVA)"
            )
        )

        # Arestas
        # Contratos (e1 em 3 municípios -> multi-municipal + recém-aberta + capital 50x)
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, e1),
                src_id=m1,
                dst_id=e1,
                tipo_relacao="CONTRATOU",
                valor=500_000.0,
                data_ref="2025-02-20",
                fonte="Portal BH",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m2, e1),
                src_id=m2,
                dst_id=e1,
                tipo_relacao="CONTRATOU",
                valor=200_000.0,
                data_ref="2025-03-01",
                fonte="Portal SP",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m3, e1),
                src_id=m3,
                dst_id=e1,
                tipo_relacao="CONTRATOU",
                valor=150_000.0,
                data_ref="2025-03-10",
                fonte="Portal RJ",
            )
        )
        # Contrato com e2 (INAPTA + mesmo sócio em BH)
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, e2),
                src_id=m1,
                dst_id=e2,
                tipo_relacao="CONTRATOU",
                valor=90_000.0,
                data_ref="2025-03-12",
                fonte="Portal BH",
            )
        )
        # QSA sócio comum
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p1, "soc", e1),
                src_id=p1,
                dst_id=e1,
                tipo_relacao="SOCIO_DE",
                fonte="BrasilAPI",
                metadata={"cpf_parcial": "***.456.789-**", "qualificacao": "Sócio-Administrador"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p1, "soc", e2),
                src_id=p1,
                dst_id=e2,
                tipo_relacao="SOCIO_DE",
                fonte="BrasilAPI",
                metadata={"cpf_parcial": "***.456.789-**"},
            )
        )
        # Folhas intermunicipais (BH + SP)
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p1, "trab", m1),
                src_id=p1,
                dst_id=m1,
                tipo_relacao="TRABALHA_EM",
                valor=12_000.0,
                fonte="Portal BH",
                metadata={"cargo": "Médico", "cpf_parcial": "***.456.789-**"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p1, "trab", m2),
                src_id=p1,
                dst_id=m2,
                tipo_relacao="TRABALHA_EM",
                valor=14_000.0,
                fonte="Portal SP",
                metadata={"cargo": "Médico", "cpf_parcial": "***.456.789-**"},
            )
        )
        # Sanção CEIS
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", e1, "sanc", sanc),
                src_id=e1,
                dst_id=sanc,
                tipo_relacao="SANCIONADA_EM",
                fonte="TCU Certidão Consolidada",
                metadata={"emissor": "CGU", "tipo": "CEIS", "situacao": "CONSTAM"},
            )
        )
        # Emenda + Doação TSE (fecha ciclo!)
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", pol, "emenda", m1),
                src_id=pol,
                dst_id=m1,
                tipo_relacao="DESTINOU_EMENDA",
                valor=1_000_000.0,
                fonte="TransfereGov",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p1, "doou", pol),
                src_id=p1,
                dst_id=pol,
                tipo_relacao="DOOU_PARA",
                valor=50_000.0,
                fonte="TSE",
                metadata={"uf": "MG"},
            )
        )

        alerts = run_all_detectors(store, persist=True)
        fired_rules = {a.regra for a in alerts}

        # Todos os 5 detectores devem disparar com explicações claras
        assert "CONFLITO_SOCIO_SERVIDOR" in fired_rules
        assert "CONFLITO_SOCIO_TSE" in fired_rules
        assert "EMPRESA_SANCIONADA" in fired_rules
        assert "SITUACAO_CADASTRAL_IRREGULAR" in fired_rules
        assert "EMPRESA_RECEM_CRIADA" in fired_rules
        assert "CAPITAL_SOCIAL_DESPROPORCIONAL" in fired_rules
        assert "SOCIO_MULTIPLAS_EMPRESAS" in fired_rules
        assert "ENDERECO_COMPARTILHADO" in fired_rules
        assert "FORNECEDOR_MULTI_MUNICIPAL" in fired_rules
        assert "CICLO_EMENDA_DOADOR_CONTRATO" in fired_rules
        assert "VINCULO_INTERMUNICIPAL" in fired_rules

        # Persistidos no DuckDB
        persisted = store.get_alerts()
        assert len(persisted) == len(alerts)
        assert persisted[0].severidade == "ALTA"


def test_same_cnpj_root_branches_do_not_trigger_shared_address() -> None:
    """Filiais com mesma raiz CNPJ (8 dígitos) não devem disparar falso positivo de endereço."""
    with GraphStore(":memory:") as store:
        e_matriz = make_empresa_id("33.333.333/0001-01")
        e_filial = make_empresa_id("33.333.333/0002-82")
        store.upsert_node(
            GraphNode(
                node_id=e_matriz,
                tipo="empresa",
                chave_canonica="33333333000101",
                nome="EMPRESA MATRIZ",
                metadata={"cnpj_raiz": "33333333", "cep": "01001000", "numero": "10"},
            )
        )
        store.upsert_node(
            GraphNode(
                node_id=e_filial,
                tipo="empresa",
                chave_canonica="33333333000282",
                nome="EMPRESA FILIAL",
                metadata={"cnpj_raiz": "33333333", "cep": "01001000", "numero": "10"},
            )
        )

        alerts = run_all_detectors(store, persist=False)
        assert not any(a.regra == "ENDERECO_COMPARTILHADO" for a in alerts)
