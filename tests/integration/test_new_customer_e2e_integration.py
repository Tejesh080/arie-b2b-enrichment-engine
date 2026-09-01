"""Productization M6 Part 45 — one test proving the whole self-service
commercial lifecycle end to end, against a real database, with no real
Stripe account or money: signup -> organization creation -> owner
membership -> billing state -> Checkout (mocked Stripe boundary) ->
webhook-driven entitlement activation -> onboarding/usage visibility ->
invitation -> lead creation under quota -> Customer Portal -> cancellation
-> entitlement drop -> owner never locked out.

Each stage is already covered in isolation by test_provisioning_integration
.py / test_billing_integration.py / test_entitlement_enforcement_integration
.py; this file's only job is proving they compose into one coherent
customer journey, not re-testing each stage's own edge cases.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app

import arie.api.main as api_main
import arie.billing.service as billing_service
import arie.billing.stripe_gateway as stripe_gateway
from arie.api.main import AppState, create_app, get_verified_identity
from arie.auth import AuthContext, VerifiedIdentity
from arie.billing.repository import get_billing

pytestmark = pytest.mark.integration

_WEBHOOK_SECRET = "whsec_e2e_test"
_PRICE_GROWTH = "price_e2e_growth"


@pytest.fixture(autouse=True)
def _stripe_test_config(monkeypatch: pytest.MonkeyPatch) -> None:
    from arie.config import StripeConfig

    config = StripeConfig(
        secret_key="sk_test_e2e",
        webhook_secret=_WEBHOOK_SECRET,
        price_starter="price_e2e_starter",
        price_growth=_PRICE_GROWTH,
        price_pro="price_e2e_pro",
    )
    monkeypatch.setattr(billing_service, "STRIPE", config)
    monkeypatch.setattr(stripe_gateway, "STRIPE", config)
    # No Supabase Admin API in this test environment — start_checkout needs
    # the caller's account email to create/reuse a Stripe Customer.
    monkeypatch.setattr(api_main, "get_user_email", lambda user_id: "founder@e2e-test.example")


def _sign(payload: bytes, *, timestamp: int | None = None) -> str:
    ts = timestamp if timestamp is not None else int(time.time())
    signed_payload = f"{ts}.".encode() + payload
    signature = hmac.new(_WEBHOOK_SECRET.encode(), signed_payload, hashlib.sha256).hexdigest()
    return f"t={ts},v1={signature}"


def _event(event_type: str, obj: dict[str, Any], *, event_id: str) -> tuple[bytes, str]:
    body = json.dumps(
        {"id": event_id, "type": event_type, "created": int(time.time()), "data": {"object": obj}}
    ).encode()
    return body, _sign(body)


def _client_as(
    app_state: AppState, *, organization_id: uuid.UUID, user_id: uuid.UUID, role: str
) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(organization_id=organization_id, auth_method="jwt", user_id=user_id, role=role),
    )
    return TestClient(app, raise_server_exceptions=False)


def test_new_customer_lifecycle(
    app_state: AppState, db_conn: psycopg.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    owner_user_id = uuid.uuid4()
    org_id: uuid.UUID | None = None

    try:
        # 1. Signup: a verified Supabase identity provisions its own organization.
        signup_app = create_app(state=app_state)
        signup_app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
            user_id=owner_user_id, email="founder@e2e-test.example"
        )
        with TestClient(signup_app, raise_server_exceptions=False) as signup_client:
            created = signup_client.post("/organizations", json={"name": "E2E Rocketship Inc"})
        assert created.status_code == 201, created.text
        org_id = uuid.UUID(created.json()["organization_id"])

        owner = _client_as(app_state, organization_id=org_id, user_id=owner_user_id, role="owner")

        # 2. Owner membership + safe pre-subscription entitlement floor.
        me = owner.get("/organization")
        assert me.status_code == 200
        assert me.json()["execution_mode"] == "simulated"

        billing_before = owner.get("/billing")
        assert billing_before.status_code == 200
        assert billing_before.json()["entitlements"]["plan"] == "unsubscribed"

        # 3. Checkout (Stripe boundary mocked — no real account/money).
        monkeypatch.setattr(stripe_gateway, "get_or_create_customer", lambda **kw: "cus_e2e_test")
        monkeypatch.setattr(
            stripe_gateway,
            "create_checkout_session",
            lambda **kw: stripe_gateway.CheckoutSession(
                session_id="cs_e2e_test", url="https://checkout.stripe.com/e2e-test"
            ),
        )
        checkout = owner.post(
            "/billing/checkout",
            json={
                "plan": "growth",
                "success_url": "https://app.example/checkout-return?status=success",
                "cancel_url": "https://app.example/checkout-return?status=canceled",
            },
        )
        assert checkout.status_code == 200, checkout.text
        assert checkout.json()["checkout_url"] == "https://checkout.stripe.com/e2e-test"

        # 4. checkout.session.completed webhook.
        webhook_client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
        completed_body, completed_sig = _event(
            "checkout.session.completed",
            {"id": "cs_e2e_test", "customer": "cus_e2e_test"},
            event_id="evt_e2e_checkout",
        )
        completed = webhook_client.post(
            "/billing/webhook", content=completed_body, headers={"stripe-signature": completed_sig}
        )
        assert completed.status_code == 200

        # 5. customer.subscription.created webhook activates the plan.
        subscription_body, subscription_sig = _event(
            "customer.subscription.created",
            {
                "id": "sub_e2e_test",
                "customer": "cus_e2e_test",
                "status": "active",
                "cancel_at_period_end": False,
                "canceled_at": None,
                "items": {
                    "data": [
                        {
                            "price": {"id": _PRICE_GROWTH},
                            "current_period_start": 1_735_689_600,
                            "current_period_end": 1_738_368_000,
                        }
                    ]
                },
            },
            event_id="evt_e2e_subscription",
        )
        activated = webhook_client.post(
            "/billing/webhook",
            content=subscription_body,
            headers={"stripe-signature": subscription_sig},
        )
        assert activated.status_code == 200

        billing_record = get_billing(db_conn, organization_id=org_id)
        assert billing_record.plan == "growth"
        assert billing_record.status == "active"

        # 6. Entitlements now reflect the paid plan.
        limits = owner.get("/organization/limits")
        assert limits.status_code == 200
        assert limits.json()["plan"] == "growth"
        assert limits.json()["members_limit"] == 10

        # 7. Invite + accept a teammate — now well under the growth member cap.
        invited = owner.post(
            "/organization/invitations",
            json={"email": "teammate@e2e-test.example", "role": "admin"},
        )
        assert invited.status_code == 201, invited.text
        raw_token = invited.json()["raw_token"]

        teammate_user_id = uuid.uuid4()
        accept_app = create_app(state=app_state)
        accept_app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
            user_id=teammate_user_id, email="teammate@e2e-test.example"
        )
        with TestClient(accept_app, raise_server_exceptions=False) as accept_client:
            accepted = accept_client.post("/invitations/accept", json={"token": raw_token})
        assert accepted.status_code == 200, accepted.text

        members = owner.get("/organization/members")
        assert members.status_code == 200
        assert len(members.json()) == 2

        # 8. Lead creation under quota.
        lead = owner.post(
            "/leads",
            json={
                "source": "e2e-test",
                "email": "prospect@e2e-test.example",
                "company_domain": "e2e-test.example",
                "external_ref": f"e2e-{uuid.uuid4().hex[:8]}",
            },
        )
        assert lead.status_code == 201, lead.text

        limits_after_lead = owner.get("/organization/limits")
        assert limits_after_lead.json()["leads_used"] >= 1

        # 9. Customer Portal — self-service management, never mutates entitlements.
        monkeypatch.setattr(
            stripe_gateway,
            "create_portal_session",
            lambda **kw: stripe_gateway.PortalSession(url="https://billing.stripe.com/e2e-test"),
        )
        portal = owner.post("/billing/portal", json={})
        assert portal.status_code == 200
        assert portal.json()["portal_url"] == "https://billing.stripe.com/e2e-test"
        assert get_billing(db_conn, organization_id=org_id).plan == "growth"  # unchanged

        # 10. customer.subscription.deleted — cancellation.
        deleted_body, deleted_sig = _event(
            "customer.subscription.deleted",
            {
                "id": "sub_e2e_test",
                "customer": "cus_e2e_test",
                "status": "canceled",
                "cancel_at_period_end": False,
                "canceled_at": int(datetime.now(UTC).timestamp()),
                "items": {"data": []},
            },
            event_id="evt_e2e_deleted",
        )
        canceled = webhook_client.post(
            "/billing/webhook", content=deleted_body, headers={"stripe-signature": deleted_sig}
        )
        assert canceled.status_code == 200

        # 11. Entitlements dropped, but nothing destructive happened.
        limits_after_cancel = owner.get("/organization/limits")
        assert limits_after_cancel.json()["plan"] == "unsubscribed"

        members_after_cancel = owner.get("/organization/members")
        assert len(members_after_cancel.json()) == 2  # both members preserved

        still_accessible = owner.get("/organization")
        assert still_accessible.status_code == 200  # owner never locked out

        billing_after_cancel = owner.get("/billing")
        assert billing_after_cancel.status_code == 200  # billing page itself stays reachable

        # 12. Further gated usage is denied now that the org is over its new
        # (lower) member ceiling — but existing access is not revoked.
        blocked_invite = owner.post(
            "/organization/invitations",
            json={"email": "third@e2e-test.example", "role": "analyst_reviewer"},
        )
        assert blocked_invite.status_code == 402

        blocked_provider = owner.put(
            "/organization/providers/abstract_company_enrichment", json={"credential": "sk_test"}
        )
        assert blocked_provider.status_code == 402
    finally:
        if org_id is not None:
            with db_conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM billing_webhook_events WHERE organization_id = %s", (org_id,)
                )
                # leads (cascades jobs/lead_events), then persons (tenant-owned,
                # RESTRICTs organizations without ON DELETE CASCADE) — never
                # companies, which stay global/shared identity across every
                # organization and test run (see migrations/0012's own
                # docstring), so deleting one here could remove data another
                # test still depends on.
                cur.execute("DELETE FROM leads WHERE organization_id = %s", (org_id,))
                cur.execute("DELETE FROM persons WHERE organization_id = %s", (org_id,))
                cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
            db_conn.commit()
