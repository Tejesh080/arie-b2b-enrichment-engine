"""Productization M4 Parts 3-7 — organization provider configuration, Vault
secret storage, connection testing, and per-organization credential
resolution.

**Local Postgres has no `supabase_vault` extension** — `docker-compose.yml`
runs plain `postgres:16-alpine`, not a Supabase-managed image, so the real
`vault` schema (confirmed present in *production* Supabase — see
`arie.vault`'s own docstring) does not exist here. `vault_stub` below
creates a minimal, functionally-equivalent `vault` schema — same table/view
shape, same function signatures — the same "stand up locally what Supabase
provides for real" approach `test_rls_membership_recursion.py`'s
`_CREATE_AUTH_STUB` already uses for `auth.uid()`/`authenticated`/`anon`.
This stub does **not** encrypt (plain `TEXT` storage) — it exists to prove
`arie.vault`/`arie.provider_configs` call Vault's real function signatures
correctly and handle real Postgres round-trips, not to re-verify Vault's own
encryption, which this codebase does not own or implement.

Tests that call `arie.provider_configs`/`arie.credential_resolver` directly
against `db_conn` for a *fresh* organization insert a real `organizations`
row first (`_insert_org`) — `organization_provider_configs.organization_id`
has a real `REFERENCES organizations` FK, unlike the `AuthContext`-override
fixtures (`api_client_org_b`, `_client_as` below) which never insert one.
RLS tests use `two_orgs` instead, which already does.
"""

from __future__ import annotations

import uuid
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app
from tests.integration.test_rls_membership_recursion import TwoOrgFixture, _as
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

import arie.api.main as api_main
from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.credential_resolver import resolve_provider_credential
from arie.provider_configs import (
    delete_provider_config,
    set_provider_credential,
    set_provider_enabled,
)
from arie.provider_testing import ConnectionTestResult
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

_PROVIDER = "abstract_company_enrichment"

_CREATE_VAULT_STUB = """
CREATE SCHEMA IF NOT EXISTS vault;

CREATE TABLE IF NOT EXISTS vault.secrets (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name        TEXT,
    description TEXT DEFAULT '',
    secret      TEXT NOT NULL,
    key_id      UUID,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW vault.decrypted_secrets AS
    SELECT id, name, description, secret, secret AS decrypted_secret, key_id,
           created_at, updated_at
    FROM vault.secrets;

CREATE OR REPLACE FUNCTION vault.create_secret(
    new_secret text, new_name text DEFAULT NULL, new_description text DEFAULT '',
    new_key_id uuid DEFAULT NULL
) RETURNS uuid LANGUAGE plpgsql AS $$
DECLARE new_id uuid;
BEGIN
    INSERT INTO vault.secrets (secret, name, description, key_id)
    VALUES (new_secret, new_name, new_description, new_key_id)
    RETURNING id INTO new_id;
    RETURN new_id;
END;
$$;

CREATE OR REPLACE FUNCTION vault.update_secret(
    secret_id uuid, new_secret text DEFAULT NULL, new_name text DEFAULT NULL,
    new_description text DEFAULT NULL, new_key_id uuid DEFAULT NULL
) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    UPDATE vault.secrets SET
        secret = COALESCE(new_secret, secret),
        name = COALESCE(new_name, name),
        description = COALESCE(new_description, description),
        updated_at = now()
    WHERE id = secret_id;
END;
$$;
"""


@pytest.fixture(scope="session")
def vault_stub(migrated_database_direct: str) -> None:
    with psycopg.connect(migrated_database_direct, autocommit=True) as conn, conn.cursor() as cur:
        cur.execute(_CREATE_VAULT_STUB)


@pytest.fixture
def app_state_with_vault(app_state: AppState, vault_stub: None) -> AppState:
    return app_state


def _client_as(
    app_state: AppState, *, organization_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(organization_id=organization_id, auth_method="jwt", user_id=user_id, role=role),
    )
    return TestClient(app, raise_server_exceptions=False)


