"""Unit tests for `arie.auth.decode_supabase_jwt` — pure token verification,
no database. `resolve_auth_context`'s organization-membership half needs a
real `organization_members` row to mean anything, so it is covered against a
live database in `tests/integration/test_tenancy_isolation_integration.py`
instead of mocked here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from arie.auth import AuthenticationError, decode_supabase_jwt
from arie.config import SupabaseAuthConfig

SECRET = "unit-test-shared-secret-at-least-32-bytes-long"


def _token(
    *, secret: str = SECRET, audience: str | None = "authenticated", **claims: object
) -> str:
    payload: dict[str, object] = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        **claims,
    }
    if audience is not None:
        payload["aud"] = audience
    return jwt.encode(payload, secret, algorithm="HS256")


def test_a_correctly_signed_token_decodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=SECRET))
    claims = decode_supabase_jwt(_token())
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"


def test_a_token_signed_with_the_wrong_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=SECRET))
    forged = _token(secret="a-different-secret")
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(forged)


def test_an_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=SECRET))
    expired = jwt.encode(
        {
            "sub": "11111111-1111-1111-1111-111111111111",
            "aud": "authenticated",
            "exp": datetime.now(UTC) - timedelta(hours=1),
        },
        SECRET,
        algorithm="HS256",
    )
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(expired)


def test_a_token_with_the_wrong_audience_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supabase user session tokens carry `aud: "authenticated"` — a token
    minted for some other purpose (a different `aud`) must not verify here."""
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=SECRET))
    wrong_audience = _token(audience="service_role")
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(wrong_audience)


def test_an_unconfigured_secret_is_rejected_rather_than_silently_trusting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=""))
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(_token())


def test_a_malformed_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=SECRET))
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt("not-a-jwt-at-all")
