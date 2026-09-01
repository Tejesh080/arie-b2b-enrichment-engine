"""Supabase Auth Admin API email lookup (Productization M6) — hermetic:
`httpx.get` is monkeypatched, so nothing here needs a real Supabase project
or `SUPABASE_SERVICE_ROLE_KEY`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

from arie import supabase_admin
from arie.config import SupabaseAuthConfig


@dataclass
class _FakeResponse:
    _body: dict[str, Any]
    status_code: int = 200

    def json(self) -> dict[str, Any]:
        return self._body


def test_unconfigured_supabase_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supabase_admin, "SUPABASE_AUTH", SupabaseAuthConfig(url="", service_role_key="")
    )
    assert supabase_admin.get_user_email(uuid.uuid4()) is None


def test_missing_service_role_key_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supabase_admin,
        "SUPABASE_AUTH",
        SupabaseAuthConfig(url="https://project.supabase.co", service_role_key=""),
    )
    assert supabase_admin.get_user_email(uuid.uuid4()) is None


def test_successful_lookup_returns_the_email(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supabase_admin,
        "SUPABASE_AUTH",
        SupabaseAuthConfig(url="https://project.supabase.co", service_role_key="sr_test"),
    )
    monkeypatch.setattr(
        "arie.supabase_admin.httpx.get",
        lambda *a, **kw: _FakeResponse({"email": "owner@example.com"}),
    )
    assert supabase_admin.get_user_email(uuid.uuid4()) == "owner@example.com"


def test_failed_lookup_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supabase_admin,
        "SUPABASE_AUTH",
        SupabaseAuthConfig(url="https://project.supabase.co", service_role_key="sr_test"),
    )
    monkeypatch.setattr(
        "arie.supabase_admin.httpx.get", lambda *a, **kw: _FakeResponse({}, status_code=404)
    )
    assert supabase_admin.get_user_email(uuid.uuid4()) is None


def test_transport_error_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        supabase_admin,
        "SUPABASE_AUTH",
        SupabaseAuthConfig(url="https://project.supabase.co", service_role_key="sr_test"),
    )

    def _raise(*a: object, **kw: object) -> Any:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr("arie.supabase_admin.httpx.get", _raise)
    assert supabase_admin.get_user_email(uuid.uuid4()) is None
