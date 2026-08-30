"""Organization API keys — machine-to-machine authentication (Productization M2A).

Owns every piece of `organization_api_keys` (see
`migrations/0017_organization_api_keys.sql`): generating a new key,
persisting only its hash, listing and revoking keys for an organization, and
verifying a raw key presented on an incoming request.

**The raw key is never persisted, logged, or returned after creation.** Only
`key_hash` (SHA-256 of the raw key) and `key_prefix` (its first 12 characters,
not a secret) ever reach the database or a response body a second time —
`create_api_key`'s caller is the one and only place the raw value exists
outside a client's own storage, and it is the API layer's job (`arie.api.main`)
to put it in exactly one HTTP response and nowhere else, in particular never
into a log line or a tracing span attribute.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

__all__ = [
    "SCOPES",
    "ApiKeyRecord",
    "GeneratedApiKey",
    "InvalidApiKeyError",
    "RevokedApiKeyError",
    "VerifiedApiKey",
    "create_api_key",
    "generate_api_key",
    "list_api_keys",
    "looks_like_api_key",
    "revoke_api_key",
    "verify_api_key",
]

API_KEY_PREFIX = "arie_"
"""The literal tag every ARIE key starts with — `arie.api.main.get_auth_context`
uses it to tell an API key apart from a Supabase JWT on the same bearer-token
header without needing a second header or a client-declared token type."""

_PREFIX_STORED_LENGTH = 12
"""`len("arie_") + 7` random characters — enough entropy that `key_prefix`
functions as a fast, effectively-unique index lookup (collisions are handled
by retrying generation against the column's own UNIQUE constraint, see
`create_api_key`), while remaining far too short to matter if it leaked on
its own: the prefix alone cannot authenticate anything, only the full key's
hash can."""

SCOPES: tuple[str, ...] = ("leads:write", "leads:read", "reviews:read", "reviews:write")
"""Mirrors `organization_api_keys`'s `scopes` CHECK constraint — kept here too
so callers validating a requested scope list don't have to import a migration."""


class InvalidApiKeyError(Exception):
    """No active `organization_api_keys` row matches the presented raw key —
    covers both "no row has this prefix" and "a row has this prefix but its
    hash doesn't match," which are deliberately not distinguished: telling
    those apart would tell a guesser whether they found a real prefix."""


class RevokedApiKeyError(Exception):
    """The raw key verified against a real row, but that row has been revoked."""

    def __init__(self, key_id: UUID) -> None:
        self.key_id = key_id
        super().__init__(f"API key {key_id} has been revoked")


@dataclass(frozen=True)
class GeneratedApiKey:
    raw_key: str
    """Shown to the caller exactly once. Never persisted or logged."""
    key_prefix: str
    key_hash: str


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def generate_api_key() -> GeneratedApiKey:
    """A fresh, cryptographically random key: `secrets.token_urlsafe(32)` is
    256 bits of entropy, the same order of magnitude GitHub/Stripe-style
    tokens use — high enough that a plain SHA-256 digest (no salt, no slow
    KDF) is the correct hash for it. Salted/slow hashing (bcrypt, argon2)
    defends against a *low-entropy* human password being brute-forced from
    its hash; a 256-bit random token has no such weakness to defend against,
    and a slow hash would only add needless latency to every authenticated
    request.
    """
    raw_key = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
    return GeneratedApiKey(
        raw_key=raw_key, key_prefix=raw_key[:_PREFIX_STORED_LENGTH], key_hash=_hash(raw_key)
    )


def looks_like_api_key(token: str) -> bool:
    return token.startswith(API_KEY_PREFIX)


@dataclass(frozen=True)
class ApiKeyRecord:
    """Metadata only — never the raw key, never `key_hash`."""

    key_id: UUID
    organization_id: UUID
    label: str
    key_prefix: str
    scopes: tuple[str, ...]
    created_by_user_id: UUID
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


@dataclass(frozen=True)
class VerifiedApiKey:
    key_id: UUID
    organization_id: UUID
    scopes: frozenset[str]


_INSERT_API_KEY = """
    INSERT INTO organization_api_keys (
        organization_id, label, key_prefix, key_hash, scopes, created_by_user_id
    ) VALUES (
        %(organization_id)s, %(label)s, %(key_prefix)s, %(key_hash)s, %(scopes)s,
        %(created_by_user_id)s
    )
    RETURNING key_id, organization_id, label, key_prefix, scopes, created_by_user_id,
              created_at, last_used_at, revoked_at
"""

_SELECT_API_KEYS_FOR_ORG = """
    SELECT key_id, organization_id, label, key_prefix, scopes, created_by_user_id,
           created_at, last_used_at, revoked_at
    FROM organization_api_keys
    WHERE organization_id = %(organization_id)s
    ORDER BY created_at DESC
"""

_SELECT_API_KEY_BY_PREFIX = """
    SELECT key_id, organization_id, key_hash, scopes, revoked_at
    FROM organization_api_keys
    WHERE key_prefix = %(key_prefix)s
"""

_SELECT_API_KEY_BY_ID = """
    SELECT key_id, organization_id, label, key_prefix, scopes, created_by_user_id,
           created_at, last_used_at, revoked_at
    FROM organization_api_keys
    WHERE key_id = %(key_id)s AND organization_id = %(organization_id)s
"""

