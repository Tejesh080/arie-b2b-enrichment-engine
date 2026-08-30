"""Productization M1 — tenant isolation and IDOR regression tests.

Two organizations are the whole point of this file: `LEGACY_ORGANIZATION_ID`
(what `api_client` authenticates as by default — see conftest.py) plays
"Organization A", and `other_org` below creates a second, real organization
row to play "Organization B". Every test proves a specific isolation claim
from the corrected tenancy boundary (`migrations/0012_organizations_and_members.sql`):

* `companies` stays global (shared canonical identity).
* `persons`, `evidence` (including company-entity rows), `leads`,
  `provider_calls`, `scores`, `human_reviews`, and `decision_receipts` are
  tenant-owned — no automatic cross-organization reuse, ever.
* Every existing customer endpoint's ownership check reads a cross-organization
  resource id as "not found," identically to a genuinely nonexistent one.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import jwt
import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import IngestCleanup

from arie.api.main import AppState, create_app
from arie.approval.workflow import request_review
from arie.config import SupabaseAuthConfig
from arie.core.types import Evidence, LeadStatus, ProviderStatus
from arie.evidence.store import PostgresEvidenceStore
from arie.identity.resolver import IdentityResolver
from arie.ledger.store import PostgresCostLedger
from arie.live.outcome_cache import ProviderOutcomeGuard
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)
_JWT_SECRET = "tenancy-isolation-test-secret-at-least-32-bytes"


def _unique_domain(label: str) -> str:
    return f"{label}-{uuid4().hex[:10]}.test"


def _sign(*, sub: str) -> str:
    return jwt.encode(
        {"sub": sub, "aud": "authenticated", "exp": datetime.now(UTC) + timedelta(hours=1)},
        _JWT_SECRET,
        algorithm="HS256",
    )


def _make_pending_review(
    conn: psycopg.Connection, cleanup: IngestCleanup, *, organization_id: UUID
) -> tuple[UUID, UUID]:
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO leads (source, status, organization_id) VALUES (%s, %s, %s) "
            "RETURNING lead_id, version",
            ("tenancy-test", str(LeadStatus.DECISION), organization_id),
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    lead_id, version = row
    cleanup.lead_ids.append(lead_id)

    pending = request_review(
        conn,
        lead_id=lead_id,
        organization_id=organization_id,
        expected_version=version,
        original_decision="auto_route",
    )
    conn.commit()
    return lead_id, pending.review_id


# --------------------------------------------------------- identity/evidence --


def test_two_organizations_never_share_person_identity(
    identity_resolver: IdentityResolver, other_org: UUID, cleanup_identity: Any
) -> None:
    """`persons` is tenant-owned even though `companies` (the identity both
    resolve through) is shared — the same email at the same company must
    resolve to two distinct rows for two different organizations."""
    domain = _unique_domain("crosstenant")
    email = f"shared@{domain}"
    cleanup_identity.domains.append(domain)
    cleanup_identity.emails.append(email)

    company = identity_resolver.resolve_company(domain=domain, name="Cross Tenant Co")
    person_a = identity_resolver.resolve_person(
        email=email, organization_id=LEGACY_ORGANIZATION_ID, company_id=company.company_id
    )
    person_b = identity_resolver.resolve_person(
        email=email, organization_id=other_org, company_id=company.company_id
    )

    assert person_a.person_id != person_b.person_id


def test_evidence_is_not_shared_across_organizations_even_for_the_same_company(
    evidence_store: PostgresEvidenceStore, other_org: UUID, cleanup_evidence: list[UUID]
) -> None:
    """The core regression for the corrected tenancy boundary: two
    organizations enriching the *same* (shared, global) company must each pay
    for and hold their own evidence — Organization B must never read
    Organization A's paid-for company facts."""
    company_id = uuid4()
    cleanup_evidence.append(company_id)

    evidence_store.put(
        Evidence(
            entity_type="company",
            entity_id=company_id,
            field_name="industry",
            value="fintech",
            source="dns_web",
            confidence=0.9,
            ttl_seconds=3600,
            fetched_at=NOW,
        ),
        organization_id=LEGACY_ORGANIZATION_ID,
    )

    from_org_a = evidence_store.get_all_fresh(
        "company",
        company_id,
        organization_id=LEGACY_ORGANIZATION_ID,
        now=NOW + timedelta(minutes=1),
    )
    from_org_b = evidence_store.get_all_fresh(
        "company", company_id, organization_id=other_org, now=NOW + timedelta(minutes=1)
    )

    assert {e.field_name for e in from_org_a} == {"industry"}
    assert from_org_b == ()


