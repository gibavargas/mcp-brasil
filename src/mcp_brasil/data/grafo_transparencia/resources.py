"""MCP resources for the Grafo de Transparência feature."""

from __future__ import annotations

import json

from .store import GraphStore


def estatisticas_grafo_resource() -> str:
    """Retorna estatísticas em JSON do banco relacional DuckDB do Grafo de Transparência."""
    with GraphStore() as store:
        return json.dumps(store.stats(), ensure_ascii=False, indent=2)
