"""Authentication and tenant-scoping for the runtime API.

Two independent ways to reach an `AuthContext`, both producing the same
type so every endpoint scopes its queries identically regardless of which one
authenticated the request:

* **Supabase user JWT + `X-Organization-Id` header** (`resolve_auth_context`,
  Productization M1) — a human, or a machine holding a real user's session
  token. `organization_id` comes from the header, checked against
  `organization_members`.
* **ARIE organization API key** (Productization M2A —
  `arie.apikeys.verify_api_key`, wrapped here as `resolve_api_key_context`) —
  a machine credential for n8n, scripts, and CRM/webhook integrations that
  should never need a human's session token. `organization_id` comes from the
  *key itself*, never from any header — seeing `X-Organization-Id` on an
  API-key-authenticated request does not make it trusted, it is simply
  ignored (`arie.api.main.get_auth_context` never even parses it on this
  path).

**This is the primary tenant-isolation control, not a secondary one.** The
FastAPI backend connects to Postgres with a service-role/superuser connection
string (`arie.api.main.build_state`), which bypasses row-level security
entirely — see `migrations/0016_row_level_security.sql`'s own docstring. Every
`organization_id` filter added elsewhere in this codebase is only as safe as
this module correctly identifying who is calling and which organization they
belong to.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal
from uuid import UUID

import jwt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.apikeys import InvalidApiKeyError, RevokedApiKeyError, verify_api_key
from arie.config import SUPABASE_AUTH

__all__ = [
    "ROLES",
    "AuthContext",
    "AuthenticationError",
    "InvalidApiKeyError",
    "NotAMemberError",
    "RevokedApiKeyError",
    "decode_supabase_jwt",
    "resolve_api_key_context",
    "resolve_auth_context",
]

ROLES: tuple[str, ...] = ("owner", "admin", "analyst_reviewer")
"""Mirrors the `organization_members.role` CHECK constraint
(`migrations/0012_organizations_and_members.sql`) — kept here too so callers
that want to validate a role string don't have to import a migration."""

_SELECT_MEMBERSHIP = """
    SELECT role FROM organization_members
    WHERE organization_id = %(organization_id)s AND user_id = %(user_id)s AND status = 'active'
"""


class AuthenticationError(Exception):
    """A missing/malformed/invalid-signature/expired bearer token.

    Deliberately one exception type for every way a token can fail to verify
    — the caller sees the same 401 whether the signature was wrong, the token
    expired, or the secret wasn't configured. Distinguishing those to a client
    would leak information about the verification process for no benefit to a
    legitimate caller, who only ever needs to "get a fresh token and retry."
    """


class NotAMemberError(Exception):
    """The token verified, but its subject is not an active member of the
    requested organization.

    A distinct exception from `AuthenticationError` because it maps to a
    different HTTP status (403, not 401) — the caller proved who they are,
    they simply aren't authorized for *this* organization.
    """

    def __init__(self, organization_id: UUID) -> None:
        self.organization_id = organization_id
        super().__init__(f"caller is not an active member of organization {organization_id}")