def test_recent_miss_is_not_suppressed_across_organizations(
    app_state: AppState, cost_ledger: PostgresCostLedger, other_org: UUID
) -> None:
    """Regression for the cross-tenant leak `arie.live.outcome_cache` had
    before its own `organization_id` filter was added: Organization A's MISS
    for a shared company must not silently suppress Organization B's
    legitimate, independent request for the same company."""
    entity_id = uuid4()
    cost_ledger.record_provider_call(
        idempotency_key=f"tenancy-test-{uuid4().hex}",
        provider="hunter",
        entity_type="company",
        entity_id=entity_id,
        status=ProviderStatus.MISS,
        cost_usd=0.0,
        latency_ms=10.0,
        organization_id=LEGACY_ORGANIZATION_ID,
    )

    guard = ProviderOutcomeGuard(app_state.pool)
    assert (
        guard.recent_miss("hunter", "company", entity_id, organization_id=LEGACY_ORGANIZATION_ID)
        is not None
    )
    assert guard.recent_miss("hunter", "company", entity_id, organization_id=other_org) is None


# -------------------------------------------------------------------- IDOR --


def test_get_lead_returns_404_for_a_lead_in_a_different_organization(
    api_client: TestClient,
    api_client_org_b: TestClient,
    make_lead: Callable[..., tuple[UUID, int]],
) -> None:
    lead_id, _version = make_lead()  # defaults to LEGACY_ORGANIZATION_ID, i.e. "Organization A"

    assert api_client.get(f"/leads/{lead_id}").status_code == 200
    other = api_client_org_b.get(f"/leads/{lead_id}")
    assert other.status_code == 404


def test_get_lead_receipt_returns_404_for_a_lead_in_a_different_organization(
    api_client: TestClient,
    api_client_org_b: TestClient,
    make_lead: Callable[..., tuple[UUID, int]],
) -> None:
    lead_id, _version = make_lead()

    assert api_client.get(f"/leads/{lead_id}/receipt").status_code == 200
    assert api_client_org_b.get(f"/leads/{lead_id}/receipt").status_code == 404


def test_get_review_returns_404_for_a_review_in_a_different_organization(
    api_client: TestClient,
    api_client_org_b: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
) -> None:
    _lead_id, review_id = _make_pending_review(
        db_conn, cleanup_ingest, organization_id=LEGACY_ORGANIZATION_ID
    )

    assert api_client.get(f"/reviews/{review_id}").status_code == 200
    assert api_client_org_b.get(f"/reviews/{review_id}").status_code == 404


def test_submit_review_decision_returns_404_for_a_review_in_a_different_organization(
    api_client_org_b: TestClient,
    db_conn: psycopg.Connection,
    cleanup_ingest: IngestCleanup,
) -> None:
    """The IDOR check that matters most: Organization B must not be able to
    *decide* a review that belongs to Organization A, not merely fail to read
    it."""
    _lead_id, review_id = _make_pending_review(
        db_conn, cleanup_ingest, organization_id=LEGACY_ORGANIZATION_ID
    )

    response = api_client_org_b.post(
        f"/reviews/{review_id}/decision",
        json={"action": "approve", "reviewer": "attacker@example.test", "expected_lead_version": 1},
    )

    assert response.status_code == 404


