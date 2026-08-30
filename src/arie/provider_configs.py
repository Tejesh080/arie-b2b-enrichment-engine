"""Organization provider configuration (Productization M4 Parts 3-4). Owns
every piece of `organization_provider_configs`
(`migrations/0025_organization_provider_configs.sql`): configuring/
replacing/removing a BYOK credential, enabling/disabling, listing, and
recording connection-test results.

**A raw credential value passes through this module exactly once per call**
(`set_provider_credential`'s `raw_credential` parameter, forwarded straight
to `arie.vault.create_secret`/`update_secret`) and is never assigned to a
variable this module's own records retain — `ProviderConfigRecord` and
`ProviderStatus` carry `vault_secret_id`/metadata only, structurally
incapable of holding a secret value.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie import vault
from arie.audit import record_event
from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES

__all__ = [
    "SUPPORTED_PROVIDERS",
    "InvalidProviderError",
    "ProviderConfigRecord",
    "ProviderStatus",
    "delete_provider_config",
    "get_provider_status",
    "list_provider_statuses",
    "record_test_result",
    "set_provider_credential",
    "set_provider_enabled",
]

SUPPORTED_PROVIDERS: tuple[str, ...] = REGISTERED_LIVE_PROVIDER_NAMES
"""The only providers BYOK configuration accepts — re-exported from
`arie.live.providers` so callers of this module don't need to import that
one too. Identical set and order; never redeclared here, so the two can
never drift."""


class InvalidProviderError(ValueError):
    """`provider` is not one of :data:`SUPPORTED_PROVIDERS` — the same guard
    `organization_provider_configs.provider`'s own CHECK constraint
    enforces, checked here first so an unrecognised provider name never
    reaches a query (and never a Vault write)."""


@dataclass(frozen=True)
class ProviderConfigRecord:
    """A row that exists — `provider` has been configured at least once.
    Never a secret value; `vault_secret_id` is a pointer, not the credential."""

    config_id: UUID
    organization_id: UUID
    provider: str
    enabled: bool
    vault_secret_id: UUID
    created_by_user_id: UUID
    updated_by_user_id: UUID
    created_at: datetime
    updated_at: datetime
    last_tested_at: datetime | None
    last_test_status: str | None
    last_test_error: str | None


@dataclass(frozen=True)
class ProviderStatus:
    """The public, API-response shape for one provider — always present for
    every entry in :data:`SUPPORTED_PROVIDERS`, whether or not it has ever
    been configured, so a client can render a fixed 3-card list without
    special-casing "no row yet." Never a secret, never an encrypted value —
    `configured`/`enabled` are booleans, everything else is a plain status
    field. Built from a :class:`ProviderConfigRecord` when one exists
    (`from_record`), or synthesized as "never configured" when one doesn't.
    """

    provider: str
    configured: bool
    enabled: bool
    updated_at: datetime | None
    last_tested_at: datetime | None
    last_test_status: str | None
    last_test_error: str | None

    @classmethod
    def unconfigured(cls, provider: str) -> ProviderStatus:
        return cls(
            provider=provider,
            configured=False,
            enabled=False,
            updated_at=None,
            last_tested_at=None,
            last_test_status=None,
            last_test_error=None,
        )

    @classmethod
    def from_record(cls, record: ProviderConfigRecord) -> ProviderStatus:
        return cls(
            provider=record.provider,
            configured=True,
            enabled=record.enabled,
            updated_at=record.updated_at,
            last_tested_at=record.last_tested_at,
            last_test_status=record.last_test_status,
            last_test_error=record.last_test_error,
        )


def _row_to_record(row: Mapping[str, Any]) -> ProviderConfigRecord:
    return ProviderConfigRecord(
        config_id=row["config_id"],
        organization_id=row["organization_id"],
        provider=row["provider"],
        enabled=row["enabled"],
        vault_secret_id=row["vault_secret_id"],
        created_by_user_id=row["created_by_user_id"],
        updated_by_user_id=row["updated_by_user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        last_tested_at=row["last_tested_at"],
        last_test_status=row["last_test_status"],
        last_test_error=row["last_test_error"],
    )


def _require_known_provider(provider: str) -> None:
    if provider not in SUPPORTED_PROVIDERS:
        raise InvalidProviderError(
            f"unknown provider {provider!r} — must be one of {list(SUPPORTED_PROVIDERS)}"
        )


_CONFIG_COLUMNS = """
    config_id, organization_id, provider, enabled, vault_secret_id,
    created_by_user_id, updated_by_user_id, created_at, updated_at,
    last_tested_at, last_test_status, last_test_error
