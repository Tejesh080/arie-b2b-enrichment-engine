"""Productization M6 — the commercial paths are traceable, and their traces
are safe to ship to a third-party collector.

M1 Step 9 made one *lead* traceable end to end. Money moving is the other
thing an operator has to be able to reconstruct after the fact: "this
organization says it paid and has no plan" is answerable only if checkout,
the webhook that follows it, and the provisioning that preceded both left
spans in the same shape as the rest of the system.

The second half of this file is the part that matters more. A span exporter
sends attributes to somewhere outside this process, so anything that reaches
a span leaves the trust boundary. Stripe's secret key, its webhook signing
secret, the signature header, an invitation's raw token — none may ever
appear in one, and asserting that is cheaper than auditing every future
`set_attributes` call by eye.

Requires TEST_DATABASE_URL; skipped otherwise (see conftest.py).
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
from opentelemetry.sdk.trace import ReadableSpan
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from tests.integration.conftest import authorize_app

import arie.api.main as api_main
import arie.billing.service as billing_service
import arie.billing.stripe_gateway as stripe_gateway
from arie.api.main import AppState, create_app, get_verified_identity
from arie.auth import AuthContext, VerifiedIdentity
from arie.config import StripeConfig

pytestmark = pytest.mark.integration

_WEBHOOK_SECRET = "whsec_observability_secret"
_SECRET_KEY = "sk_test_observability_secret"
_PRICE_GROWTH = "price_observability_growth"


@pytest.fixture(autouse=True)
def _stripe_test_config(monkeypatch: pytest.MonkeyPatch) -> None:
    config = StripeConfig(
        secret_key=_SECRET_KEY,
        webhook_secret=_WEBHOOK_SECRET,
        price_starter="price_observability_starter",
        price_growth=_PRICE_GROWTH,
        price_pro="price_observability_pro",
    )
    monkeypatch.setattr(billing_service, "STRIPE", config)
    monkeypatch.setattr(stripe_gateway, "STRIPE", config)
    monkeypatch.setattr(api_main, "get_user_email", lambda user_id: "founder@observability.example")


@pytest.fixture
def org(db_conn: psycopg.Connection) -> Iterator[UUID]:
    org_id = uuid.uuid4()
    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (org_id, "Observability Test Org", f"obs-test-{org_id.hex[:10]}"),
        )
    db_conn.commit()
    try:
        yield org_id
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM billing_webhook_events WHERE organization_id = %s", (org_id,))
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
        db_conn.commit()


def _sign(payload: bytes) -> str:
    ts = int(time.time())
    signature = hmac.new(
        _WEBHOOK_SECRET.encode(), f"{ts}.".encode() + payload, hashlib.sha256
    ).hexdigest()
    return f"t={ts},v1={signature}"


def _event(event_type: str, obj: dict[str, Any]) -> tuple[bytes, str]:
    body = json.dumps(
        {
            "id": f"evt_{uuid.uuid4().hex[:24]}",
            "type": event_type,
            "created": int(time.time()),
            "data": {"object": obj},
        }
    ).encode()
    return body, _sign(body)


def _client_as(app_state: AppState, *, organization_id: UUID, user_id: UUID) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=organization_id, auth_method="jwt", user_id=user_id, role="owner"
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


def _named(spans: InMemorySpanExporter, name: str) -> list[ReadableSpan]:
    return [span for span in spans.get_finished_spans() if span.name == name]


def _one(spans: InMemorySpanExporter, name: str) -> ReadableSpan:
    matches = _named(spans, name)
    assert len(matches) == 1, f"expected exactly one {name!r} span, got {len(matches)}"
    return matches[0]


def _stub_stripe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(stripe_gateway, "get_or_create_customer", lambda **kw: "cus_observability")
    monkeypatch.setattr(
        stripe_gateway,
        "create_checkout_session",
        lambda **kw: stripe_gateway.CheckoutSession(
            session_id="cs_observability", url="https://checkout.stripe.com/observability"
        ),
    )
    monkeypatch.setattr(
        stripe_gateway,
        "create_portal_session",
        lambda **kw: stripe_gateway.PortalSession(url="https://billing.stripe.com/observability"),
    )


# --------------------------------------------------------- spans are emitted --


def test_provisioning_emits_a_span(
    app_state: AppState, db_conn: psycopg.Connection, spans: InMemorySpanExporter
) -> None:
    user_id = uuid.uuid4()
    app = create_app(state=app_state)
    app.dependency_overrides[get_verified_identity] = lambda: VerifiedIdentity(
        user_id=user_id, email="founder@observability.example"
    )
    org_id: UUID | None = None
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            created = client.post("/organizations", json={"name": "Observability Signup"})
        assert created.status_code == 201, created.text
        org_id = UUID(created.json()["organization_id"])

        assert _named(spans, "organization.provision"), (
            "self-service signup is the first thing a new customer does — an "
            "un-traced one cannot be reconstructed when it half-fails"
        )
    finally:
        if org_id is not None:
            with db_conn.cursor() as cur:
                cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
            db_conn.commit()


def test_checkout_emits_a_span_carrying_the_organization_and_plan(
    app_state: AppState, org: UUID, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_stripe(monkeypatch)
    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4())

    response = client.post(
        "/billing/checkout",
        json={
            "plan": "growth",
            "success_url": "https://app.example/return",
            "cancel_url": "https://app.example/cancel",
        },
    )
    assert response.status_code == 200, response.text

    span = _one(spans, "billing.checkout.start")
    assert span.attributes is not None
    assert span.attributes["arie.organization_id"] == str(org)
    assert span.attributes["arie.plan"] == "growth"


def test_portal_emits_a_span(
    app_state: AppState,
    org: UUID,
    db_conn: psycopg.Connection,
    spans: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_stripe(monkeypatch)
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET stripe_customer_id = %s WHERE organization_id = %s",
            ("cus_observability", org),
        )
    db_conn.commit()

    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4())
    assert client.post("/billing/portal", json={}).status_code == 200

    span = _one(spans, "billing.portal.open")
    assert span.attributes is not None
    assert span.attributes["arie.organization_id"] == str(org)


def test_webhook_span_records_the_status_it_returned(
    app_state: AppState, org: UUID, db_conn: psycopg.Connection, spans: InMemorySpanExporter
) -> None:
    """The webhook handler answers 200 for several genuinely different
    reasons (handled, ignored, already-seen). The span carries the status
    code so an operator can tell "Stripe was told everything is fine" from
    "ARIE actually did something", which the HTTP log alone cannot."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET stripe_customer_id = %s WHERE organization_id = %s",
            ("cus_observability", org),
        )
    db_conn.commit()

    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, signature = _event(
        "checkout.session.completed", {"id": "cs_observability", "customer": "cus_observability"}
    )
    response = client.post(
        "/billing/webhook", content=body, headers={"stripe-signature": signature}
    )
    assert response.status_code == 200

    span = _one(spans, "billing.webhook.process")
    assert span.attributes is not None
    assert span.attributes["arie.webhook.status_code"] == 200
    assert span.status.status_code is not StatusCode.ERROR


