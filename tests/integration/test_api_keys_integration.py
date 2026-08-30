"""Productization M2A — organization API keys.

`api_client` authenticates as an owner of `LEGACY_ORGANIZATION_ID` (see
conftest.py), so every key-management call below is already an authorized
owner/admin session unless a test deliberately swaps in a different role or
auth method.

A key-authenticated request never goes through `api_client`'s dependency
override — that override exists to skip *JWT* verification for tests that
aren't about auth, but this file's whole point is exercising the real,
non-overridden API-key path end to end. Each such test builds its own
`TestClient(create_app(state=app_state))` (`raw_client`) and sends the raw
key as a plain bearer token.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import IngestCleanup, authorize_app

from arie.api.main import AppState, create_app
from arie.auth import AuthContext

pytestmark = pytest.mark.integration


def _create_key(
    api_client: TestClient,
    cleanup: list[uuid.UUID],
    *,
    label: str = "test key",
    scopes: list[str] | None = None,
) -> dict[str, Any]:
    response = api_client.post(
        "/api-keys", json={"label": label, "scopes": scopes if scopes is not None else []}
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    cleanup.append(uuid.UUID(body["key_id"]))
    return body


@pytest.fixture
def raw_client(app_state: AppState) -> Iterator[TestClient]:
    """A client with no auth override at all — every request goes through the
    real `get_auth_context`, JWT or API key alike."""
    with TestClient(create_app(state=app_state), raise_server_exceptions=False) as client:
        yield client


def _bearer(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


# --------------------------------------------------------------- creation --


def test_create_api_key_returns_the_raw_key_exactly_once(
    api_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    body = _create_key(
        api_client, cleanup_api_keys, label="n8n production", scopes=["leads:write", "leads:read"]
    )

    assert body["raw_key"].startswith("arie_")
    assert body["raw_key"].startswith(body["key_prefix"])
    assert body["label"] == "n8n production"
    assert set(body["scopes"]) == {"leads:write", "leads:read"}
    assert body["revoked_at"] is None
    assert body["last_used_at"] is None


def test_the_raw_key_is_never_persisted(
    api_client: TestClient, db_conn: psycopg.Connection, cleanup_api_keys: list[uuid.UUID]
) -> None:
    """The one thing this whole feature must never do: store or leak the raw
    value anywhere the created-key response doesn't already put it."""
    body = _create_key(api_client, cleanup_api_keys)
    key_id = uuid.UUID(body["key_id"])

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT key_hash, key_prefix FROM organization_api_keys WHERE key_id = %s", (key_id,)
        )
        row = cur.fetchone()
    assert row is not None
    key_hash, key_prefix = row
    assert key_hash != body["raw_key"]
    assert body["raw_key"] not in key_hash
    assert key_prefix == body["key_prefix"]
    # The response model has no column for it either — a raw_key leaking into
    # a future GET/list response would be a schema change, not silent.
    assert len(key_hash) == 64, "SHA-256 hex digest is 64 characters"


def test_an_unknown_scope_is_rejected_before_a_row_is_written(
    api_client: TestClient, db_conn: psycopg.Connection
) -> None:
    label = f"bad-{uuid.uuid4().hex[:8]}"
    response = api_client.post("/api-keys", json={"label": label, "scopes": ["leads:delete"]})
    assert response.status_code == 422

    with db_conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM organization_api_keys WHERE label = %s", (label,))
        count_row = cur.fetchone()
    assert count_row is not None and count_row[0] == 0


def test_list_api_keys_never_includes_the_raw_key_or_hash(
    api_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, label="listed key")

    response = api_client.get("/api-keys")
    assert response.status_code == 200
    items = response.json()
    match = next(item for item in items if item["key_id"] == created["key_id"])
    assert "raw_key" not in match
    assert "key_hash" not in match
    assert match["label"] == "listed key"


# -------------------------------------------------------------- revocation --


def test_revoke_api_key_is_idempotent(
    api_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys)
    key_id = created["key_id"]

    first = api_client.post(f"/api-keys/{key_id}/revoke")
    second = api_client.post(f"/api-keys/{key_id}/revoke")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["revoked_at"] == second.json()["revoked_at"]


def test_revoking_an_unknown_key_is_404(api_client: TestClient) -> None:
    response = api_client.post(f"/api-keys/{uuid.uuid4()}/revoke")
    assert response.status_code == 404