def _insert_org(db_conn: psycopg.Connection, organization_id: uuid.UUID) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (organization_id, "Provider Config Test Org", f"pc-test-{organization_id.hex[:10]}"),
        )
    db_conn.commit()


def _audit_events(db_conn: psycopg.Connection, organization_id: uuid.UUID) -> list[tuple[str, Any]]:
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT event_type, payload FROM organization_audit_events "
            "WHERE organization_id = %s ORDER BY event_id",
            (organization_id,),
        )
        return cur.fetchall()


# ------------------------------------------------------------------------ save --


def test_owner_can_configure_a_provider(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    record = set_provider_credential(
        db_conn,
        organization_id=LEGACY_ORGANIZATION_ID,
        provider=_PROVIDER,
        raw_credential="sk_test_abstract_12345",
        actor_user_id=uuid.uuid4(),
    )
    assert record.enabled is True
    assert record.provider == _PROVIDER
    assert not hasattr(record, "credential")
    assert not hasattr(record, "raw_credential")


def test_replacing_a_credential_reuses_the_same_vault_secret_id(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    actor = uuid.uuid4()

    first = set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=_PROVIDER,
        raw_credential="key-v1",
        actor_user_id=actor,
    )
    second = set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=_PROVIDER,
        raw_credential="key-v2",
        actor_user_id=actor,
    )

    assert first.config_id == second.config_id
    assert first.vault_secret_id == second.vault_secret_id

    resolved = resolve_provider_credential(db_conn, organization_id=org_id, provider=_PROVIDER)
    assert resolved == "key-v2"  # the OLD value is gone, not merely superseded


def test_configure_then_replace_are_audited_distinctly(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    actor = uuid.uuid4()

    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=_PROVIDER,
        raw_credential="k1",
        actor_user_id=actor,
    )
    set_provider_credential(
        db_conn,
        organization_id=org_id,
        provider=_PROVIDER,
        raw_credential="k2",
        actor_user_id=actor,
    )

    events = _audit_events(db_conn, org_id)
    event_types = [e[0] for e in events]
    assert "provider.configured" in event_types
    assert "provider.credential_replaced" in event_types
    for _, payload in events:
        assert "k1" not in str(payload)
        assert "k2" not in str(payload)


def test_configuring_an_unknown_provider_is_404(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault,
        organization_id=LEGACY_ORGANIZATION_ID,
        user_id=uuid.uuid4(),
        role="owner",
    )
    response = client.put("/organization/providers/not_a_real_provider", json={"credential": "x"})
    assert response.status_code == 404


# -------------------------------------------------------------------------- API --


def test_list_always_shows_all_three_providers(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault, organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role="owner"
    )
    response = client.get("/organization/providers")
    assert response.status_code == 200
    body = response.json()
    providers = {p["provider"] for p in body}
    assert providers == {
        "abstract_company_enrichment",
        "hunter_combined_enrichment",
        "apollo_person_enrichment",
    }
    assert all(p["configured"] is False for p in body)


