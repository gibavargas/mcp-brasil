"""MCP prompt templates for Grafo de Transparência investigations."""

from __future__ import annotations


def auditoria_rede_municipio(municipio: str, uf: str, ano: str = "2025") -> str:
    """Roteiro investigativo 360° para rede de contratos, sócios, emendas e indícios municipais."""
    return (
        f"Realize uma auditoria investigativa 360° da rede de transparência do município de "
        f"{municipio}/{uf} (exercício {ano}):\n"
        f"1. Execute `investigar_municipio_360(municipio='{municipio}', uf='{uf}', ano={ano})`.\n"
        "2. Para cada fornecedor com alerta de severidade ALTA ou MEDIA, execute "
        "`investigar_fornecedor_360(cnpj=...)` para expandir o QSA, sanções "
        "(TCU/CEIS/CNEP/CNJ) e vínculos no TSE.\n"
        "3. Use `encontrar_conexoes` entre parlamentares autores de emendas e os maiores "
        "fornecedores.\n"
        "4. Exporte o diagrama principal com `exportar_grafo(entidade=..., formato='mermaid')`.\n"
        "5. Apresente todos os achados como indícios explicáveis para auditoria "
        "(LGPD-safe, com URLs oficiais)."
    )


def follow_the_money_emenda(autor_ou_municipio: str, ano: str = "2024") -> str:
    """Roteiro para rastrear o trajeto completo de emendas até fornecedores e sócios."""
    return (
        f"Rastreie o fluxo completo de emendas parlamentares ligadas a "
        f"'{autor_ou_municipio}' ({ano}):\n"
        f"1. Execute `seguir_emenda_parlamentar("
        f"autor_ou_municipio='{autor_ou_municipio}', ano={ano})`.\n"
        "2. Destaque os municípios beneficiários, maiores contratos/despesas no mesmo exercício "
        "e sócios das empresas.\n"
        "3. Verifique se há indícios de sanção ativa, empresa recém-aberta ou sobreposição "
        "QSA/folha/TSE.\n"
        "4. Gere o grafo de relações em Mermaid com `exportar_grafo`."
    )