def test_a_revoked_key_can_no_longer_authenticate(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read"])
    api_client.post(f"/api-keys/{created['key_id']}/revoke")

    response = raw_client.get(f"/leads/{uuid.uuid4()}", headers=_bearer(created["raw_key"]))

    assert response.status_code == 401


def test_an_unknown_raw_key_is_rejected(raw_client: TestClient) -> None:
    response = raw_client.get(
        f"/leads/{uuid.uuid4()}", headers=_bearer(f"arie_{uuid.uuid4().hex}bogus")
    )
    assert response.status_code == 401


# ----------------------------------------------------- api-key auth itself --


def test_a_valid_api_key_authenticates_with_no_organization_header_at_all(
    api_client: TestClient,
    raw_client: TestClient,
    make_lead: Any,
    cleanup_api_keys: list[uuid.UUID],
) -> None:
    lead_id, _version = make_lead()  # under LEGACY_ORGANIZATION_ID, the key's own org
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read"])

    response = raw_client.get(f"/leads/{lead_id}", headers=_bearer(created["raw_key"]))

    assert response.status_code == 200
    assert response.json()["lead_id"] == str(lead_id)


def test_x_organization_id_header_is_ignored_for_api_key_authentication(
    api_client: TestClient,
    raw_client: TestClient,
    make_lead: Any,
    other_org: uuid.UUID,
    cleanup_api_keys: list[uuid.UUID],
) -> None:
    """The core claim of item 4: even a *valid-looking, different* organization
    header must not redirect an API key's authority — it authenticates as its
    own organization regardless of what the header says."""
    lead_id, _version = make_lead()  # under LEGACY_ORGANIZATION_ID
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read"])

    response = raw_client.get(
        f"/leads/{lead_id}",
        headers={**_bearer(created["raw_key"]), "X-Organization-Id": str(other_org)},
    )

    # Still resolves against the key's real organization (A), not the header's
    # claimed one (B) — if the header were trusted, this would 404 instead.
    assert response.status_code == 200
    assert response.json()["lead_id"] == str(lead_id)


def test_using_the_key_updates_last_used_at(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read"])
    assert created["last_used_at"] is None

    raw_client.get(f"/leads/{uuid.uuid4()}", headers=_bearer(created["raw_key"]))

    listing = api_client.get("/api-keys").json()
    match = next(item for item in listing if item["key_id"] == created["key_id"])
    assert match["last_used_at"] is not None


# ------------------------------------------------------------ scope checks --


def test_a_key_scoped_to_leads_read_cannot_write_leads(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read"])

    write = raw_client.post(
        "/leads",
        json={"source": "test", "email": f"nobody-{uuid.uuid4().hex[:8]}@example.test"},
        headers=_bearer(created["raw_key"]),
    )
    read = raw_client.get(f"/leads/{uuid.uuid4()}", headers=_bearer(created["raw_key"]))

    assert write.status_code == 403
    assert read.status_code == 404  # authorized to read; this lead just doesn't exist


def test_a_key_with_only_leads_scopes_cannot_touch_reviews(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read", "leads:write"])

    response = raw_client.get(f"/reviews/{uuid.uuid4()}", headers=_bearer(created["raw_key"]))

    assert response.status_code == 403


def test_a_key_with_only_reviews_scopes_cannot_touch_leads(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=["reviews:read", "reviews:write"])

    response = raw_client.get(f"/leads/{uuid.uuid4()}", headers=_bearer(created["raw_key"]))

    assert response.status_code == 403


def test_a_key_with_no_scopes_can_authenticate_but_do_nothing(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=[])

    response = raw_client.get(f"/leads/{uuid.uuid4()}", headers=_bearer(created["raw_key"]))

    assert response.status_code == 403  # not 401 — the key itself is valid


# --------------------------------------------------------- tenant isolation --


def test_an_api_key_cannot_reach_a_different_organizations_lead(
    api_client: TestClient,
    raw_client: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
    cleanup_api_keys: list[uuid.UUID],
    other_org: uuid.UUID,
) -> None:
    created = _create_key(api_client, cleanup_api_keys, scopes=["leads:read"])  # org A's key

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (source, organization_id) VALUES (%s, %s) RETURNING lead_id",
            ("tenancy-test", other_org),
        )
        row = cur.fetchone()
    assert row is not None
    db_conn.commit()
    other_org_lead_id = row[0]
    cleanup_ingest.lead_ids.append(other_org_lead_id)

    response = raw_client.get(f"/leads/{other_org_lead_id}", headers=_bearer(created["raw_key"]))

    assert response.status_code == 404


# -------------------------------------------------- key management is admin-only --


def test_creating_a_key_requires_an_owner_or_admin_jwt_session(app_state: AppState) -> None:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=uuid.uuid4(),
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="analyst_reviewer",
        ),
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.post("/api-keys", json={"label": "should fail", "scopes": []})

    assert response.status_code == 403


def test_an_api_key_cannot_create_list_or_revoke_api_keys(
    api_client: TestClient, raw_client: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    """No scope grants organization management — proven with a key holding
    every data-plane scope there is, so the refusal can only be explained by
    `AuthContext.is_org_admin()` excluding API-key auth outright."""
    created = _create_key(
        api_client,
        cleanup_api_keys,
        scopes=["leads:write", "leads:read", "reviews:read", "reviews:write"],
    )
    headers = _bearer(created["raw_key"])

    create = raw_client.post(
        "/api-keys", json={"label": "escalation attempt", "scopes": []}, headers=headers
    )
    listing = raw_client.get("/api-keys", headers=headers)
    revoke = raw_client.post(f"/api-keys/{created['key_id']}/revoke", headers=headers)

    assert create.status_code == 403
    assert listing.status_code == 403
    assert revoke.status_code == 403


def test_revoking_a_key_in_a_different_organization_is_404(
    api_client: TestClient, api_client_org_b: TestClient, cleanup_api_keys: list[uuid.UUID]
) -> None:
    created = _create_key(api_client, cleanup_api_keys)  # belongs to Organization A

    response = api_client_org_b.post(f"/api-keys/{created['key_id']}/revoke")

    assert response.status_code == 404
    # And Organization A's own view still shows it active.
    still_active = api_client.get("/api-keys").json()
    match = next(item for item in still_active if item["key_id"] == created["key_id"])
    assert match["revoked_at"] is None
