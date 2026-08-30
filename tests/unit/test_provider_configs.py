"""Organization provider configuration (Productization M4 Parts 3-4) — the
parts of `arie.provider_configs` that don't need a live database or Vault:
provider-name validation happens before any query is built, and
`ProviderStatus`'s two constructors are pure.

Vault-backed behavior (create/replace/delete, tenant isolation, RLS,
never-returns-a-secret) is covered by
tests/integration/test_provider_configs_integration.py instead — Supabase
Vault is not present on a vanilla local Postgres, so that file stands up a
functional stub first (see its own docstring).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import psycopg
import pytest

from arie.provider_configs import (
    SUPPORTED_PROVIDERS,
    InvalidProviderError,
    ProviderConfigRecord,
    ProviderStatus,
    delete_provider_config,
    get_provider_status,
    record_test_result,
    set_provider_credential,
    set_provider_enabled,
)

_UNUSED_CONN = cast(psycopg.Connection, None)
_UNUSED_ORG_ID = cast(UUID, "not-used")
_UNUSED_USER_ID = cast(UUID, "not-used")


def test_supported_providers_are_exactly_the_three_real_adapters() -> None:
    assert set(SUPPORTED_PROVIDERS) == {
        "abstract_company_enrichment",
        "hunter_combined_enrichment",
        "apollo_person_enrichment",
    }


def test_get_provider_status_rejects_an_unknown_provider_before_touching_the_connection() -> None:
    with pytest.raises(InvalidProviderError, match="unknown provider"):
        get_provider_status(_UNUSED_CONN, organization_id=_UNUSED_ORG_ID, provider="bogus")


def test_set_provider_credential_rejects_an_unknown_provider_before_touching_the_connection() -> (
    None
):
    with pytest.raises(InvalidProviderError, match="unknown provider"):
        set_provider_credential(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            provider="bogus",
            raw_credential="x",
            actor_user_id=_UNUSED_USER_ID,
        )


def test_set_provider_enabled_rejects_an_unknown_provider_before_touching_the_connection() -> None:
    with pytest.raises(InvalidProviderError, match="unknown provider"):
        set_provider_enabled(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            provider="bogus",
            enabled=True,
            actor_user_id=_UNUSED_USER_ID,
        )


def test_record_test_result_rejects_an_unknown_provider_before_touching_the_connection() -> None:
    with pytest.raises(InvalidProviderError, match="unknown provider"):
        record_test_result(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            provider="bogus",
            success=True,
            sanitized_error=None,
            actor_user_id=_UNUSED_USER_ID,
        )


def test_delete_provider_config_rejects_an_unknown_provider_before_touching_the_connection() -> (
    None
):
    with pytest.raises(InvalidProviderError, match="unknown provider"):
        delete_provider_config(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            provider="bogus",
            actor_user_id=_UNUSED_USER_ID,
        )


def test_provider_status_unconfigured_shape() -> None:
    status = ProviderStatus.unconfigured("hunter_combined_enrichment")
    assert status.configured is False
    assert status.enabled is False
    assert status.last_tested_at is None
    assert status.last_test_status is None


def test_provider_status_from_record_never_carries_vault_secret_id() -> None:
    record = ProviderConfigRecord(
        config_id=uuid4(),
        organization_id=uuid4(),
        provider="abstract_company_enrichment",
        enabled=True,
        vault_secret_id=uuid4(),
        created_by_user_id=uuid4(),
        updated_by_user_id=uuid4(),
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
        last_tested_at=None,
        last_test_status=None,
        last_test_error=None,
    )
    status = ProviderStatus.from_record(record)
    assert status.configured is True
    assert status.enabled is True
    assert not hasattr(status, "vault_secret_id")
