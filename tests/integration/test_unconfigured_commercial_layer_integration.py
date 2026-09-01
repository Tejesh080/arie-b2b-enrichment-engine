"""The state production is actually in: M6's code deployed, none of M6's
third-party accounts configured.

Every other file in this suite configures Stripe, or a fake email sender, or
both, because that is what it takes to exercise the feature. None of them
test the configuration the deployment will genuinely run under on the day
this milestone ships — no `STRIPE_SECRET_KEY`, no `STRIPE_WEBHOOK_SECRET`,
no AhaSend credentials, no Turnstile secret — which is exactly the
configuration nobody notices is broken until a customer sees it.

The contract asserted here is narrow and worth stating plainly: **unset must
mean "safely off", never "half-working" and never "5xx"**. Reading billing
keeps working. Anything that would need a third party refuses with a status
an interface can render. Nothing that requires a secret proceeds without one
— most importantly, an unverifiable webhook is rejected rather than trusted.

Requires TEST_DATABASE_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app

import arie.api.main as api_main
import arie.billing.service as billing_service
import arie.billing.stripe_gateway as stripe_gateway
import arie.email as email_pkg
import arie.turnstile as turnstile_module
from arie.api.main import AppState, create_app, get_verified_identity
from arie.auth import AuthContext, VerifiedIdentity
from arie.config import EmailConfig, StripeConfig, TurnstileConfig
from arie.email.fake import FakeEmailSender

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def nothing_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty configs everywhere the commercial layer reads them — the literal
    production state, not an approximation of it."""
    blank_stripe = StripeConfig(
        secret_key="", webhook_secret="", price_starter="", price_growth="", price_pro=""
    )
    monkeypatch.setattr(billing_service, "STRIPE", blank_stripe)
    monkeypatch.setattr(stripe_gateway, "STRIPE", blank_stripe)
    monkeypatch.setattr(email_pkg, "EMAIL", EmailConfig(ahasend_api_key="", ahasend_account_id=""))
    monkeypatch.setattr(turnstile_module, "TURNSTILE", TurnstileConfig(secret_key="", site_key=""))
    # Supabase Auth *is* configured in production (it is where sessions come
    # from), so stub the one lookup Checkout needs. Without this the route
    # refuses earlier, on "could not resolve the caller's account email",
    # and never reaches the Stripe boundary these tests are about.
    monkeypatch.setattr(api_main, "get_user_email", lambda user_id: "owner@unconfigured.example")


@pytest.fixture
def org(db_conn: psycopg.Connection) -> Iterator[UUID]:
    org_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (org_id, "Unconfigured Org", f"unconfigured-{org_id.hex[:10]}"),
        )
    db_conn.commit()
    try:
        yield org_id
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM billing_webhook_events WHERE organization_id = %s", (org_id,))
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
        db_conn.commit()


def _client_as(app_state: AppState, *, organization_id: UUID) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=organization_id,
            auth_method="jwt",
            user_id=uuid.uuid4(),
            role="owner",
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


# ------------------------------------------------------- reads still work --


def test_billing_is_readable_with_no_stripe_account(app_state: AppState, org: UUID) -> None:
    """The Settings page must render. An organization that has never paid
    anyone still has a plan, a status, and entitlements — all of which come
    from this database, not from Stripe."""
    response = _client_as(app_state, organization_id=org).get("/billing")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["billing"]["plan"] == "starter"
    assert body["billing"]["stripe_customer_id"] is None
    assert body["entitlements"]["plan"] == "unsubscribed"


def test_limits_and_onboarding_are_readable(app_state: AppState, org: UUID) -> None:
    client = _client_as(app_state, organization_id=org)

    assert client.get("/organization/limits").status_code == 200
    assert client.get("/organization/onboarding").status_code == 200


# ---------------------------------------------- writes refuse, not crash --


def test_checkout_refuses_with_503_rather_than_a_500(app_state: AppState, org: UUID) -> None:
    """503, not 500: "this deployment cannot do that right now" is the true
    statement, and it is the one an interface can turn into readable copy.
    A 500 would send an operator hunting for a bug that isn't there."""
    response = _client_as(app_state, organization_id=org).post(
        "/billing/checkout",
        json={
            "plan": "growth",
            "success_url": "https://app.example/return",
            "cancel_url": "https://app.example/cancel",
        },
    )

    assert response.status_code == 503, response.text
    assert "STRIPE_SECRET_KEY" in response.json()["detail"]


