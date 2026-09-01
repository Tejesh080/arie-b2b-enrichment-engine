"""Cloudflare Turnstile verification (Productization M6 Part 12) — hermetic:
`httpx.post` is monkeypatched, so nothing here needs a real Cloudflare
account or network access.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from arie import turnstile
from arie.config import TurnstileConfig


@dataclass
class _FakeResponse:
    _body: dict[str, Any]
    status_code: int = 200

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError("http error")

    def json(self) -> dict[str, Any]:
        return self._body


def test_unconfigured_turnstile_always_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(turnstile, "TURNSTILE", TurnstileConfig(secret_key="", site_key=""))
    assert turnstile.verify_turnstile_token(None) is True
    assert turnstile.verify_turnstile_token("garbage") is True


def test_configured_turnstile_rejects_a_missing_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        turnstile, "TURNSTILE", TurnstileConfig(secret_key="sk_test", site_key="pk_test")
    )
    assert turnstile.verify_turnstile_token(None) is False


def test_configured_turnstile_accepts_a_successful_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        turnstile, "TURNSTILE", TurnstileConfig(secret_key="sk_test", site_key="pk_test")
    )
    monkeypatch.setattr(
        "arie.turnstile.httpx.post", lambda *a, **kw: _FakeResponse({"success": True})
    )
    assert turnstile.verify_turnstile_token("a-real-looking-token") is True


def test_configured_turnstile_rejects_a_failed_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        turnstile, "TURNSTILE", TurnstileConfig(secret_key="sk_test", site_key="pk_test")
    )
    monkeypatch.setattr(
        "arie.turnstile.httpx.post",
        lambda *a, **kw: _FakeResponse(
            {"success": False, "error-codes": ["invalid-input-response"]}
        ),
    )
    assert turnstile.verify_turnstile_token("a-bad-token") is False


def test_configured_turnstile_fails_closed_on_a_transport_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        turnstile, "TURNSTILE", TurnstileConfig(secret_key="sk_test", site_key="pk_test")
    )

    def _raise(*a: object, **kw: object) -> Any:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("arie.turnstile.httpx.post", _raise)
    assert turnstile.verify_turnstile_token("a-token") is False
