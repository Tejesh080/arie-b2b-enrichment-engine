"""Productization M6 Parts 2-9/17-19/25-26/36-38 — the billing domain against
a real database. Stripe's own network API is never called: webhook
signature verification is genuinely local HMAC (Stripe's own documented
scheme, reproduced by `_sign` below — no mock needed, no network), and
`start_checkout`/`open_billing_portal` monkeypatch the one seam that talks to
Stripe (`arie.billing.stripe_gateway`) so this suite proves ARIE's own
orchestration/persistence/idempotency logic without needing a Stripe test
account.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from collections.abc import Iterator
from typing import Any
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app
from tests.integration.test_rls_membership_recursion import TwoOrgFixture, _as
from tests.integration.test_rls_membership_recursion import rls_test_roles as rls_test_roles
from tests.integration.test_rls_membership_recursion import two_orgs as two_orgs

import arie.billing.service as billing_service
import arie.billing.stripe_gateway as stripe_gateway
from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.billing.repository import get_billing
from arie.config import StripeConfig
from arie.tenancy import LEGACY_ORGANIZATION_ID

pytestmark = pytest.mark.integration

_WEBHOOK_SECRET = "whsec_test_secret"
_PRICE_STARTER = "price_test_starter"
_PRICE_GROWTH = "price_test_growth"


@pytest.fixture(autouse=True)
def _stripe_test_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A fake-but-shaped Stripe config, everywhere the two billing modules
    read `STRIPE` from — never real credentials, never a network call for
    anything this fixture covers (webhook signature verification is pure
    local HMAC; Checkout/Portal creation is monkeypatched per-test where
    needed)."""
    config = StripeConfig(
        secret_key="sk_test_fake",
        webhook_secret=_WEBHOOK_SECRET,
        price_starter=_PRICE_STARTER,
        price_growth=_PRICE_GROWTH,
        price_pro="price_test_pro",
    )
    monkeypatch.setattr(billing_service, "STRIPE", config)
    monkeypatch.setattr(stripe_gateway, "STRIPE", config)


def _sign(payload: bytes, *, secret: str = _WEBHOOK_SECRET, timestamp: int | None = None) -> str:
    """Stripe's own documented webhook signature scheme
    (`t=<unix>,v1=<hmac-sha256 of "<unix>.<payload>">`) — genuinely local,
    no network call, so this is a real signature Stripe's own verifier
    (`stripe.Webhook.construct_event`, called unmodified by
    `arie.billing.stripe_gateway.construct_event`) accepts."""
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    signature = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


def _event(
    event_type: str, obj: dict[str, Any], *, event_id: str | None = None, created: int | None = None
) -> tuple[bytes, str, str]:
    """Build `(raw_body, event_id, signature_header)` for one fake Stripe
    event."""
    eid = event_id or f"evt_{uuid.uuid4().hex[:24]}"
    body = json.dumps(
        {
            "id": eid,
            "type": event_type,
            "created": created if created is not None else int(time.time()),
            "data": {"object": obj},
        }
    ).encode()
    return body, eid, _sign(body)


def _subscription_object(
    *,
    sub_id: str,
    customer: str,
    price_id: str,
    status: str = "active",
    cancel_at_period_end: bool = False,
    canceled_at: int | None = None,
) -> dict[str, Any]:
    return {
        "id": sub_id,
        "customer": customer,
        "status": status,
        "cancel_at_period_end": cancel_at_period_end,
        "canceled_at": canceled_at,
        "items": {
            "data": [
                {
                    "price": {"id": price_id},
                    "current_period_start": 1_735_689_600,
                    "current_period_end": 1_738_368_000,
                }
            ]
        },
    }


@pytest.fixture
def org(db_conn: psycopg.Connection) -> Iterator[UUID]:
    """A fresh organization — migration 0033's trigger gives it a
    `plan='starter'`/`status='none'` billing row automatically."""
    org_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (org_id, "Billing Test Org", f"billing-test-{org_id.hex[:10]}"),
        )
    db_conn.commit()
    try:
        yield org_id
    finally:
        with db_conn.cursor() as cur:
            # billing_webhook_events.organization_id has no ON DELETE clause
            # (default RESTRICT, migration 0030) — an event is durable
            # operator-debugging history even if the organization it names
            # is later gone, so this is a deliberate FK shape, not an
            # oversight. Tests that populate it must clear it themselves.
            cur.execute("DELETE FROM billing_webhook_events WHERE organization_id = %s", (org_id,))
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
        db_conn.commit()


