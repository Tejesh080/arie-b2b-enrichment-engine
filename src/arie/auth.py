"""Authentication and tenant-scoping for the runtime API — Productization M1.

Verifies a Supabase-issued user session JWT and resolves the caller's active
membership in the organization named by the request's `X-Organization-Id`
header, producing an `AuthContext` every endpoint scopes its queries by.

**This is the primary tenant-isolation control, not a secondary one.** The
FastAPI backend connects to Postgres with a service-role/superuser connection
string (`arie.api.main.build_state`), which bypasses row-level security
entirely — see `migrations/0016_row_level_security.sql`'s own docstring. Every
`organization_id` filter added elsewhere in this codebase is only as safe as
this module correctly identifying who is calling and which organization they
belong to.

**No machine-credential mechanism exists yet.** `organization_api_keys` is
explicitly out of scope for this milestone, so every caller — a human in a
browser, or a machine integration like an n8n workflow — authenticates with
the same Supabase user JWT. A production n8n workflow will need a real user's
session token until API keys land in a later phase; that is a stated,
deliberate limitation of this milestone, not an oversight.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

import jwt
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.config import SUPABASE_AUTH

__all__ = [
    "ROLES",
    "AuthContext",
    "AuthenticationError",
    "NotAMemberError",
    "decode_supabase_jwt",
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
    for this milestone; the frontend repo is untouched).
    """

    user_id: UUID
    organization_id: UUID
    role: str


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

    return AuthContext(user_id=user_id, organization_id=organization_id, role=row["role"])
