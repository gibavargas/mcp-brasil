"""Grafo de Transparência feature server — registers tools, resources, and prompts."""

from fastmcp import FastMCP

from .prompts import auditoria_rede_municipio, follow_the_money_emenda
from .resources import estatisticas_grafo_resource
from .tools import (
    encontrar_conexoes,
    exportar_grafo,
    importar_harvest_grafo,
    investigar_fornecedor_360,
    investigar_municipio_360,
    investigar_pessoa_360,
    seguir_emenda_parlamentar,
)

mcp = FastMCP("mcp-brasil-grafo-transparencia")

mcp.tool(
    investigar_municipio_360,
    tags={
        "grafo",
        "transparencia",
        "municipios",
        "auditoria",
        "contratos",
        "qsa",
        "emendas",
        "tcu",
    },
)
mcp.tool(
    investigar_fornecedor_360,
    tags={"grafo", "transparencia", "fornecedores", "cnpj", "qsa", "sancoes", "tcu", "tse"},
)
mcp.tool(
    investigar_pessoa_360,
    tags={"grafo", "transparencia", "pessoas", "servidores", "qsa", "tse", "homonimos"},
)
mcp.tool(
    seguir_emenda_parlamentar,
    tags={"grafo", "transparencia", "emendas", "transferegov", "follow_the_money", "municipios"},
)
mcp.tool(
    encontrar_conexoes,
    tags={"grafo", "transparencia", "conexoes", "bfs", "rede", "investigacao"},
)
mcp.tool(
    exportar_grafo,
    tags={"grafo", "transparencia", "exportacao", "mermaid", "json", "html", "visualizacao"},
)
mcp.tool(
    importar_harvest_grafo,
    tags={"grafo", "transparencia", "harvest", "duckdb", "batch", "municipios"},
)

mcp.resource("grafo://estatisticas", tags={"grafo", "transparencia", "estatisticas"})(
    estatisticas_grafo_resource
)

mcp.prompt(auditoria_rede_municipio, tags={"grafo", "transparencia", "auditoria", "municipios"})
mcp.prompt(follow_the_money_emenda, tags={"grafo", "transparencia", "emendas", "investigacao"})