def _link_customer(db_conn: psycopg.Connection, org_id: UUID, customer_id: str) -> None:
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET stripe_customer_id = %s WHERE organization_id = %s",
            (customer_id, org_id),
        )
    db_conn.commit()


def _client_as(
    app_state: AppState, *, organization_id: UUID, user_id: UUID, role: str
) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(organization_id=organization_id, auth_method="jwt", user_id=user_id, role=role),
    )
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------------ webhook security --


def test_invalid_signature_is_rejected(app_state: AppState, db_conn: psycopg.Connection) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, event_id, _ = _event("checkout.session.completed", {"id": "cs_test"})
    response = client.post(
        "/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=deadbeef"}
    )
    assert response.status_code == 400
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM billing_webhook_events WHERE event_id = %s", (event_id,))
        assert cur.fetchone() is None  # never durably recorded — signature didn't verify


def test_missing_signature_is_rejected(app_state: AppState) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, _, _ = _event("checkout.session.completed", {"id": "cs_test"})
    response = client.post("/billing/webhook", content=body)
    assert response.status_code == 400


def test_webhook_secret_never_appears_in_the_response(app_state: AppState) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, _, _ = _event("checkout.session.completed", {"id": "cs_test"})
    response = client.post(
        "/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=bad"}
    )
    assert _WEBHOOK_SECRET not in response.text


def test_unhandled_event_type_is_acknowledged_and_recorded_as_ignored(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, event_id, sig = _event("customer.updated", {"id": "cus_test"})
    response = client.post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert response.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT processing_status FROM billing_webhook_events WHERE event_id = %s", (event_id,)
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "ignored"


def test_unresolvable_organization_does_not_500(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    """A subscription event for a Stripe customer no organization is linked
    to (Part 4 — never mutate an arbitrary org from metadata alone) is
    durably recorded as failed, acknowledged with 200 (nothing about
    retrying would resolve a customer link that doesn't exist), and touches
    no organization's entitlements."""
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, event_id, sig = _event(
        "customer.subscription.updated",
        _subscription_object(sub_id="sub_orphan", customer="cus_unknown", price_id=_PRICE_STARTER),
    )
    response = client.post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert response.status_code == 200
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT processing_status, organization_id FROM billing_webhook_events WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "failed"
    assert row[1] is None


# --------------------------------------------------------- idempotency/ordering --


def test_duplicate_event_delivery_grants_entitlement_only_once(
    app_state: AppState, db_conn: psycopg.Connection, org: UUID
) -> None:
    _link_customer(db_conn, org, "cus_dup_test")
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, event_id, sig = _event(
        "customer.subscription.created",
        _subscription_object(sub_id="sub_dup", customer="cus_dup_test", price_id=_PRICE_STARTER),
        event_id="evt_dup_fixed",
    )

    first = client.post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    second = client.post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert first.status_code == 200
    assert second.status_code == 200

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM organization_audit_events "
            "WHERE organization_id = %s AND event_type = 'billing.subscription_activated'",
            (org,),
        )
        audit_count = cur.fetchone()
        cur.execute("SELECT count(*) FROM billing_webhook_events WHERE event_id = %s", (event_id,))
        event_count = cur.fetchone()
    assert audit_count is not None and audit_count[0] == 1
    assert event_count is not None and event_count[0] == 1


def test_a_stale_event_cannot_roll_subscription_state_backward(
    app_state: AppState, db_conn: psycopg.Connection, org: UUID
) -> None:
    _link_customer(db_conn, org, "cus_ordering_test")
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    now = int(time.time())

    newer_body, _, newer_sig = _event(
        "customer.subscription.updated",
        _subscription_object(
            sub_id="sub_order",
            customer="cus_ordering_test",
            price_id=_PRICE_STARTER,
            status="active",
        ),
        created=now,
    )
    assert (
        client.post(
            "/billing/webhook", content=newer_body, headers={"stripe-signature": newer_sig}
        ).status_code
        == 200
    )

    stale_body, _, stale_sig = _event(
        "customer.subscription.updated",
        _subscription_object(
            sub_id="sub_order",
            customer="cus_ordering_test",
            price_id=_PRICE_STARTER,
            status="past_due",
        ),
        created=now - 3600,  # an hour *before* the event already applied
    )
    assert (
        client.post(
            "/billing/webhook", content=stale_body, headers={"stripe-signature": stale_sig}
        ).status_code
        == 200
    )

    billing = get_billing(db_conn, organization_id=org)
    assert billing.status == "active"  # NOT rolled back to past_due


# ----------------------------------------------------------- entitlement sync --


def test_subscription_created_activates_the_plan_and_syncs_enforced_limits(
    app_state: AppState, db_conn: psycopg.Connection, org: UUID
) -> None:
    _link_customer(db_conn, org, "cus_activate_test")
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, _, sig = _event(
        "customer.subscription.created",
        _subscription_object(
            sub_id="sub_activate", customer="cus_activate_test", price_id=_PRICE_STARTER
        ),
    )
    response = client.post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert response.status_code == 200

    billing = get_billing(db_conn, organization_id=org)
    assert billing.plan == "starter"
    assert billing.status == "active"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT max_leads_per_month FROM organizations WHERE organization_id = %s", (org,)
        )
        row = cur.fetchone()
    from arie.billing.plans import PLAN_DEFINITIONS

    assert row is not None and row[0] == PLAN_DEFINITIONS["starter"].max_leads_per_month


