"""Constants and defaults for the Grafo de Transparência feature."""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_DB_PATH = REPO_ROOT / "data" / "grafo_transparencia.duckdb"
DB_PATH_ENV_VAR = "MCP_BRASIL_GRAFO_DB"

DEFAULT_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24 horas para CNPJ/TCU/TSE
DEFAULT_ONDEMAND_TIMEOUT_SECONDS = 45.0
DEFAULT_MAX_FORNECEDORES = 10
DEFAULT_MAX_CONCURRENCY = 5
DEFAULT_MAX_HOPS = 4

# Limiares explicáveis dos detectores de indícios (auditáveis, não acusatórios)
YOUNG_COMPANY_DAYS_THRESHOLD = 180
CAPITAL_RATIO_THRESHOLD = 10.0
MIN_CONTRACT_VALUE_FOR_CAPITAL_CHECK = 50_000.0
REGIONAL_RADIUS_KM = 150.0
LONG_DISTANCE_PAYROLL_KM = 150.0
MIN_PERSON_MATCH_SCORE = 60.0

LGPD_AUDIT_DISCLAIMER = (
    "Nota de uso responsável (LGPD): os indícios e conexões apresentados baseiam-se "
    "exclusivamente em dados públicos oficiais e representam sinais estatísticos/relacionais "
    "para triagem e auditoria — não constituem prova de irregularidade ou vínculo ilícito. "
    "CPFs permanecem mascarados."
)


def resolve_db_path(explicit_path: str | Path | None = None) -> Path:
    """Resolve DuckDB path from explicit arg, env var, or repo default."""
    if explicit_path is not None and str(explicit_path).strip():
        return Path(str(explicit_path).strip()).expanduser()
    env_val = os.environ.get(DB_PATH_ENV_VAR, "").strip()
    if env_val:
        return Path(env_val).expanduser()
    return DEFAULT_DB_PATH
