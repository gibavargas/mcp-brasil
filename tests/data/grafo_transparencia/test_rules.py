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


def test_conflito_pf_servidor_filters_diarias_folha_auxilios_estornos_and_html_mirrors() -> None:
    """Diárias, folha, auxílios, indenizações, estornos e tabelas HTML espelhadas são filtrados."""
    with GraphStore(":memory:") as store:
        m1 = make_municipio_id("5211701")
        store.upsert_node(
            GraphNode(
                node_id=m1,
                tipo="municipio",
                chave_canonica="5211701",
                nome="Jandaia (GO)",
                uf="GO",
                codigo_ibge="5211701",
            )
        )

        # 1) Servidor com diária (339014) + salário (319011) + auxílio-alimentação (339046)
        #    + indenização trabalhista (319094) + restituição (339093) + contratação temp (319004)
        #    + convênio saúde (339041) + estorno negativo (-142.0) -> TODOS falsos positivos
        p_fp = make_pessoa_id("CARLOS ALBERTO SERVIDOR REGULAR")
        store.upsert_node(
            GraphNode(
                node_id=p_fp,
                tipo="pessoa",
                chave_canonica="CARLOS ALBERTO SERVIDOR REGULAR",
                nome="Carlos Alberto Servidor Regular",
                uf="GO",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p_fp, "trab", m1),
                src_id=p_fp,
                dst_id=m1,
                tipo_relacao="TRABALHA_EM",
                valor=5000.0,
                metadata={"cargo": "VEREADOR"},
            )
        )
        fp_cases = [
            (600.0, "339014 - DIARIAS CIVIL", "CONCESSAO DE DIARIA PARA VIAGEM A GOIANIA"),
            (5064.45, "319011 - VENCIMENTOS E VANTAGENS FIXAS", "SUBSIDIO SALARIAL MENSAL"),
            (600.0, "339046 - AUXILIO-ALIMENTACAO", "AUXILIO ALIMENTACAO COMPETENCIA 11/2024"),
            (711.92, "319094 - INDENIZACOES TRABALHISTAS", "ACERTO DE VERBAS SALARIAIS"),
            (100.0, "33909303000 - OUTRAS INDENIZACOES E RESTITUICOES", ""),
            (864.95, "31900499000 - OUTRAS CONTRATACOES POR TEMPO DETERMINADO", ""),
            (2000.0, "339034 - OUTRAS DESPESAS DE PESSOAL - TERCEIRIZACAO", "AGENTE ENDEMIAS"),
            (230.25, "339041 - CONTRIBUICOES", "PAGAMENTO DE CONVENIO IPASGO 50%"),
            (-142.0, "33903999000 - OUTROS SERVICOS DE TERCEIROS - PJ", ""),
            (0.0, "", ""),
        ]
        for idx, (val, elem, hist) in enumerate(fp_cases):
            store.upsert_edge(
                GraphEdge(
                    edge_id=make_stable_id("e", m1, "dp_fp", p_fp, str(idx)),
                    src_id=m1,
                    dst_id=p_fp,
                    tipo_relacao="PAGOU_DESPESA",
                    valor=val,
                    metadata={"elemento": elem, "historico": hist},
                )
            )

        # 2) Servidor com tabela HTML espelhada (sem elemento/histórico, funcao == cargo)
        p_html = make_pessoa_id("MARIA HTML ESPELHO")
        store.upsert_node(
            GraphNode(
                node_id=p_html,
                tipo="pessoa",
                chave_canonica="MARIA HTML ESPELHO",
                nome="Maria HTML Espelho",
                uf="BA",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p_html, "trab", m1),
                src_id=p_html,
                dst_id=m1,
                tipo_relacao="TRABALHA_EM",
                metadata={"cargo": "ENFERMEIRO CONTRATADO"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, "dp_html", p_html),
                src_id=m1,
                dst_id=p_html,
                tipo_relacao="PAGOU_DESPESA",
                valor=1500.0,
                metadata={"funcao": "ENFERMEIRO CONTRATADO"},
            )
        )

        # 3) Servidor com despesa sem descrição mas presente em RECEBEU_DIARIA
        p_diar = make_pessoa_id("JOSE DIARIA CRUZADA")
        store.upsert_node(
            GraphNode(
                node_id=p_diar,
                tipo="pessoa",
                chave_canonica="JOSE DIARIA CRUZADA",
                nome="José Diária Cruzada",
                uf="GO",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p_diar, "trab", m1),
                src_id=p_diar,
                dst_id=m1,
                tipo_relacao="TRABALHA_EM",
                metadata={"cargo": "MOTORISTA"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p_diar, "diar", m1),
                src_id=p_diar,
                dst_id=m1,
                tipo_relacao="RECEBEU_DIARIA",
                valor=300.0,
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, "dp_diar", p_diar),
                src_id=m1,
                dst_id=p_diar,
                tipo_relacao="PAGOU_DESPESA",
                valor=300.0,
            )
        )

        alerts = run_all_detectors(store, persist=False)
        pf_alerts = [a for a in alerts if a.regra == "CONFLITO_PF_SERVIDOR"]
        assert pf_alerts == []