def test_subscription_deleted_drops_the_organization_to_the_unsubscribed_floor(
    app_state: AppState, db_conn: psycopg.Connection, org: UUID
) -> None:
    _link_customer(db_conn, org, "cus_cancel_test")
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)

    activate_body, _, activate_sig = _event(
        "customer.subscription.created",
        _subscription_object(
            sub_id="sub_cancel", customer="cus_cancel_test", price_id=_PRICE_GROWTH
        ),
    )
    client.post(
        "/billing/webhook", content=activate_body, headers={"stripe-signature": activate_sig}
    )

    delete_body, _, delete_sig = _event(
        "customer.subscription.deleted",
        _subscription_object(
            sub_id="sub_cancel",
            customer="cus_cancel_test",
            price_id=_PRICE_GROWTH,
            status="canceled",
        ),
    )
    response = client.post(
        "/billing/webhook", content=delete_body, headers={"stripe-signature": delete_sig}
    )
    assert response.status_code == 200

    billing = get_billing(db_conn, organization_id=org)
    assert billing.status == "canceled"
    assert billing.plan == "growth"  # plan history preserved, not erased

    from arie.billing.plans import UNSUBSCRIBED

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT max_leads_per_month FROM organizations WHERE organization_id = %s", (org,)
        )
        row = cur.fetchone()
    assert row is not None and row[0] == UNSUBSCRIBED.max_leads_per_month


def test_unmapped_price_id_fails_the_event_without_mutating_entitlements(
    app_state: AppState, db_conn: psycopg.Connection, org: UUID
) -> None:
    _link_customer(db_conn, org, "cus_badprice_test")
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, event_id, sig = _event(
        "customer.subscription.created",
        _subscription_object(
            sub_id="sub_bad", customer="cus_badprice_test", price_id="price_never_configured"
        ),
    )
    response = client.post("/billing/webhook", content=body, headers={"stripe-signature": sig})
    assert response.status_code == 200  # acked; Stripe retrying would not fix a config problem

    billing = get_billing(db_conn, organization_id=org)
    assert billing.status == "none"  # unchanged
    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT processing_status, sanitized_error FROM billing_webhook_events WHERE event_id = %s",
            (event_id,),
        )
        row = cur.fetchone()
    assert row is not None and row[0] == "failed" and row[1]


# -------------------------------------------------------- checkout/portal ------


def test_start_checkout_persists_the_customer_id_and_audits(
    db_conn: psycopg.Connection, org: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stripe_gateway, "get_or_create_customer", lambda **kw: "cus_from_checkout")
    monkeypatch.setattr(
        stripe_gateway,
        "create_checkout_session",
        lambda **kw: stripe_gateway.CheckoutSession(
            session_id="cs_test_123", url="https://checkout.stripe.com/test"
        ),
    )
    actor_id = uuid.uuid4()
    url = billing_service.start_checkout(
        db_conn,
        organization_id=org,
        actor_user_id=actor_id,
        actor_email="owner@example.com",
        plan="starter",
        success_url="https://app.example/success",
        cancel_url="https://app.example/cancel",
    )
    assert url == "https://checkout.stripe.com/test"
    billing = get_billing(db_conn, organization_id=org)
    assert billing.stripe_customer_id == "cus_from_checkout"

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM organization_audit_events WHERE organization_id = %s "
            "AND event_type = 'billing.checkout_started'",
            (org,),
        )
        assert cur.fetchone() is not None


