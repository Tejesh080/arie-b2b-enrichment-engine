"""Supabase Vault — the only place a provider credential's raw value is ever
written to or read from the database (Productization M4 Part 4).

**Verified privilege boundary (production, 2026-08-31):** the
`supabase_vault` extension is enabled; `vault.secrets` and
`vault.decrypted_secrets` grant `SELECT`/`DELETE` to `postgres` and
`service_role` only — `authenticated` and `anon` (the roles a direct-to-
Supabase client, e.g. PostgREST, ever runs as) have **no** grant on either.
`vault.create_secret`/`vault.update_secret` show the same split
(`has_function_privilege` confirmed `EXECUTE` for `postgres`/`service_role`,
denied for `authenticated`/`anon`). ARIE's backend connects as `postgres`
(`arie.api.main.build_state`), so this module's functions work; nothing
reachable through a browser or a direct Supabase client session ever can,
by construction — not by an application-level check this module could get
wrong.

**No custom cryptography.** Every function here is a thin wrapper around a
Vault-provided primitive (`vault.create_secret`, `vault.update_secret`, a
plain `SELECT`/`DELETE` against `vault.secrets`/`vault.decrypted_secrets`) —
this module owns no encryption, key management, or hashing of its own.

**Same transaction as the caller's metadata write.** Every function here
takes an already-open `conn` and never calls `conn.commit()` itself —
Vault is ordinary tables/functions in the *same* Postgres database, not an
external service, so `arie.provider_configs.set_provider_credential` can
create/replace a secret and write `organization_provider_configs`' metadata
row in one real transaction, genuinely atomic, not "as close as practical."
"""

from __future__ import annotations

from uuid import UUID

import psycopg

__all__ = ["create_secret", "delete_secret", "resolve_secret", "update_secret"]

_CREATE_SECRET = "SELECT vault.create_secret(%(raw_value)s, %(name)s) AS secret_id"
_UPDATE_SECRET = "SELECT vault.update_secret(%(secret_id)s, %(raw_value)s, %(name)s)"
_DELETE_SECRET = "DELETE FROM vault.secrets WHERE id = %(secret_id)s"
_RESOLVE_SECRET = "SELECT decrypted_secret FROM vault.decrypted_secrets WHERE id = %(secret_id)s"


def create_secret(conn: psycopg.Connection, *, raw_value: str, name: str) -> UUID:
    """Store a new secret and return its Vault-assigned id. `name` is
    metadata only (shown in a Supabase dashboard listing, never the secret
    value itself) — callers pass a non-sensitive identifying label, e.g.
    `f"arie:provider_credential:{organization_id}:{provider}"`, never
    anything derived from the raw value.
    """
    with conn.cursor() as cur:
        cur.execute(_CREATE_SECRET, {"raw_value": raw_value, "name": name})
        row = cur.fetchone()
    assert row is not None
    return row[0]  # type: ignore[no-any-return]


def update_secret(conn: psycopg.Connection, *, secret_id: UUID, raw_value: str, name: str) -> None:
    """Replace an existing secret's value in place — the same Vault row id
    is kept, so `organization_provider_configs.vault_secret_id` never has
    to change on a credential replacement."""
    with conn.cursor() as cur:
        cur.execute(_UPDATE_SECRET, {"secret_id": secret_id, "raw_value": raw_value, "name": name})


def delete_secret(conn: psycopg.Connection, *, secret_id: UUID) -> None:
    """Permanently remove a secret. Not idempotent-checked (a missing id is
    simply zero rows deleted) — callers that need to know whether anything
    existed should check their own metadata row instead."""
    with conn.cursor() as cur:
        cur.execute(_DELETE_SECRET, {"secret_id": secret_id})


def resolve_secret(conn: psycopg.Connection, *, secret_id: UUID) -> str | None:
    """The raw, decrypted secret value — `None` if `secret_id` doesn't exist
    (already deleted, or never valid). This is the one function in this
    codebase that a caller may hold the return value of only as long as it
    takes to use it: never log it, never put it in a response body, never
    persist it anywhere outside Vault itself.
    """
    with conn.cursor() as cur:
        cur.execute(_RESOLVE_SECRET, {"secret_id": secret_id})
        row = cur.fetchone()
    return row[0] if row is not None else None