def test_configure_read_enable_disable_delete_round_trip(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    client = _client_as(
        app_state_with_vault, organization_id=org_id, user_id=uuid.uuid4(), role="owner"
    )

    created = client.put(f"/organization/providers/{_PROVIDER}", json={"credential": "sk_abc"})
    assert created.status_code == 200
    body = created.json()
    assert body["configured"] is True
    assert body["enabled"] is True
    assert "credential" not in body
    assert "sk_abc" not in str(body)

    disabled = client.patch(f"/organization/providers/{_PROVIDER}", json={"enabled": False})
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False

    deleted = client.delete(f"/organization/providers/{_PROVIDER}")
    assert deleted.status_code == 204

    after = client.get(f"/organization/providers/{_PROVIDER}").json()
    assert after["configured"] is False


def test_enabling_an_unconfigured_provider_is_404(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault, organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role="owner"
    )
    response = client.patch(f"/organization/providers/{_PROVIDER}", json={"enabled": True})
    assert response.status_code == 404


def test_deleting_an_unconfigured_provider_is_404(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault, organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role="owner"
    )
    response = client.delete(f"/organization/providers/{_PROVIDER}")
    assert response.status_code == 404


# -------------------------------------------------------------------- authz --


def test_member_cannot_configure_a_provider(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault,
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="analyst_reviewer",
    )
    response = client.put(f"/organization/providers/{_PROVIDER}", json={"credential": "x"})
    assert response.status_code == 403


def test_member_can_read_provider_status(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault,
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="analyst_reviewer",
    )
    response = client.get("/organization/providers")
    assert response.status_code == 200


def test_an_api_key_cannot_manage_or_read_provider_config(
    api_client: TestClient, app_state_with_vault: AppState, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = api_client.post(
        "/api-keys", json={"label": "provider-probe", "scopes": ["leads:write"]}
    )
    assert created.status_code == 201
    cleanup_api_keys.append(uuid.UUID(created.json()["key_id"]))
    headers = {"Authorization": f"Bearer {created.json()['raw_key']}"}

    with TestClient(
        create_app(state=app_state_with_vault), raise_server_exceptions=False
    ) as raw_client:
        listed = raw_client.get("/organization/providers", headers=headers)
        write = raw_client.put(
            f"/organization/providers/{_PROVIDER}", json={"credential": "x"}, headers=headers
        )

    assert listed.status_code == 403
    assert write.status_code == 403


def test_anon_cannot_read_provider_config_via_rls(
    migrated_database_direct: str,
    vault_stub: None,
    two_orgs: TwoOrgFixture,
    db_conn: psycopg.Connection,
) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_provider_configs "
            "(organization_id, provider, vault_secret_id, created_by_user_id, updated_by_user_id) "
            "VALUES (%s, %s, gen_random_uuid(), %s, %s)",
            (two_orgs.org_a, _PROVIDER, two_orgs.user_a, two_orgs.user_a),
        )
    db_conn.commit()

    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, None)
        cur.execute("SELECT config_id FROM organization_provider_configs")
        rows = cur.fetchall()
    assert rows == []


# -------------------------------------------------------------- tenant isolation --


