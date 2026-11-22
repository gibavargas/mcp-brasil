"""Grafo de Transparência feature — cross-source entity graph and audit intelligence.

Auto-discovered by FeatureRegistry via FEATURE_META and server.mcp.
"""

from mcp_brasil._shared.feature import FeatureMeta

FEATURE_META = FeatureMeta(
    name="grafo_transparencia",
    description=(
        "Grafo de Transparência Pública: conexões e cruzamentos entre os 5.571 municípios, "
        "fornecedores (CNPJ/QSA), servidores, candidatos/doadores (TSE), sanções "
        "(TCU/CEIS/CNEP/CNJ), contratos (PNCP) e emendas parlamentares (Transferegov) "
        "com persistência DuckDB"
    ),
    version="0.1.0",
    api_base="https://brasilapi.com.br/api",
    requires_auth=False,
    tags=[
        "transparencia",
        "grafo",
        "investigacao",
        "auditoria",
        "municipios",
        "contratos",
        "fornecedores",
        "emendas",
        "qsa",
        "tcu",
        "tse",
        "sancoes",
    ],
)

__all__ = ["FEATURE_META"]