"""

_LOCK_ORG_PROVIDER = (
    "SELECT pg_advisory_xact_lock(hashtext(%(organization_id)s::text || ':' || %(provider)s))"
)
"""Serializes concurrent calls for the same (organization, provider) pair —
mirrors `arie.icp_profiles`/`arie.members`'s own locks. Without it, two
concurrent "save credential" calls for a provider with no existing row could
both see "not configured yet" and both attempt an INSERT, one losing to the
table's own UNIQUE constraint with a raw, unfriendly error instead of this
module's own clean create-vs-replace branch."""

_SELECT_ONE = f"""
    SELECT {_CONFIG_COLUMNS} FROM organization_provider_configs
    WHERE organization_id = %(organization_id)s AND provider = %(provider)s
"""

_SELECT_ALL_FOR_ORG = f"""
    SELECT {_CONFIG_COLUMNS} FROM organization_provider_configs
    WHERE organization_id = %(organization_id)s
"""

_INSERT_CONFIG = f"""
    INSERT INTO organization_provider_configs (
        organization_id, provider, enabled, vault_secret_id,
        created_by_user_id, updated_by_user_id
    ) VALUES (
        %(organization_id)s, %(provider)s, true, %(vault_secret_id)s,
        %(actor_user_id)s, %(actor_user_id)s
    )
    RETURNING {_CONFIG_COLUMNS}
"""

_UPDATE_SECRET_REPLACED = f"""
    UPDATE organization_provider_configs
    SET updated_by_user_id = %(actor_user_id)s, updated_at = now()
    WHERE organization_id = %(organization_id)s AND provider = %(provider)s
    RETURNING {_CONFIG_COLUMNS}
"""

_UPDATE_ENABLED = f"""
    UPDATE organization_provider_configs
    SET enabled = %(enabled)s, updated_by_user_id = %(actor_user_id)s, updated_at = now()
    WHERE organization_id = %(organization_id)s AND provider = %(provider)s
    RETURNING {_CONFIG_COLUMNS}
"""

_UPDATE_TEST_RESULT = f"""
    UPDATE organization_provider_configs
    SET last_tested_at = now(), last_test_status = %(status)s, last_test_error = %(error)s
    WHERE organization_id = %(organization_id)s AND provider = %(provider)s
    RETURNING {_CONFIG_COLUMNS}
"""

_DELETE_CONFIG = """
    DELETE FROM organization_provider_configs
    WHERE organization_id = %(organization_id)s AND provider = %(provider)s
    RETURNING vault_secret_id
"""


def get_provider_status(
    conn: psycopg.Connection, *, organization_id: UUID, provider: str
) -> ProviderStatus:
    """Raises :class:`InvalidProviderError` for a provider name outside
    :data:`SUPPORTED_PROVIDERS` — every *known* provider always has a status
    (`unconfigured` if no row exists), so this never returns `None`."""
    _require_known_provider(provider)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ONE, {"organization_id": organization_id, "provider": provider})
        row = cur.fetchone()
    if row is None:
        return ProviderStatus.unconfigured(provider)
    return ProviderStatus.from_record(_row_to_record(row))


