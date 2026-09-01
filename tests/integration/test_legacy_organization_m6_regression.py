"""Productization M6 — the Legacy Organization is not a customer, and M6
must not have turned it into one.

`LEGACY_ORGANIZATION_ID` is the well-known tenant every pre-M4 row was
backfilled into (`migrations/0014`): it is the *deployed system's own*
organization, carrying the existing production data, the demo, the n8n
workflows, and every API key minted before multi-tenancy existed. It has no
Stripe customer, will never check out, and must never be asked to.

M6 introduced entitlement gates in five places — member quota, live-provider
feature, lead/CSV/spend ceilings, execution-mode changes, and provider
credential writes. Each of those is a new way for a milestone about *selling*
the product to break the instance already running it. The grandfathering is
one word in one migration (`0030`'s `CASE WHEN organization_id = ... THEN
'internal'`), which is exactly the kind of thing a later refactor silently
drops; this file is what fails loudly if it ever does.

Deliberately written against the *behavior* (HTTP status codes, resolved
entitlements, enforcement outcomes) rather than the backfill SQL, because
"the row still says internal" is not the property that matters — "the
production tenant is not gated" is.

Requires TEST_DATABASE_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

import uuid

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import IngestCleanup, authorize_app, source_for

from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.billing.plans import (
    PLAN_DEFINITIONS,
    UNSUBSCRIBED,
    enforce_member_quota,
    is_live_provider_feature_allowed,
    resolve_organization_entitlements,
    sync_organization_limits,
)
from arie.billing.repository import get_billing
from arie.limits import get_limits
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration


def _owner_client(app_state: AppState) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=LEGACY_ORGANIZATION_ID,
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="owner",
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------ grandfathering --


def test_the_legacy_organization_has_a_billing_row_on_the_internal_plan(
    db_conn: psycopg.Connection,
) -> None:
    """Migration 0030's backfill, still standing. `internal` is not in
    `PURCHASABLE_PLANS`, so nothing in the product can move it off this plan
    by accident — only a deliberate UPDATE could."""
    billing = get_billing(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert billing.plan == "internal"
    assert billing.stripe_customer_id is None
    assert billing.stripe_subscription_id is None


def test_the_legacy_organization_resolves_to_the_internal_entitlements(
    db_conn: psycopg.Connection,
) -> None:
    entitlements = resolve_organization_entitlements(
        db_conn, organization_id=LEGACY_ORGANIZATION_ID
    )
    assert entitlements == PLAN_DEFINITIONS["internal"]
    assert entitlements != UNSUBSCRIBED


def test_internal_entitlements_survive_a_non_subscribed_status(
    db_conn: psycopg.Connection,
) -> None:
    """`internal` short-circuits *before* subscription status is consulted.

    The legacy row itself carries `status='active'` (migration 0030's
    backfill wrote that so the record reads coherently, not because Stripe
    ever said so), which means asserting against it alone cannot distinguish
    "grandfathered" from "happens to look subscribed". A throwaway
    organization forced to `internal`/`canceled` isolates the branch that
    actually matters: if someone ever reorders `resolve_organization_
    entitlements` to check `is_subscribed` first, a status Stripe never
    granted would drop the production tenant to 25 leads a month.

    Done on a temporary organization rather than by mutating the legacy row,
    which other tests in this session read.
    """
    org_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (org_id, "Grandfathered Probe", f"grandfathered-{org_id.hex[:10]}"),
        )
        cur.execute(
            "UPDATE organization_billing SET plan = 'internal', status = 'canceled' "
            "WHERE organization_id = %s",
            (org_id,),
        )
    db_conn.commit()
    try:
        assert (
            resolve_organization_entitlements(db_conn, organization_id=org_id)
            == PLAN_DEFINITIONS["internal"]
        )
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
        db_conn.commit()


def test_internal_ceilings_match_the_pre_m6_defaults() -> None:
    """`internal` was chosen to reproduce the exact numbers M4 hard-coded as
    every organization's default (`migrations/0026`). Asserting the values
    here means a future edit to the plan table cannot quietly shrink the
    production tenant's ceilings while every other test still passes."""
    internal = PLAN_DEFINITIONS["internal"]
    assert internal.max_leads_per_month == 5000
    assert internal.max_csv_rows_per_upload == 200
    assert internal.max_modeled_spend_usd_per_month == 50.0
    assert internal.max_members == 25
    assert internal.live_provider_feature_allowed is True


