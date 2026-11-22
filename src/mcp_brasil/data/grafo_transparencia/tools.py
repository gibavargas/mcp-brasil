"""MCP tools for the Grafo de Transparência feature."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from . import orchestrator
from .constants import DEFAULT_MAX_FORNECEDORES, DEFAULT_MAX_HOPS
from .exporter import (
    export_graph_html,
    export_graph_json,
    export_graph_mermaid,
    format_dossier_markdown,
    format_paths_markdown,
)
from .rules import run_all_detectors
from .store import GraphStore


async def investigar_municipio_360(
    municipio: str,
    uf: str | None = None,
    ano: int | None = None,
    max_fornecedores: int = DEFAULT_MAX_FORNECEDORES,
    forcar_atualizacao: bool = False,
    formato: Literal["markdown", "json", "mermaid"] = "markdown",
    db_path: str | None = None,
) -> str:
    """Executa investigação 360° de um município cruzando contratos, folha, QSA, TCU e emendas.

    Consulta em paralelo o portal municipal (via transparencia-go/fallback), emendas Pix no
    TransfereGov e enriquece os principais fornecedores (CNPJs) na BrasilAPI (QSA/Receita
    Federal) e na Certidão Consolidada do TCU (TCU Inidôneos, CNJ Improbidade, CEIS e CNEP).
    Persiste o subgrafo no DuckDB local e aplica os 5 detectores explicáveis (LGPD-safe).

    Args:
        municipio: Nome do município ou código IBGE de 7 dígitos (ex: "Santa Luzia" ou "3157807").
        uf: Sigla da UF com 2 letras para desambiguação (ex: "MG", "SP").
        ano: Exercício fiscal de referência (opcional).
        max_fornecedores: Máximo de maiores fornecedores (CNPJs) enriquecidos (1-25, padrão 10).
        forcar_atualizacao: Se True, ignora cache DuckDB (TTL 24h) e consulta APIs novamente.
        formato: Saída desejada: "markdown" (dossiê completo), "json" ou "mermaid".
        db_path: Caminho opcional do banco DuckDB (padrão MCP_BRASIL_GRAFO_DB).
    """
    with GraphStore(db_path) as store:
        dossier = await orchestrator.investigar_municipio_360(
            store=store,
            municipio=municipio,
            uf=uf,
            ano=ano,
            max_fornecedores=max_fornecedores,
            forcar_atualizacao=forcar_atualizacao,
        )
        if formato == "json":
            return export_graph_json(dossier.nodes, dossier.edges, dossier.alerts)
        if formato == "mermaid":
            return export_graph_mermaid(dossier.nodes, dossier.edges)
        return format_dossier_markdown(dossier)


async def investigar_fornecedor_360(
    cnpj: str,
    forcar_atualizacao: bool = False,
    formato: Literal["markdown", "json", "mermaid"] = "markdown",
    db_path: str | None = None,
) -> str:
    """Executa raio-X 360° de um fornecedor (CNPJ) conectando QSA, sanções, TSE e municípios.

    Consulta dados cadastrais e quadro societário (QSA) na BrasilAPI, verifica sanções na
    Certidão Consolidada do TCU (TCU/CNJ/CEIS/CNEP), enriquece opcionalmente no Portal da
    Transparência Federal (se TRANSPARENCIA_API_KEY estiver configurada), busca vínculos
    eleitorais dos sócios no TSE e cruza com todos os contratos/municípios no grafo DuckDB.

    Args:
        cnpj: CNPJ do fornecedor (com ou sem pontuação, 14 dígitos).
        forcar_atualizacao: Se True, ignora o cache DuckDB de 24h.
        formato: Formato de saída: "markdown", "json" ou "mermaid".
        db_path: Caminho opcional do banco DuckDB.
    """
    with GraphStore(db_path) as store:
        dossier = await orchestrator.investigar_fornecedor_360(
            store=store,
            cnpj=cnpj,
            forcar_atualizacao=forcar_atualizacao,
        )
        if formato == "json":
            return export_graph_json(dossier.nodes, dossier.edges, dossier.alerts)
        if formato == "mermaid":
            return export_graph_mermaid(dossier.nodes, dossier.edges)
        return format_dossier_markdown(dossier)


async def investigar_pessoa_360(
    nome: str,
    uf: str | None = None,
    ano_eleicao: int = 2024,
    forcar_atualizacao: bool = False,
    formato: Literal["markdown", "json", "mermaid"] = "markdown",
    db_path: str | None = None,
) -> str:
    """Mapeia vínculos públicos, societários (QSA) e eleitorais (TSE) de uma pessoa (LGPD-safe).

    Cruza ocorrências já coletadas no grafo DuckDB (folhas de pagamento municipais e quadros
    societários de fornecedores) com candidaturas no TSE, aplicando resolução probabilística
    explicável com CPF sempre mascarado (`***.456.789-**`).

    Args:
        nome: Nome completo da pessoa a investigar.
        uf: Sigla da UF (opcional) para filtrar buscas no TSE.
        ano_eleicao: Ano eleitoral de referência no TSE (padrão 2024).
        forcar_atualizacao: Se True, ignora o cache DuckDB de 24h.
        formato: Formato de saída: "markdown", "json" ou "mermaid".
        db_path: Caminho opcional do banco DuckDB.
    """
    with GraphStore(db_path) as store:
        dossier = await orchestrator.investigar_pessoa_360(
            store=store,
            nome=nome,
            uf=uf,
            ano_eleicao=ano_eleicao,
            forcar_atualizacao=forcar_atualizacao,
        )
        if formato == "json":
            return export_graph_json(dossier.nodes, dossier.edges, dossier.alerts)
        if formato == "mermaid":
            return export_graph_mermaid(dossier.nodes, dossier.edges)
        return format_dossier_markdown(dossier)


async def seguir_emenda_parlamentar(
    autor_ou_municipio: str,
    uf: str | None = None,
    ano: int | None = None,
    max_municipios: int = 3,
    forcar_atualizacao: bool = False,
    formato: Literal["markdown", "json", "mermaid"] = "markdown",
    db_path: str | None = None,
) -> str:
    """Rastreia 'Follow the Money': Parlamentar -> Emenda Pix -> Município -> Contratos -> Sócios.

    Busca transferências especiais no TransfereGov por autor ou município beneficiário,
    enriquece os principais municípios destino com contratos, QSA e certidões TCU, e sinaliza
    eventuais ciclos ou concentrações no grafo DuckDB.

    Args:
        autor_ou_municipio: Nome do parlamentar autor da emenda ou do município beneficiário.
        uf: Sigla da UF (opcional).
        ano: Ano da emenda/plano de ação (opcional).
        max_municipios: Máximo de municípios beneficiários enriquecidos em detalhe (1-5, padrão 3).
        forcar_atualizacao: Se True, ignora o cache DuckDB.
        formato: Formato de saída: "markdown", "json" ou "mermaid".
        db_path: Caminho opcional do banco DuckDB.
    """
    with GraphStore(db_path) as store:
        dossier = await orchestrator.seguir_emenda_parlamentar(
            store=store,
            autor_ou_municipio=autor_ou_municipio,
            uf=uf,
            ano=ano,
            max_municipios=max_municipios,
            forcar_atualizacao=forcar_atualizacao,
        )
        if formato == "json":
            return export_graph_json(dossier.nodes, dossier.edges, dossier.alerts)
        if formato == "mermaid":
            return export_graph_mermaid(dossier.nodes, dossier.edges)
        return format_dossier_markdown(dossier)


async def encontrar_conexoes(
    entidade_origem: str,
    entidade_destino: str,
    max_saltos: int = DEFAULT_MAX_HOPS,
    db_path: str | None = None,
) -> str:
    """Encontra os menores caminhos (BFS) conectando duas entidades quaisquer no grafo DuckDB.

    Aceita nomes, códigos IBGE (7 dígitos), CNPJs (14 dígitos) ou IDs canônicos
    (`municipio:3157807`, `empresa:12345678000199`, `pessoa:JOAO SILVA`, `politico:...`).

    Args:
        entidade_origem: Nome, CNPJ, IBGE ou node_id da primeira entidade.
        entidade_destino: Nome, CNPJ, IBGE ou node_id da segunda entidade.
        max_saltos: Distância máxima em saltos no grafo (1-6, padrão 4).
        db_path: Caminho opcional do banco DuckDB.
    """
    hops = max(1, min(int(max_saltos), 6))
    with GraphStore(db_path) as store:
        orig_hits = store.find_nodes(entidade_origem, limit=1)
        dst_hits = store.find_nodes(entidade_destino, limit=1)
        if not orig_hits:
            return f"Entidade de origem `{entidade_origem}` não encontrada no grafo DuckDB."
        if not dst_hits:
            return f"Entidade de destino `{entidade_destino}` não encontrada no grafo DuckDB."
        src = orig_hits[0]
        dst = dst_hits[0]
        paths = store.find_shortest_paths(src.node_id, dst.node_id, max_hops=hops)
        return format_paths_markdown(src.nome, dst.nome, paths)


async def exportar_grafo(
    entidade: str,
    formato: Literal["json", "mermaid", "html"] = "mermaid",
    max_saltos: int = 2,
    salvar_em: str | None = None,
    db_path: str | None = None,
) -> str:
    """Exporta o subgrafo de uma entidade persistida no DuckDB em JSON, Mermaid ou HTML interativo.

    Args:
        entidade: Nome, CNPJ, código IBGE ou node_id da entidade central.
        formato: "json", "mermaid" (flowchart LR) ou "html" (SVG interativo autocontido).
        max_saltos: Profundidade de expansão a partir da entidade central (1-4, padrão 2).
        salvar_em: Caminho opcional de arquivo onde salvar o conteúdo exportado.
        db_path: Caminho opcional do banco DuckDB.
    """
    hops = max(1, min(int(max_saltos), 4))
    with GraphStore(db_path) as store:
        hits = store.find_nodes(entidade, limit=1)
        if not hits:
            return f"Entidade `{entidade}` não encontrada no grafo DuckDB."
        root = hits[0]
        nodes, edges = store.query_neighborhood(root.node_id, max_hops=hops)
        alerts = store.get_alerts(entidade_ids={n.node_id for n in nodes}, limit=50)

        if formato == "json":
            content = export_graph_json(nodes, edges, alerts)
        elif formato == "html":
            content = export_graph_html(f"Grafo — {root.nome}", nodes, edges, alerts)
        else:
            content = export_graph_mermaid(nodes, edges)

        if salvar_em:
            out_path = Path(salvar_em).expanduser()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(content, encoding="utf-8")
            return (
                f"Subgrafo de **{root.nome}** ({len(nodes)} nós, {len(edges)} arestas, "
                f"{len(alerts)} indícios) exportado em `{formato}` para `{out_path}`."
            )
        return content


async def importar_harvest_grafo(
    arquivo_ndjson: str,
    recomputar_alertas: bool = True,
    db_path: str | None = None,
) -> str:
    """Ingere arquivo NDJSON de `transparencia-go harvest` no grafo DuckDB e roda os detectores.

    Permite consolidar coletas em lote de UFs ou regiões inteiras e descobrir conexões
    transversais entre municípios (fornecedores multi-municipais, sócios e vínculos de pessoal).

    Args:
        arquivo_ndjson: Caminho do NDJSON produzido por `transparencia-go harvest --out ...`.
        recomputar_alertas: Se True (padrão), executa os 5 detectores explicáveis após importar.
        db_path: Caminho opcional do banco DuckDB.
    """
    with GraphStore(db_path) as store:
        summary = store.import_harvest_ndjson(arquivo_ndjson)
        if recomputar_alertas:
            alerts = run_all_detectors(store, scope_node_ids=None, persist=True)
            summary.alerts_gerados = len(alerts)
        st = store.stats()

    return (
        "# Importação de Harvest Concluída\n\n"
        f"- **Arquivo**: `{summary.arquivo}`\n"
        f"- **Municípios lidos**: {summary.municipios_lidos} "
        f"(erros de leitura: {summary.erros_leitura})\n"
        f"- **Nós processados**: {summary.nodes_inseridos} "
        f"(total no grafo: {st['total_nodes']})\n"
        f"- **Arestas processadas**: {summary.edges_inseridas} "
        f"(total no grafo: {st['total_edges']})\n"
        f"- **Indícios gerados**: {summary.alerts_gerados} "
        f"(total no grafo: {st['total_alerts']})\n"
        f"- **Banco DuckDB**: `{st['db_path']}`\n"
    )