def list_provider_statuses(
    conn: psycopg.Connection, *, organization_id: UUID
) -> list[ProviderStatus]:
    """One entry per :data:`SUPPORTED_PROVIDERS`, in that order, always —
    see :class:`ProviderStatus`'s own docstring for why an unconfigured
    provider still gets an entry rather than being omitted."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ALL_FOR_ORG, {"organization_id": organization_id})
        by_provider = {row["provider"]: _row_to_record(row) for row in cur.fetchall()}
    return [
        ProviderStatus.from_record(by_provider[provider])
        if provider in by_provider
        else ProviderStatus.unconfigured(provider)
        for provider in SUPPORTED_PROVIDERS
    ]


def set_provider_credential(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    provider: str,
    raw_credential: str,
    actor_user_id: UUID,
) -> ProviderConfigRecord:
    """Save (create, or replace an existing) credential and commit. The raw
    value goes straight to `arie.vault.create_secret`/`update_secret` and is
    never assigned anywhere else in this function.

    Creating: mints a new Vault secret, inserts the metadata row `enabled`
    by default, audits `provider.configured`. Replacing: updates the
    *existing* Vault secret in place (same `vault_secret_id` — see
    `arie.vault.update_secret`'s own docstring), leaves `enabled` exactly as
    it was, audits `provider.credential_replaced`. Both share one
    transaction with their Vault write — see this module's own docstring
    and `migrations/0025_organization_provider_configs.sql`'s.

    Raises :class:`InvalidProviderError` for an unrecognised provider.
    """
    _require_known_provider(provider)
    secret_name = f"arie:provider_credential:{organization_id}:{provider}"

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_LOCK_ORG_PROVIDER, {"organization_id": organization_id, "provider": provider})
        cur.execute(_SELECT_ONE, {"organization_id": organization_id, "provider": provider})
        existing = cur.fetchone()

        if existing is None:
            secret_id = vault.create_secret(conn, raw_value=raw_credential, name=secret_name)
            cur.execute(
                _INSERT_CONFIG,
                {
                    "organization_id": organization_id,
                    "provider": provider,
                    "vault_secret_id": secret_id,
                    "actor_user_id": actor_user_id,
                },
            )
            event_type = "provider.configured"
        else:
            vault.update_secret(
                conn,
                secret_id=existing["vault_secret_id"],
                raw_value=raw_credential,
                name=secret_name,
            )
            cur.execute(
                _UPDATE_SECRET_REPLACED,
                {
                    "organization_id": organization_id,
                    "provider": provider,
                    "actor_user_id": actor_user_id,
                },
            )
            event_type = "provider.credential_replaced"

        row = cur.fetchone()
    assert row is not None
    record = _row_to_record(row)

    # Safe payload: provider name only — never anything derived from the
    # credential itself.
    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type=event_type,
        payload={"provider": provider},
    )
    conn.commit()
    return record


def set_provider_enabled(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    provider: str,
    enabled: bool,
    actor_user_id: UUID,
) -> ProviderConfigRecord | None:
    """Toggle `enabled` on an existing config and commit. `None` if
    `provider` has never been configured for this organization — there is
    nothing to enable/disable yet.
    """
    _require_known_provider(provider)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _UPDATE_ENABLED,
            {
                "organization_id": organization_id,
                "provider": provider,
                "enabled": enabled,
                "actor_user_id": actor_user_id,
            },
        )
        row = cur.fetchone()
    if row is None:
        return None
    record = _row_to_record(row)
    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="provider.enabled" if enabled else "provider.disabled",
        payload={"provider": provider},
    )
    conn.commit()
    return record


def record_test_result(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    provider: str,
    success: bool,
    sanitized_error: str | None,
    actor_user_id: UUID,
) -> ProviderConfigRecord | None:
    """Persist a connection-test outcome and commit. `None` if `provider`
    has never been configured. `sanitized_error` must already have any
    secret/credential value stripped by the caller
    (`arie.provider_testing.test_connection`'s own contract) — this
    function does not, and cannot, tell a safe message from an unsafe one.
    """
    _require_known_provider(provider)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _UPDATE_TEST_RESULT,
            {
                "organization_id": organization_id,
                "provider": provider,
                "status": "success" if success else "failure",
                "error": None if success else sanitized_error,
            },
        )
        row = cur.fetchone()
    if row is None:
        return None
    record = _row_to_record(row)
    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="provider.connection_tested",
        payload={"provider": provider, "success": success},
    )
    conn.commit()
    return record


def delete_provider_config(
    conn: psycopg.Connection, *, organization_id: UUID, provider: str, actor_user_id: UUID
) -> bool:
    """Remove a provider's credential — the metadata row and its Vault
    secret together, one transaction (see this module's own docstring).
    Returns `False` if `provider` was never configured for this
    organization; never raises for that case.
    """
    _require_known_provider(provider)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_DELETE_CONFIG, {"organization_id": organization_id, "provider": provider})
        row = cur.fetchone()
    if row is None:
        return False
    vault.delete_secret(conn, secret_id=row["vault_secret_id"])

    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="provider.credential_removed",
        payload={"provider": provider},
    )
    conn.commit()
    return True