def test_syncing_limits_does_not_shrink_the_legacy_organization(
    db_conn: psycopg.Connection,
) -> None:
    """`sync_organization_limits` writes the resolved plan's ceilings onto
    `organizations`, and M6 calls it on every billing change. Running it
    against the legacy org must be a no-op in effect — the failure it guards
    against is a webhook (or a manual re-sync) reaching this tenant and
    downgrading the live deployment's own limits.
    """
    before = get_limits(db_conn, organization_id=LEGACY_ORGANIZATION_ID)

    sync_organization_limits(db_conn, organization_id=LEGACY_ORGANIZATION_ID)

    after = get_limits(db_conn, organization_id=LEGACY_ORGANIZATION_ID)
    assert after.max_leads_per_month == before.max_leads_per_month == 5000
    assert after.max_csv_rows_per_upload == before.max_csv_rows_per_upload == 200
    assert after.max_modeled_spend_usd_per_month == before.max_modeled_spend_usd_per_month == 50.0


# ------------------------------------------------------ the five M6 gates ---


def test_the_member_quota_gate_does_not_fire(db_conn: psycopg.Connection) -> None:
    """25 members, and the deployment has a handful. Calling the enforcer
    directly (rather than through an invitation) is the point: it proves the
    gate resolves to "allowed" rather than merely that the org happens not to
    have hit a lower ceiling yet."""
    enforce_member_quota(db_conn, organization_id=LEGACY_ORGANIZATION_ID)


def test_the_live_provider_feature_gate_does_not_fire(db_conn: psycopg.Connection) -> None:
    assert is_live_provider_feature_allowed(db_conn, organization_id=LEGACY_ORGANIZATION_ID)


def test_provider_credential_writes_are_not_entitlement_blocked(app_state: AppState) -> None:
    """M6 Part 20 added a 402 in front of provider credential writes. A 402
    here would mean the deployment could no longer configure the provider it
    already uses. Any other outcome (including a Vault-related failure in an
    environment with no Vault configured) is out of this test's scope — what
    it rules out is specifically the *entitlement* refusal.
    """
    client = _owner_client(app_state)

    response = client.put(
        "/organization/providers/abstract_company_enrichment",
        json={"credential": "legacy-regression-not-a-real-key"},
    )

    assert response.status_code != 402, response.text

    if response.status_code < 300:
        client.delete("/organization/providers/abstract_company_enrichment")


def test_execution_mode_changes_are_not_entitlement_blocked(app_state: AppState) -> None:
    """Same gate, other side. This deliberately sets `simulated` — the mode
    production is already in — so the request is a real, accepted
    execution-mode change that cannot enable live spend even if it
    unexpectedly succeeded in an unintended way. M5's own execution-safety
    guards are untouched and out of scope here.
    """
    client = _owner_client(app_state)

    response = client.patch("/organization/execution-mode", json={"execution_mode": "simulated"})

    assert response.status_code != 402, response.text
    assert response.status_code == 200, response.text
    assert response.json()["execution_mode"] == "simulated"


def test_lead_ingestion_still_works_under_the_internal_plan(
    api_client: TestClient, cleanup_ingest: IngestCleanup
) -> None:
    """The one flow the entire deployment exists to run. M6 put an
    entitlement-derived quota in front of it; `api_client` authenticates as
    an owner of the legacy organization, so this is that flow, unchanged."""
    domain = f"legacy-regression-{uuid.uuid4().hex[:10]}.test"
    email = f"ada@{domain}"
    cleanup_ingest.domains.append(domain)
    cleanup_ingest.emails.append(email)

    response = api_client.post(
        "/leads",
        json={
            "source": source_for("legacy-regression"),
            "email": email,
            "company_domain": domain,
            "external_ref": f"legacy-{uuid.uuid4().hex[:10]}",
        },
    )

    assert response.status_code == 201, response.text
    cleanup_ingest.lead_ids.append(uuid.UUID(response.json()["lead_id"]))


# ------------------------------------------------- the commercial surfaces --


def test_the_billing_page_is_readable_and_shows_no_stripe_relationship(
    app_state: AppState,
) -> None:
    """An operator opening Settings on the deployed instance must see a
    coherent page, not a 500 from code that assumes a Stripe customer."""
    response = _owner_client(app_state).get("/billing")

    assert response.status_code == 200
    body = response.json()
    assert body["billing"]["plan"] == "internal"
    assert body["billing"]["stripe_customer_id"] is None
    assert body["entitlements"]["plan"] == "internal"
    assert body["entitlements"]["live_provider_feature_allowed"] is True


def test_the_portal_is_refused_cleanly_rather_than_crashing(app_state: AppState) -> None:
    """There is no Stripe customer to open a Portal for. The requirement is
    that this is a clean 4xx an interface can render, never a 5xx — the
    frontend hides the button for `internal`, but an API is not allowed to
    depend on its client hiding things."""
    response = _owner_client(app_state).post("/billing/portal", json={})

    assert 400 <= response.status_code < 500, response.text


def test_usage_reports_the_internal_plan(app_state: AppState) -> None:
    body = _owner_client(app_state).get("/organization/limits").json()

    assert body["plan"] == "internal"
    assert body["members_limit"] == 25
    assert body["leads_limit"] == 5000
    assert body["max_csv_rows_per_upload"] == 200
