"""Unit tests for Wayback Machine (archive.org) evidence archiver."""

from __future__ import annotations

import httpx
import pytest
import respx

from mcp_brasil.data.grafo_transparencia.archiver import (
    WAYBACK_AVAILABLE_API,
    force_wayback_snapshot,
    format_wayback_timestamp,
    make_wayback_history_url,
    pncp_app_url_to_api_url,
)


def test_pncp_app_url_to_api_url_and_helpers() -> None:
    assert (
        pncp_app_url_to_api_url("https://pncp.gov.br/app/contratos/00394684000153/2024/129")
        == "https://pncp.gov.br/api/pncp/v1/orgaos/00394684000153/contratos/2024/129"
    )
    assert pncp_app_url_to_api_url("https://example.com/foo") is None
    assert format_wayback_timestamp("20261123182622") == "2026-11-23 18:26 UTC"
    assert (
        make_wayback_history_url("https://lavandeira.megasofttransparencia.com.br")
        == "https://web.archive.org/web/*/https://lavandeira.megasofttransparencia.com.br"
    )


@pytest.mark.asyncio
async def test_force_wayback_snapshot_live_redirect() -> None:
    target = "https://brasilapi.com.br/api/cnpj/v1/33095133000148"
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://web.archive.org/save/brasilapi.com.br/api/cnpj/v1/33095133000148").mock(
            return_value=httpx.Response(
                302,
                headers={
                    "Location": (
                        "https://web.archive.org/web/20261123182539/"
                        "https://brasilapi.com.br/api/cnpj/v1/33095133000148"
                    )
                },
            )
        )
        async with httpx.AsyncClient() as client:
            res = await force_wayback_snapshot(target, client)
    assert res.status == "archived_live"
    assert res.timestamp == "20261123182539"
    assert res.archive_url.startswith("https://web.archive.org/web/20261123182539/")


@pytest.mark.asyncio
async def test_force_wayback_snapshot_fallback_to_available() -> None:
    target = "https://galileia-mg.portaltp.com.br"
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://web.archive.org/save/galileia-mg.portaltp.com.br").mock(
            return_value=httpx.Response(523)
        )
        mock.get(WAYBACK_AVAILABLE_API).mock(
            return_value=httpx.Response(
                200,
                json={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "status": "200",
                            "timestamp": "20261123182818",
                            "url": (
                                "http://web.archive.org/web/20261123182818/"
                                "http://galileia-mg.portaltp.com.br/"
                            ),
                        }
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            res = await force_wayback_snapshot(target, client, today_prefix="20261123")
    assert res.status == "archived_live"
    assert res.archive_url.startswith("https://web.archive.org/web/20261123182818/")


@pytest.mark.asyncio
async def test_force_wayback_snapshot_existing_older_snapshot() -> None:
    target = "https://example.gov.br/portal"
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://web.archive.org/save/example.gov.br/portal").mock(
            return_value=httpx.Response(429)
        )
        mock.get(WAYBACK_AVAILABLE_API).mock(
            return_value=httpx.Response(
                200,
                json={
                    "archived_snapshots": {
                        "closest": {
                            "available": True,
                            "status": "200",
                            "timestamp": "20251009004516",
                            "url": "http://web.archive.org/web/20251009004516/https://example.gov.br/portal",
                        }
                    }
                },
            )
        )
        async with httpx.AsyncClient() as client:
            res = await force_wayback_snapshot(target, client, today_prefix="20261123")
    assert res.status == "archived_existing"
    assert res.timestamp == "20251009004516"
    assert res.archive_url.startswith("https://web.archive.org/web/20251009004516/")