def test_a_rejected_webhook_still_produces_a_span(
    app_state: AppState, spans: InMemorySpanExporter
) -> None:
    """A forged or misconfigured signature is exactly the case an operator
    needs to see. A span that only exists on the happy path would go missing
    at the moment it is most wanted."""
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, _ = _event("checkout.session.completed", {"id": "cs_observability"})

    response = client.post(
        "/billing/webhook", content=body, headers={"stripe-signature": "t=1,v1=forged"}
    )
    assert response.status_code == 400

    span = _one(spans, "billing.webhook.process")
    assert span.attributes is not None
    assert span.attributes["arie.webhook.status_code"] == 400


# ------------------------------------------------- and carry no credentials --


def _all_span_text(spans: InMemorySpanExporter) -> str:
    """Everything a collector would receive, flattened: names, attributes, and
    every event/exception message recorded on every span."""
    chunks: list[str] = []
    for span in spans.get_finished_spans():
        chunks.append(span.name)
        for key, value in (span.attributes or {}).items():
            chunks.append(f"{key}={value}")
        for event in span.events:
            chunks.append(event.name)
            for key, value in (event.attributes or {}).items():
                chunks.append(f"{key}={value}")
        if span.status.description:
            chunks.append(span.status.description)
    return "\n".join(chunks)


def test_no_stripe_credential_reaches_a_span_on_the_happy_path(
    app_state: AppState, org: UUID, spans: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_stripe(monkeypatch)
    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4())
    client.post(
        "/billing/checkout",
        json={
            "plan": "growth",
            "success_url": "https://app.example/return",
            "cancel_url": "https://app.example/cancel",
        },
    )

    text = _all_span_text(spans)
    assert _SECRET_KEY not in text
    assert _WEBHOOK_SECRET not in text


def test_no_stripe_credential_or_signature_reaches_a_span_on_the_webhook_path(
    app_state: AppState, org: UUID, db_conn: psycopg.Connection, spans: InMemorySpanExporter
) -> None:
    """Both the secret *and* the signature header: a signature is not a
    long-lived credential, but it is caller-supplied bytes that an exception
    message could easily carry into a span on a parse failure."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET stripe_customer_id = %s WHERE organization_id = %s",
            ("cus_observability", org),
        )
    db_conn.commit()

    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, signature = _event(
        "customer.subscription.created",
        {
            "id": "sub_observability",
            "customer": "cus_observability",
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
    )
    client.post("/billing/webhook", content=body, headers={"stripe-signature": signature})

    text = _all_span_text(spans)
    assert _SECRET_KEY not in text
    assert _WEBHOOK_SECRET not in text
    assert signature not in text


def test_a_rejected_webhooks_span_does_not_echo_the_offered_signature(
    app_state: AppState, spans: InMemorySpanExporter
) -> None:
    """The failure path is where a signature is most likely to be logged,
    because the natural thing to write is "signature X did not verify"."""
    forged = "t=1,v1=forged_signature_value_that_must_not_be_traced"
    client = TestClient(create_app(state=app_state), raise_server_exceptions=False)
    body, _ = _event("checkout.session.completed", {"id": "cs_observability"})

    client.post("/billing/webhook", content=body, headers={"stripe-signature": forged})

    text = _all_span_text(spans)
    assert "forged_signature_value_that_must_not_be_traced" not in text
    assert _WEBHOOK_SECRET not in text


def test_an_invitation_token_never_reaches_a_span(
    app_state: AppState, org: UUID, db_conn: psycopg.Connection, spans: InMemorySpanExporter
) -> None:
    """The raw invitation token is a bearer credential — whoever holds it can
    join the organization. It is returned once to the inviter and stored only
    as a hash; a span carrying it would make the trace collector a second,
    unhashed copy."""
    with db_conn.cursor() as cur:
        cur.execute(
            "UPDATE organization_billing SET plan = 'internal' WHERE organization_id = %s", (org,)
        )
    db_conn.commit()

    client = _client_as(app_state, organization_id=org, user_id=uuid.uuid4())
    invited = client.post(
        "/organization/invitations",
        json={"email": "teammate@observability.example", "role": "analyst_reviewer"},
    )
    assert invited.status_code == 201, invited.text
    raw_token = invited.json()["raw_token"]

    assert raw_token not in _all_span_text(spans)