def test_open_billing_portal_requires_an_existing_stripe_customer(
    db_conn: psycopg.Connection, org: UUID
) -> None:
    with pytest.raises(billing_service.NoStripeCustomerError):
        billing_service.open_billing_portal(
            db_conn, organization_id=org, actor_user_id=uuid.uuid4()
        )


def test_open_billing_portal_never_mutates_entitlements(
    db_conn: psycopg.Connection, org: UUID, monkeypatch: pytest.MonkeyPatch
) -> None:
    _link_customer(db_conn, org, "cus_portal_test")
    monkeypatch.setattr(
        stripe_gateway,
        "create_portal_session",
        lambda **kw: stripe_gateway.PortalSession(url="https://billing.stripe.com/test"),
    )
    before = get_billing(db_conn, organization_id=org)
    billing_service.open_billing_portal(db_conn, organization_id=org, actor_user_id=uuid.uuid4())
    after = get_billing(db_conn, organization_id=org)
    assert before.plan == after.plan
    assert before.status == after.status


# --------------------------------------------------------------- API surface --


def test_billing_endpoint_requires_owner_or_admin(app_state: AppState, org: UUID) -> None:
    client = _client_as(
        app_state, organization_id=org, user_id=uuid.uuid4(), role="analyst_reviewer"
    )
    assert client.get("/billing").status_code == 403
    assert (
        client.post(
            "/billing/checkout",
            json={"plan": "starter", "success_url": "https://a", "cancel_url": "https://b"},
        ).status_code
        == 403
    )
    assert client.post("/billing/portal", json={}).status_code == 403


def test_billing_endpoint_rejects_an_unpurchasable_plan(app_state: AppState, org: UUID) -> None:
    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4(), role="owner")
    response = client.post(
        "/billing/checkout",
        json={"plan": "internal", "success_url": "https://a", "cancel_url": "https://b"},
    )
    assert response.status_code == 422


def test_billing_endpoint_shows_unsubscribed_floor_for_a_fresh_organization(
    app_state: AppState, org: UUID
) -> None:
    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4(), role="owner")
    response = client.get("/billing")
    assert response.status_code == 200
    body = response.json()
    assert body["billing"]["plan"] == "starter"
    assert body["billing"]["status"] == "none"
    assert body["entitlements"]["plan"] == "unsubscribed"
    assert body["entitlements"]["live_provider_feature_allowed"] is False


def test_legacy_organization_is_internal_and_unaffected_by_subscription_status(
    app_state: AppState,
) -> None:
    client = _client_as(
        app_state, organization_id=LEGACY_ORGANIZATION_ID, user_id=uuid.uuid4(), role="owner"
    )
    response = client.get("/billing")
    assert response.status_code == 200
    body = response.json()
    assert body["entitlements"]["plan"] == "internal"
    assert body["entitlements"]["live_provider_feature_allowed"] is True


def test_usage_endpoint_reports_plan_and_member_counts(app_state: AppState, org: UUID) -> None:
    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4(), role="owner")
    response = client.get("/organization/limits")
    assert response.status_code == 200
    body = response.json()
    assert body["plan"] == "unsubscribed"
    assert body["members_limit"] == 1  # UNSUBSCRIBED.max_members


# ------------------------------------------------------------------------- RLS --


def test_rls_owner_cannot_select_another_organizations_billing_row(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT organization_id FROM organization_billing WHERE organization_id = %s",
            (two_orgs.org_b,),
        )
        rows = cur.fetchall()
    assert rows == []


def test_rls_owner_can_select_their_own_billing_row(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute(
            "SELECT organization_id FROM organization_billing WHERE organization_id = %s",
            (two_orgs.org_a,),
        )
        rows = cur.fetchall()
    assert rows == [(two_orgs.org_a,)]


def test_rls_denies_all_access_to_billing_webhook_events(
    migrated_database_direct: str, two_orgs: TwoOrgFixture
) -> None:
    """RLS is enabled with zero policies on this table on purpose — see
    migration 0031's own docstring. No customer-facing path should ever see
    a row of it."""
    with (
        psycopg.connect(migrated_database_direct) as conn,
        conn.transaction(),
        conn.cursor() as cur,
    ):
        _as(cur, two_orgs.user_a)
        cur.execute("SELECT count(*) FROM billing_webhook_events")
        row = cur.fetchone()
    assert row is not None and row[0] == 0