def test_two_organizations_can_reuse_the_same_source_and_external_ref(
    api_client: TestClient, api_client_org_b: TestClient, cleanup_ingest: IngestCleanup
) -> None:
    """The composite unique constraint (`0015_...sql`): two organizations'
    upstream CRMs legitimately reuse the same (source, external_ref) pair, and
    must land as two independent leads, never deduplicated against each other."""
    external_ref = f"crm-{uuid4().hex[:10]}"
    domain_a, domain_b = _unique_domain("tenancy-a"), _unique_domain("tenancy-b")
    payload_a = {"source": "shared-crm", "email": f"a@{domain_a}", "external_ref": external_ref}
    payload_b = {"source": "shared-crm", "email": f"b@{domain_b}", "external_ref": external_ref}

    resp_a = api_client.post("/leads", json=payload_a)
    resp_b = api_client_org_b.post("/leads", json=payload_b)

    assert resp_a.status_code == 201
    assert resp_b.status_code == 201
    assert resp_a.json()["lead_id"] != resp_b.json()["lead_id"]

    cleanup_ingest.lead_ids.append(UUID(resp_a.json()["lead_id"]))
    cleanup_ingest.lead_ids.append(UUID(resp_b.json()["lead_id"]))
    cleanup_ingest.emails.extend([payload_a["email"], payload_b["email"]])
    cleanup_ingest.domains.extend([domain_a, domain_b])


# --------------------------------------------------------- the auth boundary --
#
# Everything above authenticates through `api_client`'s dependency override
# (see its docstring) — deliberately, since these tests are about business-
# logic isolation, not token verification. The tests below build their own
# app with no override, so `get_auth_context` runs for real end to end:
# JWT verification (`arie.auth.decode_supabase_jwt`, unit-tested in isolation
# in `tests/unit/test_auth.py`) plus the live `organization_members` lookup.


def test_missing_authorization_header_is_rejected(app_state: AppState) -> None:
    app = create_app(state=app_state)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/leads/{uuid4()}", headers={"X-Organization-Id": str(LEGACY_ORGANIZATION_ID)}
        )
    assert response.status_code == 401


def test_missing_organization_header_is_rejected(
    app_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=_JWT_SECRET))
    token = _sign(sub=str(uuid4()))
    app = create_app(state=app_state)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(f"/leads/{uuid4()}", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 400


def test_a_user_with_no_membership_is_rejected_with_403(
    app_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A valid, correctly signed token for a user who simply isn't a member
    of the requested organization — the case a stolen or mismatched
    `X-Organization-Id` produces."""
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=_JWT_SECRET))
    token = _sign(sub=str(uuid4()))  # never inserted into organization_members
    app = create_app(state=app_state)
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            f"/leads/{uuid4()}",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Organization-Id": str(LEGACY_ORGANIZATION_ID),
            },
        )
    assert response.status_code == 403


def test_a_real_member_with_a_valid_token_is_authenticated(
    app_state: AppState, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End-to-end happy path for `resolve_auth_context`: a real
    `organization_members` row plus a correctly signed token authenticates —
    proven by reaching the *lead* lookup (404, the lead genuinely doesn't
    exist) rather than being turned away at the auth boundary (401/403)."""
    monkeypatch.setattr("arie.auth.SUPABASE_AUTH", SupabaseAuthConfig(jwt_secret=_JWT_SECRET))
    user_id = uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organization_members (organization_id, user_id, role) "
            "VALUES (%s, %s, 'analyst_reviewer')",
            (LEGACY_ORGANIZATION_ID, user_id),
        )
    db_conn.commit()

    try:
        token = _sign(sub=str(user_id))
        app = create_app(state=app_state)
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get(
                f"/leads/{uuid4()}",
                headers={
                    "Authorization": f"Bearer {token}",
                    "X-Organization-Id": str(LEGACY_ORGANIZATION_ID),
                },
            )
        assert response.status_code == 404
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM organization_members WHERE user_id = %s", (user_id,))
        db_conn.commit()