def test_organization_a_cannot_see_organization_bs_provider_config(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    _insert_org(db_conn, org_a)
    _insert_org(db_conn, org_b)

    client_b = _client_as(
        app_state_with_vault, organization_id=org_b, user_id=uuid.uuid4(), role="owner"
    )
    client_b.put(f"/organization/providers/{_PROVIDER}", json={"credential": "org-b-secret"})

    client_a = _client_as(
        app_state_with_vault, organization_id=org_a, user_id=uuid.uuid4(), role="owner"
    )
    status_a = client_a.get(f"/organization/providers/{_PROVIDER}").json()
    assert status_a["configured"] is False


def test_credential_resolver_never_returns_a_foreign_organizations_credential(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    """The critical tenant-isolation guarantee: configuring the SAME
    provider for two different organizations must never let one resolve
    the other's secret, even by accident."""
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    _insert_org(db_conn, org_a)
    _insert_org(db_conn, org_b)
    actor = uuid.uuid4()

    set_provider_credential(
        db_conn,
        organization_id=org_a,
        provider=_PROVIDER,
        raw_credential="ORG-A-SECRET",
        actor_user_id=actor,
    )
    set_provider_credential(
        db_conn,
        organization_id=org_b,
        provider=_PROVIDER,
        raw_credential="ORG-B-SECRET",
        actor_user_id=actor,
    )

    resolved_a = resolve_provider_credential(db_conn, organization_id=org_a, provider=_PROVIDER)
    resolved_b = resolve_provider_credential(db_conn, organization_id=org_b, provider=_PROVIDER)

    assert resolved_a == "ORG-A-SECRET"
    assert resolved_b == "ORG-B-SECRET"
    assert resolved_a != resolved_b


def test_credential_resolver_returns_none_for_an_unconfigured_organization(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    resolved = resolve_provider_credential(db_conn, organization_id=org_id, provider=_PROVIDER)
    assert resolved is None


def test_credential_resolver_returns_none_when_disabled(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    actor = uuid.uuid4()
    set_provider_credential(
        db_conn, organization_id=org_id, provider=_PROVIDER, raw_credential="k", actor_user_id=actor
    )
    set_provider_enabled(
        db_conn, organization_id=org_id, provider=_PROVIDER, enabled=False, actor_user_id=actor
    )

    resolved = resolve_provider_credential(db_conn, organization_id=org_id, provider=_PROVIDER)
    assert resolved is None


# ------------------------------------------------------------------------ delete --


def test_deleting_a_config_also_deletes_its_vault_secret(
    app_state_with_vault: AppState, db_conn: psycopg.Connection
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    actor = uuid.uuid4()

    record = set_provider_credential(
        db_conn, organization_id=org_id, provider=_PROVIDER, raw_credential="k", actor_user_id=actor
    )
    secret_id = record.vault_secret_id

    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM vault.secrets WHERE id = %s", (secret_id,))
        assert cur.fetchone() is not None

    deleted = delete_provider_config(
        db_conn, organization_id=org_id, provider=_PROVIDER, actor_user_id=actor
    )
    assert deleted is True

    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM vault.secrets WHERE id = %s", (secret_id,))
        assert cur.fetchone() is None  # no orphaned Vault secret


# ------------------------------------------------------------------- connection test --


def test_connection_test_endpoint_persists_a_sanitized_result(
    app_state_with_vault: AppState, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    client = _client_as(
        app_state_with_vault, organization_id=org_id, user_id=uuid.uuid4(), role="owner"
    )
    client.put(f"/organization/providers/{_PROVIDER}", json={"credential": "sk_test"})

    monkeypatch.setattr(
        api_main,
        "test_connection",
        lambda provider, raw_credential: ConnectionTestResult(
            success=False, sanitized_error="authentication_failed:401"
        ),
    )

    response = client.post(f"/organization/providers/{_PROVIDER}/test")

    assert response.status_code == 200
    body = response.json()
    assert body["last_test_status"] == "failure"
    assert body["last_test_error"] == "authentication_failed:401"
    assert body["last_tested_at"] is not None
    assert "sk_test" not in str(body)


def test_connection_test_success_is_persisted(
    app_state_with_vault: AppState, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    org_id = uuid.uuid4()
    _insert_org(db_conn, org_id)
    client = _client_as(
        app_state_with_vault, organization_id=org_id, user_id=uuid.uuid4(), role="owner"
    )
    client.put(f"/organization/providers/{_PROVIDER}", json={"credential": "sk_test"})

    monkeypatch.setattr(
        api_main,
        "test_connection",
        lambda provider, raw_credential: ConnectionTestResult(success=True, sanitized_error=None),
    )

    response = client.post(f"/organization/providers/{_PROVIDER}/test")

    assert response.status_code == 200
    assert response.json()["last_test_status"] == "success"
    assert response.json()["last_test_error"] is None


def test_testing_an_unconfigured_provider_is_404(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault, organization_id=uuid.uuid4(), user_id=uuid.uuid4(), role="owner"
    )
    response = client.post(f"/organization/providers/{_PROVIDER}/test")
    assert response.status_code == 404


def test_member_cannot_trigger_a_connection_test(app_state_with_vault: AppState) -> None:
    client = _client_as(
        app_state_with_vault,
        organization_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        role="analyst_reviewer",
    )
    response = client.post(f"/organization/providers/{_PROVIDER}/test")
    assert response.status_code == 403