def test_the_refusal_names_the_missing_variable_and_no_value(
    app_state: AppState, org: UUID
) -> None:
    """Naming the variable is what makes this self-diagnosing. Naming a
    *value* — even a partial one — would put a credential in an HTTP response
    body the moment one existed."""
    detail = (
        _client_as(app_state, organization_id=org)
        .post(
            "/billing/checkout",
            json={
                "plan": "growth",
                "success_url": "https://app.example/return",
                "cancel_url": "https://app.example/cancel",
            },
        )
        .json()["detail"]
    )

    assert "STRIPE_SECRET_KEY" in detail
    assert "sk_" not in detail
    assert "whsec_" not in detail


def test_portal_refuses_cleanly(app_state: AppState, org: UUID) -> None:
    response = _client_as(app_state, organization_id=org).post("/billing/portal", json={})

    assert 400 <= response.status_code < 600
    assert response.status_code != 500, response.text


# ------------------------------------------------- the webhook stays shut --


def test_an_unsigned_webhook_is_rejected_when_no_secret_is_configured(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    """The one that would actually be dangerous. With no signing secret there
    is no way to tell Stripe's traffic from anyone else's, so the only safe
    behavior is to trust none of it — "no secret configured" must never
    degrade into "skip verification".

    503, not 400: the fault is this deployment's, not the caller's, and
    Stripe retries a 5xx with backoff, so a delivery arriving after the
    secret is configured succeeds instead of having been discarded. Writing
    this test is what found it returning 500 — safe, but it pages an
    operator for a configuration state and buries the line saying what to
    fix.
    """
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body = json.dumps(
        {
            "id": "evt_forged",
            "type": "customer.subscription.created",
            "created": 1_735_689_600,
            "data": {"object": {"id": "sub_forged", "customer": "cus_forged"}},
        }
    ).encode()

    response = client.post("/billing/webhook", content=body)

    assert response.status_code == 503, response.text
    assert response.status_code != 500, "a configuration state is not a fault"
    assert "whsec_" not in response.text
    with db_conn.cursor() as cur:
        cur.execute("SELECT 1 FROM billing_webhook_events WHERE event_id = %s", ("evt_forged",))
        assert cur.fetchone() is None


def test_a_plausible_looking_signature_is_still_rejected(app_state: AppState) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body = json.dumps(
        {"id": "evt_forged_2", "type": "invoice.paid", "data": {"object": {}}}
    ).encode()

    response = client.post(
        "/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=" + "0" * 64}
    )

    # Same 503: with no secret the signature is not even reached, let alone
    # believed. What matters is that a well-formed forgery gets no further
    # than a malformed one.
    assert response.status_code == 503, response.text


# ------------------------------------------- signup and email still work --


def test_self_service_signup_works_without_a_turnstile_secret(
    app_state: AppState, db_conn: psycopg.Connection
) -> None:
    """The documented dev/CI bypass, asserted rather than assumed. It is a
    bypass only while the secret is unset — `tests/unit/test_turnstile.py`
    covers the configured case, where a missing or bad token is refused."""
    user_id = uuid.uuid4()
    app = create_app(state=app_state)
    app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
        user_id=user_id, email="founder@unconfigured.example"
    )
    org_id: UUID | None = None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            created = client.post("/organizations", json={"name": "Unconfigured Signup"})
        assert created.status_code == 201, created.text
        org_id = UUID(created.json()["organization_id"])
    finally:
        if org_id is not None:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
            db_conn.commit()


def test_the_notifier_falls_back_to_a_sender_that_opens_no_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With AhaSend unconfigured, `get_notifier()` must hand back the fake —
    not the real client pointed at an empty account id, which would attempt a
    request per notification and fail slowly instead of instantly."""
    notifier = email_pkg.get_notifier()

    assert isinstance(notifier._sender, FakeEmailSender)


def test_an_invitation_still_succeeds_with_no_email_provider(
    app_state: AppState, org: UUID, db_conn: psycopg.Connection
) -> None:
    """Delivery is not the same thing as the invitation existing. An owner
    must still be able to create one — and hand over the link themselves —
    when no email provider is configured. The row records that delivery did
    not happen rather than pretending it did."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET plan = 'internal' WHERE organization_id = %s", (org,)
        )
    db_conn.commit()

    response = _client_as(app_state, organization_id=org).post(
        "/organization/invitations",
        json={"email": "teammate@unconfigured.example", "role": "analyst_reviewer"},
    )

    assert response.status_code == 201, response.text
    assert response.json()["raw_token"]
    assert response.json()["email_status"] != "sent"


# --------------------------------------------------- liveness is unaffected --


def test_health_endpoints_are_unaffected_by_missing_commercial_config(
    app_state: AppState,
) -> None:
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)

    health = client.get("/healthz")
    assert health.status_code == 200
    assert health.json()["status"] == "ok"

    worker = client.get("/healthz/worker")
    assert worker.status_code == 200
