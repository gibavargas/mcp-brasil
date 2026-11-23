"""Preservação anti-exclusão de evidências via Internet Archive (Wayback Machine)."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

WAYBACK_SAVE_BASE = "https://web.archive.org/save/"
WAYBACK_AVAILABLE_API = "https://archive.org/wayback/available"
DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; mcp-brasil-audit-archiver/1.0)"


@dataclass
class ArchiveSnapshotResult:
    """Resultado auditável da tentativa de snapshot no Internet Archive."""

    original_url: str
    status: str  # "archived_live" | "archived_existing" | "unavailable" | "error"
    archive_url: str = ""
    timestamp: str = ""
    http_status: int = 0
    method: str = ""
    checked_at_utc: str = ""
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def pncp_app_url_to_api_url(url: str) -> str | None:
    """Converte URL SPA do PNCP em URL JSON oficial imutável para arquivamento."""
    prefix = "https://pncp.gov.br/app/contratos/"
    if not url.startswith(prefix):
        return None
    parts = url[len(prefix) :].strip("/").split("/")
    if len(parts) == 3 and parts[0].isdigit() and parts[1].isdigit() and parts[2].isdigit():
        return (
            f"https://pncp.gov.br/api/pncp/v1/orgaos/{parts[0]}/contratos/{parts[1]}/{parts[2]}"
        )
    return None


def _strip_scheme(url: str) -> str:
    s = (url or "").strip()
    if "://" in s:
        return s.split("://", 1)[1]
    return s


def _extract_wayback_timestamp(archive_url: str) -> str:
    """Extrai timestamp YYYYMMDDhhmmss de uma URL https://web.archive.org/web/<ts>/..."""
    marker = "/web/"
    if marker not in archive_url:
        return ""
    tail = archive_url.split(marker, 1)[1]
    ts = tail.split("/", 1)[0]
    digits = "".join(ch for ch in ts if ch.isdigit())
    return digits[:14]


def _normalize_wayback_https(url: str) -> str:
    s = (url or "").strip()
    if s.startswith("http://web.archive.org/"):
        return "https://" + s[len("http://") :]
    return s


async def lookup_wayback_available(
    url: str,
    client: httpx.AsyncClient,
) -> tuple[str, str]:
    """Consulta o snapshot mais recente já indexado no Wayback Machine."""
    try:
        resp = await client.get(
            WAYBACK_AVAILABLE_API,
            params={"url": url},
            timeout=15.0,
        )
        if resp.status_code == 200:
            payload = resp.json()
            closest = (payload.get("archived_snapshots") or {}).get("closest") or {}
            if closest.get("available") and closest.get("url"):
                arch_url = _normalize_wayback_https(str(closest["url"]))
                ts = str(closest.get("timestamp") or _extract_wayback_timestamp(arch_url))
                return arch_url, ts
    except Exception as exc:
        logger.debug("Wayback available lookup falhou para %s: %s", url, exc)
    return "", ""


async def force_wayback_snapshot(
    url: str,
    client: httpx.AsyncClient,
    *,
    save_timeout: float = 28.0,
    today_prefix: str | None = None,
) -> ArchiveSnapshotResult:
    """Força snapshot ao vivo de `url` via `web.archive.org/save/` com fallback verificável.

    Nunca inventa URL de snapshot: só retorna `archive_url` quando confirmada pelo cabeçalho
    `Location`/`Content-Location` do Wayback Machine ou pela API `/wayback/available`.
    """
    now_utc = datetime.now(timezone.utc)
    checked_at = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    today_tag = today_prefix or now_utc.strftime("%Y%m%d")

    target = (url or "").strip()
    if not target.startswith(("http://", "https://")):
        return ArchiveSnapshotResult(
            original_url=target,
            status="error",
            checked_at_utc=checked_at,
            detail="URL inválida (sem esquema http/https)",
        )

    save_endpoint = f"{WAYBACK_SAVE_BASE}{_strip_scheme(target)}"
    http_status = 0
    detail = ""

    try:
        resp = await client.get(
            save_endpoint,
            follow_redirects=False,
            timeout=save_timeout,
        )
        http_status = resp.status_code
        loc = resp.headers.get("location") or resp.headers.get("content-location") or ""
        if loc and "/web/" in loc:
            if loc.startswith("/"):
                loc = f"https://web.archive.org{loc}"
            loc = _normalize_wayback_https(loc)
            ts = _extract_wayback_timestamp(loc)
            return ArchiveSnapshotResult(
                original_url=target,
                status="archived_live",
                archive_url=loc,
                timestamp=ts,
                http_status=http_status,
                method="save_sync_redirect",
                checked_at_utc=checked_at,
            )
        detail = f"HTTP {http_status}"
    except httpx.TimeoutException:
        detail = "timeout_save_sync"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {str(exc)[:120]}"

    # Aguarda brevemente e verifica se a captura foi indexada ou se há snapshot existente
    if detail == "timeout_save_sync":
        await asyncio.sleep(2.0)

    arch_url, ts = await lookup_wayback_available(target, client)
    if arch_url:
        is_fresh = bool(ts and ts.startswith(today_tag))
        return ArchiveSnapshotResult(
            original_url=target,
            status="archived_live" if is_fresh else "archived_existing",
            archive_url=arch_url,
            timestamp=ts,
            http_status=http_status,
            method="save_then_available" if is_fresh else "wayback_available_fallback",
            checked_at_utc=checked_at,
            detail=detail,
        )

    return ArchiveSnapshotResult(
        original_url=target,
        status="unavailable",
        archive_url="",
        timestamp="",
        http_status=http_status,
        method="save_failed_no_snapshot",
        checked_at_utc=checked_at,
        detail=detail or "Sem snapshot disponível no Wayback Machine",
    )


def format_wayback_timestamp(ts: str) -> str:
    """Formata YYYYMMDDhhmmss em YYYY-MM-DD HH:MM UTC legível."""
    d = "".join(ch for ch in (ts or "") if ch.isdigit())
    if len(d) >= 12:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]} {d[8:10]}:{d[10:12]} UTC"
    if len(d) >= 8:
        return f"{d[0:4]}-{d[4:6]}-{d[6:8]}"
    return ""


def make_wayback_history_url(url: str) -> str:
    """Retorna link do índice histórico do Wayback Machine para consulta manual."""
    encoded = urllib.parse.quote((url or "").strip(), safe=":/?=&%#")
    return f"https://web.archive.org/web/*/{encoded}"
