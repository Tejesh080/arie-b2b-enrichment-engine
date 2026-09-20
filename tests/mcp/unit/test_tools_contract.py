"""Unit tests for ``inspect_api_contract`` using ``httpx.MockTransport``
(part of httpx itself — no extra test dependency) instead of a real
process, so these stay in the fast/no-DB/no-network unit tier."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from arie_mcp.errors import UpstreamUnavailableError
from arie_mcp.tools import contract

_FAKE_SPEC = {
    "openapi": "3.1.0",
    "info": {"title": "ARIE API"},
    "paths": {
        "/healthz": {"get": {"summary": "Health", "tags": ["health"]}},
        "/leads": {
            "post": {"summary": "Ingest a lead", "tags": ["leads"]},
        },
        "/leads/{lead_id}": {
            "get": {"summary": "Get a lead", "tags": ["leads"]},
            "parameters": [{"name": "lead_id"}],  # not a method — must be skipped
        },
        "/organization": {"get": {"summary": "Get org", "tags": ["organization"]}},
    },
    "components": {"schemas": {"Lead": {"type": "object"}}},
}

_ORIGINAL_ASYNC_CLIENT_INIT = httpx.AsyncClient.__init__
"""Captured once, at import time, before any test patches ``__init__`` —
every patch below calls back into this, never into whatever
``httpx.AsyncClient.__init__`` currently resolves to (which would recurse
into itself once patched)."""


def _install_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def patched_init(self: httpx.AsyncClient, *args: Any, **kwargs: Any) -> None:
        kwargs["transport"] = httpx.MockTransport(handler)
        _ORIGINAL_ASYNC_CLIENT_INIT(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)


def _json_handler(
    status_code: int = 200, json_body: dict[str, Any] | None = None
) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json=json_body if json_body is not None else _FAKE_SPEC)

    return handler


@pytest.fixture(autouse=True)
def _default_spec_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test gets the fake spec above unless it installs its own
    transport (which simply overrides this fixture's patch for that test)."""
    _install_transport(monkeypatch, _json_handler())


async def test_lists_routes_from_the_live_spec() -> None:
    outcome = await contract.inspect_api_contract_impl(
        "http://localhost:8000", path_prefix=None, include_schemas=False, timeout_seconds=5.0
    )

    paths = {(r["method"], r["path"]) for r in outcome.data["routes"]}
    assert ("GET", "/healthz") in paths
    assert ("POST", "/leads") in paths
    assert ("GET", "/leads/{lead_id}") in paths
    assert ("GET", "/organization") in paths
    assert outcome.data["route_count"] == 4
    assert "schemas" not in outcome.data


async def test_path_prefix_filters_routes() -> None:
    outcome = await contract.inspect_api_contract_impl(
        "http://localhost:8000", path_prefix="/leads", include_schemas=False, timeout_seconds=5.0
    )

    paths = {r["path"] for r in outcome.data["routes"]}
    assert paths == {"/leads", "/leads/{lead_id}"}


async def test_include_schemas_returns_components() -> None:
    outcome = await contract.inspect_api_contract_impl(
        "http://localhost:8000", path_prefix=None, include_schemas=True, timeout_seconds=5.0
    )

    assert outcome.data["schemas"] == {"Lead": {"type": "object"}}
    assert outcome.truncated is False


async def test_oversized_schemas_are_truncated_not_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    huge_spec = dict(_FAKE_SPEC)
    huge_spec["components"] = {"schemas": {"Huge": {"description": "x" * 200_000}}}
    _install_transport(monkeypatch, _json_handler(json_body=huge_spec))

    outcome = await contract.inspect_api_contract_impl(
        "http://localhost:8000", path_prefix=None, include_schemas=True, timeout_seconds=5.0
    )

    assert "schemas" not in outcome.data
    assert outcome.truncated is True


async def test_connection_failure_raises_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_transport(monkeypatch, handler)

    with pytest.raises(UpstreamUnavailableError):
        await contract.inspect_api_contract_impl(
            "http://localhost:8000", path_prefix=None, include_schemas=False, timeout_seconds=5.0
        )


async def test_non_2xx_status_raises_upstream_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_transport(monkeypatch, _json_handler(status_code=500, json_body={"detail": "error"}))

    with pytest.raises(UpstreamUnavailableError):
        await contract.inspect_api_contract_impl(
            "http://localhost:8000", path_prefix=None, include_schemas=False, timeout_seconds=5.0
        )