# `AND revoked_at IS NULL` makes this compare-and-swap idempotent by
# construction, the same idiom `arie.approval.workflow._COMPLETE_REVIEW`
# uses: a second revoke of an already-revoked key matches zero rows here
# (RETURNING nothing) rather than erroring, and `revoke_api_key` falls back to
# a plain SELECT to report its already-revoked state instead of treating the
# retry as a failure.
_REVOKE_API_KEY = """
    UPDATE organization_api_keys
    SET revoked_at = now()
    WHERE key_id = %(key_id)s AND organization_id = %(organization_id)s AND revoked_at IS NULL
    RETURNING key_id, organization_id, label, key_prefix, scopes, created_by_user_id,
              created_at, last_used_at, revoked_at
"""

_TOUCH_LAST_USED = "UPDATE organization_api_keys SET last_used_at = now() WHERE key_id = %(key_id)s"


def _row_to_record(row: dict[str, object]) -> ApiKeyRecord:
    return ApiKeyRecord(
        key_id=row["key_id"],  # type: ignore[arg-type]
        organization_id=row["organization_id"],  # type: ignore[arg-type]
        label=row["label"],  # type: ignore[arg-type]
        key_prefix=row["key_prefix"],  # type: ignore[arg-type]
        scopes=tuple(row["scopes"]),  # type: ignore[arg-type]
        created_by_user_id=row["created_by_user_id"],  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        last_used_at=row["last_used_at"],  # type: ignore[arg-type]
        revoked_at=row["revoked_at"],  # type: ignore[arg-type]
    )


def create_api_key(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    created_by_user_id: UUID,
    label: str,
    scopes: Sequence[str],
) -> tuple[ApiKeyRecord, str]:
    """Create a key and commit. Returns `(record, raw_key)` — `raw_key` exists
    only in this return value; the caller (`arie.api.main`) must put it in the
    creation response and nowhere else.

    Retries generation (up to 5 times) on a `key_prefix` collision — the
    astronomically unlikely case two independently generated keys share their
    first 12 characters. Each retry rolls back first: a `UniqueViolation`
    aborts the current transaction, and any statement on a caller-shared
    connection after that must start clean.
    """
    for _ in range(5):
        generated = generate_api_key()
        try:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    _INSERT_API_KEY,
                    {
                        "organization_id": organization_id,
                        "label": label,
                        "key_prefix": generated.key_prefix,
                        "key_hash": generated.key_hash,
                        "scopes": list(scopes),
                        "created_by_user_id": created_by_user_id,
                    },
                )
                row = cur.fetchone()
            assert row is not None
            conn.commit()
            return _row_to_record(row), generated.raw_key
        except psycopg.errors.UniqueViolation:
            conn.rollback()
            continue
    raise RuntimeError("failed to generate a unique API key prefix after 5 attempts")


def list_api_keys(conn: psycopg.Connection, *, organization_id: UUID) -> list[ApiKeyRecord]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_API_KEYS_FOR_ORG, {"organization_id": organization_id})
        rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def revoke_api_key(
    conn: psycopg.Connection, *, organization_id: UUID, key_id: UUID
) -> ApiKeyRecord | None:
    """Revoke a key and commit. `None` only when no key with this id exists
    for this organization — including one that exists in a *different*
    organization, which reads identically (see `_SELECT_API_KEY_BY_ID`'s own
    `organization_id` filter) rather than letting a caller distinguish
    "wrong organization" from "doesn't exist," the same IDOR-safe shape every
    other ownership check in this codebase uses.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_REVOKE_API_KEY, {"key_id": key_id, "organization_id": organization_id})
        row = cur.fetchone()
    if row is not None:
        conn.commit()
        return _row_to_record(row)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_API_KEY_BY_ID, {"key_id": key_id, "organization_id": organization_id})
        row = cur.fetchone()
    conn.commit()
    return _row_to_record(row) if row is not None else None


def verify_api_key(pool: ConnectionPool, *, raw_key: str) -> VerifiedApiKey:
    """Authenticate a raw key presented on an incoming request.

    `organization_id` comes from the matched row alone — never from any
    header the caller supplies, which is the whole point: a machine
    credential's organization cannot be spoofed by sending a different
    `X-Organization-Id`, because API-key authentication never reads it.

    The hash comparison is constant-time (`hmac.compare_digest`) so a timing
    side-channel can't help an attacker distinguish "wrong prefix" from
    "right prefix, wrong key" one byte at a time. `last_used_at` is touched
    best-effort in the same connection/transaction as the verifying read —
    unlike the cost ledger's deliberate "commit independently" rule
    (`arie.ledger.store`), there is no money at stake here, so keeping it in
    one round trip is simpler and loses nothing.
    """
    key_prefix = raw_key[:_PREFIX_STORED_LENGTH]
    with pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_API_KEY_BY_PREFIX, {"key_prefix": key_prefix})
            row = cur.fetchone()
        if row is None or not hmac.compare_digest(row["key_hash"], _hash(raw_key)):
            raise InvalidApiKeyError()
        if row["revoked_at"] is not None:
            raise RevokedApiKeyError(row["key_id"])

        with conn.cursor() as cur:
            cur.execute(_TOUCH_LAST_USED, {"key_id": row["key_id"]})
        conn.commit()

    return VerifiedApiKey(
        key_id=row["key_id"],
        organization_id=row["organization_id"],
        scopes=frozenset(row["scopes"]),
    )