@dataclass(frozen=True)
class AuthContext:
    """The authenticated caller, scoped to one organization for this request.

    A user who belongs to more than one organization gets a different
    `AuthContext` per request depending on which `X-Organization-Id` they
    sent — there is no session-wide "current organization," and no
    organization-switcher UI to pick one exists yet (explicitly out of scope
    for Productization M1/M2A; the frontend repo is untouched).

    `auth_method` decides which of the two field groups below is populated:
    `user_id`/`role` for `"jwt"`, `api_key_id`/`scopes` for `"api_key"`. They
    are not merged into one shape because they answer different questions —
    a human's authority comes from their *role* in the organization, a
    machine credential's from the *scopes* an admin explicitly granted it —
    and collapsing them would let one silently stand in for the other.
    """

    organization_id: UUID
    auth_method: Literal["jwt", "api_key"]
    user_id: UUID | None = None
    role: str | None = None
    api_key_id: UUID | None = None
    scopes: frozenset[str] | None = None

    def has_scope(self, scope: str) -> bool:
        """Whether this caller may perform an action gated by `scope`.

        A JWT session is never scope-limited — role (owner/admin/
        analyst_reviewer) already gates the boundaries that matter for a
        human, and every role can perform every data-plane action in this
        API today (see `arie.api.main`'s endpoint list). Scopes exist
        specifically to let an *API key* be granted less than full access —
        `leads:read` without `leads:write`, say — which a human session has
        no equivalent restriction for yet.
        """
        if self.auth_method == "jwt":
            return True
        return scope in (self.scopes or frozenset())

    def is_org_admin(self) -> bool:
        """Whether this caller may manage the organization itself (API keys
        today; members/settings/billing in later phases).

        Deliberately `False` for every API-key-authenticated request,
        regardless of its scopes — none of `arie.apikeys.SCOPES` grants
        organization management, and there is no scope that could, on
        purpose: a machine credential must never be able to mint, list, or
        revoke API keys, including the very one authenticating the request
        that asks.
        """
        return self.auth_method == "jwt" and self.role in ("owner", "admin")


def decode_supabase_jwt(token: str) -> dict[str, Any]:
    """Verify and decode a Supabase-issued access token.

    HS256 against `SUPABASE_JWT_SECRET` — Supabase's shared-secret
    verification path, the simplest mechanism available without adding the
    Supabase SDK as a dependency for three endpoints' worth of auth. Every
    failure (unconfigured secret, bad signature, expired token, malformed
    token, wrong audience) raises `AuthenticationError` rather than letting
    `PyJWT`'s exception hierarchy leak into callers that would otherwise have
    to know its vocabulary too.
    """
    if not SUPABASE_AUTH.jwt_secret:
        raise AuthenticationError("SUPABASE_JWT_SECRET is not configured")
    try:
        return jwt.decode(
            token,
            SUPABASE_AUTH.jwt_secret,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except jwt.PyJWTError as exc:
        raise AuthenticationError(f"invalid bearer token: {exc}") from exc


def resolve_auth_context(pool: ConnectionPool, *, token: str, organization_id: UUID) -> AuthContext:
    """Verify `token` and confirm its subject is an active member of `organization_id`.

    Raises `AuthenticationError` for a token that doesn't verify, and
    `NotAMemberError` for a token that verifies but names a user with no
    active membership in `organization_id` — including a nonexistent
    organization, which reads identically rather than letting a caller
    enumerate real organization ids by watching which ones answer differently.
    """
    claims = decode_supabase_jwt(token)
    sub = claims.get("sub")
    if not sub:
        raise AuthenticationError("token has no 'sub' claim")
    try:
        user_id = UUID(str(sub))
    except ValueError as exc:
        raise AuthenticationError(f"token 'sub' claim is not a UUID: {sub!r}") from exc

    with pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_MEMBERSHIP, {"organization_id": organization_id, "user_id": user_id})
        row = cur.fetchone()
    if row is None:
        raise NotAMemberError(organization_id)

    return AuthContext(
        organization_id=organization_id, auth_method="jwt", user_id=user_id, role=row["role"]
    )


def resolve_api_key_context(pool: ConnectionPool, *, raw_key: str) -> AuthContext:
    """Verify an ARIE organization API key and build its `AuthContext`.

    A thin wrapper around `arie.apikeys.verify_api_key` — this module is
    where every request-time authentication path lives, so `arie.api.main`
    only ever imports from here, never reaching into `arie.apikeys` directly
    for the verification step (it still imports `arie.apikeys.looks_like_api_key`
    to decide which of the two paths to take at all).

    Propagates `InvalidApiKeyError`/`RevokedApiKeyError` unchanged — both are
    re-exported from this module so `arie.api.main` has one place to import
    every auth exception from.
    """
    verified = verify_api_key(pool, raw_key=raw_key)
    return AuthContext(
        organization_id=verified.organization_id,
        auth_method="api_key",
        api_key_id=verified.key_id,
        scopes=verified.scopes,
    )