def test_conflito_pf_servidor_keeps_commercial_contracts_and_excludes_diaria() -> None:
    """Contratos comerciais (ex: locação de imóvel) e serviços PF mantêm alerta com valor limpo."""
    with GraphStore(":memory:") as store:
        m1 = make_municipio_id("5222302")
        store.upsert_node(
            GraphNode(
                node_id=m1,
                tipo="municipio",
                chave_canonica="5222302",
                nome="Vila Propício (GO)",
                uf="GO",
                codigo_ibge="5222302",
            )
        )

        # Caso A: Servidor com locação de imóvel + uma diária (diária deve ser excluída da soma)
        p_loc = make_pessoa_id("ABIGAIL LOCADORA SERVIDORA")
        store.upsert_node(
            GraphNode(
                node_id=p_loc,
                tipo="pessoa",
                chave_canonica="ABIGAIL LOCADORA SERVIDORA",
                nome="Abigail Locadora Servidora",
                uf="GO",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p_loc, "trab", m1),
                src_id=p_loc,
                dst_id=m1,
                tipo_relacao="TRABALHA_EM",
                metadata={"cargo": "ASSESSOR EXECUTIVO"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, "ct_loc", p_loc),
                src_id=m1,
                dst_id=p_loc,
                tipo_relacao="CONTRATOU",
                valor=4500.0,
                metadata={"numero": "442", "objeto": "LOCAÇÃO DE IMÓVEL URBANO PARA ESCOLA"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, "dp_diaria", p_loc),
                src_id=m1,
                dst_id=p_loc,
                tipo_relacao="PAGOU_DESPESA",
                valor=650.0,
                metadata={"elemento": "339014 - DIARIAS CIVIL", "historico": "DIARIA VIAGEM"},
            )
        )

        # Caso B: Servidor com pagamento 339036 (Serviços PF) sem histórico -> severidade MEDIA
        p_pf = make_pessoa_id("VERA SERVICO PF")
        store.upsert_node(
            GraphNode(
                node_id=p_pf,
                tipo="pessoa",
                chave_canonica="VERA SERVICO PF",
                nome="Vera Serviço PF",
                uf="MG",
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", p_pf, "trab", m1),
                src_id=p_pf,
                dst_id=m1,
                tipo_relacao="TRABALHA_EM",
                metadata={"cargo": "SERVENTE ESCOLAR"},
            )
        )
        store.upsert_edge(
            GraphEdge(
                edge_id=make_stable_id("e", m1, "dp_36", p_pf),
                src_id=m1,
                dst_id=p_pf,
                tipo_relacao="PAGOU_DESPESA",
                valor=700.0,
                metadata={"elemento": "33903699000 - Outros Servicos de Pessoa Fisica"},
            )
        )

        alerts = run_all_detectors(store, persist=False)
        pf_by_ent = {a.entidade_id: a for a in alerts if a.regra == "CONFLITO_PF_SERVIDOR"}
        assert set(pf_by_ent.keys()) == {p_loc, p_pf}

        a_loc = pf_by_ent[p_loc]
        assert a_loc.severidade == "ALTA"
        assert a_loc.evidencias[0]["valor_total"] == 4500.0
        assert a_loc.evidencias[0]["descartados_regulares"] == {"DIARIA_PASSAGEM": 1}
        assert "LOCAÇÃO DE IMÓVEL" in a_loc.explicacao

        a_pf = pf_by_ent[p_pf]
        assert a_pf.severidade == "MEDIA"
        assert a_pf.evidencias[0]["valor_total"] == 700.0
        assert "33903699000" in a_pf.explicacao

