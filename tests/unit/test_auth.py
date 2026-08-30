"""Unit tests for `arie.auth.decode_supabase_jwt` — pure token verification,
no database, no network. `resolve_auth_context`'s organization-membership
half needs a real `organization_members` row to mean anything, so it is
covered against a live database in
`tests/integration/test_tenancy_isolation_integration.py` instead of mocked
here.

Tokens are signed ES256 (an EC P-256 key pair generated per test), matching
this project's actual Supabase signing key — confirmed via its dashboard,
not assumed (see `SupabaseAuthConfig`'s own docstring). Rather than mocking
network I/O or standing up a fake JWKS HTTP endpoint, every test monkeypatches
`arie.auth._signing_key_for` directly to return the test key pair's public
key — the same seam `decode_supabase_jwt` uses in production to resolve a
key from Supabase's real JWKS by `kid`.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import EllipticCurvePrivateKey

from arie.auth import AuthenticationError, decode_supabase_jwt
from arie.config import SupabaseAuthConfig

ISSUER_BASE = "https://unit-test-project.supabase.co"
CONFIG = SupabaseAuthConfig(url=ISSUER_BASE)


def _keypair() -> EllipticCurvePrivateKey:
    return ec.generate_private_key(ec.SECP256R1())


def _token(
    private_key: EllipticCurvePrivateKey,
    *,
    audience: str | None = "authenticated",
    issuer: str | None = None,
    **claims: object,
) -> str:
    payload: dict[str, object] = {
        "sub": "11111111-1111-1111-1111-111111111111",
        "exp": datetime.now(UTC) + timedelta(hours=1),
        "iss": CONFIG.issuer if issuer is None else issuer,
        **claims,
    }
    if audience is not None:
        payload["aud"] = audience
    return jwt.encode(payload, private_key, algorithm="ES256")


def _patch(monkeypatch: pytest.MonkeyPatch, public_key: Any) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", CONFIG)
    monkeypatch.setattr("arie.auth._signing_key_for", lambda token: public_key)


def test_a_correctly_signed_token_decodes(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key = _keypair()
    _patch(monkeypatch, private_key.public_key())
    claims = decode_supabase_jwt(_token(private_key))
    assert claims["sub"] == "11111111-1111-1111-1111-111111111111"


def test_a_token_signed_with_the_wrong_key_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The attacker's own key signs the token; verification still resolves
    the *real* public key (exactly as `_signing_key_for` would via a real
    `kid` lookup) and correctly rejects the mismatched signature."""
    real_key = _keypair()
    forged_key = _keypair()
    _patch(monkeypatch, real_key.public_key())
    forged = _token(forged_key)
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(forged)


def test_an_expired_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key = _keypair()
    _patch(monkeypatch, private_key.public_key())
    expired = _token(private_key, exp=datetime.now(UTC) - timedelta(hours=1))
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(expired)


def test_a_token_with_the_wrong_audience_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Supabase user session tokens carry `aud: "authenticated"` — a token
    minted for some other purpose (a different `aud`) must not verify here."""
    private_key = _keypair()
    _patch(monkeypatch, private_key.public_key())
    wrong_audience = _token(private_key, audience="service_role")
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(wrong_audience)


def test_a_token_with_the_wrong_issuer_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token issued by a *different* Supabase project must not verify
    here, even if it were somehow signed by a key this JWKS lookup would
    accept — `iss` ties verification to this specific project."""
    private_key = _keypair()
    _patch(monkeypatch, private_key.public_key())
    wrong_issuer = _token(private_key, issuer="https://a-different-project.supabase.co/auth/v1")
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(wrong_issuer)


def test_an_unconfigured_url_is_rejected_rather_than_silently_trusting_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(url=""))
    private_key = _keypair()
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(_token(private_key))


def test_a_malformed_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key = _keypair()
    _patch(monkeypatch, private_key.public_key())
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt("not-a-jwt-at-all")


def test_a_jwks_lookup_failure_is_reported_as_authentication_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_signing_key_for` can fail outside PyJWT's own exception hierarchy —
    a network error, an unknown `kid`, a malformed JWKS response. None of
    that should leak past `decode_supabase_jwt`'s one documented failure
    mode."""
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", CONFIG)

    def _raise(token: str) -> Any:
        raise RuntimeError("could not fetch JWKS")

    monkeypatch.setattr("arie.auth._signing_key_for", _raise)
    private_key = _keypair()
    with pytest.raises(AuthenticationError):
        decode_supabase_jwt(_token(private_key))
