"""Security-notice emails, wired to the four actions that can hand someone
control of an organization (Productization M6 Part 16).

Role changes, member removals, and BYOK credential writes/deletes are all
silent: nothing about them appears on a screen the affected people are
already looking at, and an attacker holding one admin session can do all
four without leaving a trace anyone would think to check. The audit log
records them; an email is the only signal that arrives *unasked*.

These tests drive the real HTTP routes against a real database and capture
what the notifier was handed, because the property that matters is not "the
template renders" (covered by tests/unit/test_email.py) but "performing this
action actually causes the notice" — the wiring is the part that silently
gets dropped.

Requires TEST_DATABASE_URL; skipped otherwise (see conftest.py).
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from uuid import UUID

import psycopg
import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import authorize_app

import arie.security_notifications as security_notifications
from arie.api.main import AppState, create_app
from arie.auth import AuthContext
from arie.email.fake import FakeEmailSender
from arie.email.notifications import EmailNotifier
from arie.email.sender import EmailMessage

pytestmark = pytest.mark.integration


@pytest.fixture
def outbox(monkeypatch: pytest.MonkeyPatch) -> FakeEmailSender:
    """Capture whatever the security-notice path sends.

    `get_notifier()` is patched *in this module's namespace* rather than
    globally: every other notification path (invitations, usage warnings)
    keeps its own real behavior, so a test here cannot pass because some
    unrelated code emailed something.
    """
    sender = FakeEmailSender()
    monkeypatch.setattr(security_notifications, "get_notifier", lambda: EmailNotifier(sender))
    return sender


@pytest.fixture
def known_emails(monkeypatch: pytest.MonkeyPatch) -> dict[UUID, str]:
    """A stand-in for the Supabase Auth Admin API, which no test environment
    has. Unknown ids resolve to `None`, exactly as the real lookup does for a
    user the API cannot see — the fallback path matters as much as the happy
    one, since a notice that silently vanishes when a lookup fails is worse
    than no feature at all."""
    table: dict[UUID, str] = {}
    monkeypatch.setattr(
        security_notifications, "get_user_email", lambda user_id: table.get(user_id)
    )
    return table


@pytest.fixture
def org_with_admins(
    db_conn: psycopg.Connection, known_emails: dict[UUID, str]
) -> Iterator[tuple[UUID, UUID, UUID, UUID]]:
    """`(organization_id, owner_id, admin_id, analyst_id)` — three members
    across the three roles, so "notify owners and admins" is distinguishable
    from "notify everybody"."""
    org_id = uuid.uuid4()
    owner_id, admin_id, analyst_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    known_emails[owner_id] = "owner@sec-test.example"
    known_emails[admin_id] = "admin@sec-test.example"
    known_emails[analyst_id] = "analyst@sec-test.example"

    with db_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO organizations (organization_id, name, slug, status) "
            "VALUES (%s, %s, %s, 'active')",
            (org_id, "Security Notice Org", f"sec-test-{org_id.hex[:10]}"),
        )
        for user_id, role in (
            (owner_id, "owner"),
            (admin_id, "admin"),
            (analyst_id, "analyst_reviewer"),
        ):
            cur.execute(
                "INSERT INTO organization_members (organization_id, user_id, role, status) "
                "VALUES (%s, %s, %s, 'active')",
                (org_id, user_id, role),
            )
        cur.execute(
            "UPDATE organization_billing SET plan = 'internal' WHERE organization_id = %s",
            (org_id,),
        )
    db_conn.commit()
    try:
        yield org_id, owner_id, admin_id, analyst_id
    finally:
        with db_conn.cursor() as cur:
            cur.execute("DELETE FROM billing_webhook_events WHERE organization_id = %s", (org_id,))
            cur.execute("DELETE FROM organizations WHERE organization_id = %s", (org_id,))
        db_conn.commit()


def _client_as(app_state: AppState, *, organization_id: UUID, user_id: UUID) -> TestClient:
    app = create_app(state=app_state)
    authorize_app(
        app,
        AuthContext(
            organization_id=organization_id, auth_method="jwt", user_id=user_id, role="owner"
        ),
    )
    return TestClient(app, raise_server_exceptions=False)


def _recipients(outbox: FakeEmailSender) -> set[str]:
    return {message.to_email for message in outbox.sent}


def _security_messages(outbox: FakeEmailSender) -> list[EmailMessage]:
    return [message for message in outbox.sent if message.category == "security_notice"]


# ------------------------------------------------------------ role changes --


def test_a_role_change_notifies_every_owner_and_admin(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    org_id, owner_id, _admin_id, analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    response = client.patch(f"/organization/members/{analyst_id}", json={"role": "admin"})
    assert response.status_code == 200, response.text

    messages = _security_messages(outbox)
    assert messages, "a role change must produce a security notice"
    # The analyst was just promoted, so they are now an admin too — the
    # recipient list is resolved after the change, which is the correct
    # moment: the new admin is exactly someone who should know.
    assert {"owner@sec-test.example", "admin@sec-test.example"} <= _recipients(outbox)
    assert "analyst@sec-test.example" in messages[0].text_content
    assert "admin" in messages[0].text_content


def test_the_actor_is_notified_too(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    """Deliberately not excluded. When the action is malicious the actor *is*
    the attacker, and a legitimate admin's own copy is what lets them say "I
    did not do this" — the whole value of the alert."""
    org_id, owner_id, _admin_id, analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    client.patch(f"/organization/members/{analyst_id}", json={"role": "admin"})

    assert "owner@sec-test.example" in _recipients(outbox)


def test_a_removal_notifies_the_remaining_admins(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    org_id, owner_id, _admin_id, analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    response = client.delete(f"/organization/members/{analyst_id}")
    assert response.status_code == 200, response.text

    messages = _security_messages(outbox)
    assert messages
    assert "analyst@sec-test.example" in messages[0].text_content
    assert "removed" in messages[0].text_content


def test_a_refused_role_change_sends_nothing(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    """Demoting the last owner is a 409. A notice for an action that did not
    happen is worse than none — it trains people to ignore these."""
    org_id, owner_id, _admin_id, _analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    response = client.patch(f"/organization/members/{owner_id}", json={"role": "analyst_reviewer"})

    assert response.status_code in (403, 409)
    assert _security_messages(outbox) == []


def test_a_role_change_for_a_member_who_does_not_exist_sends_nothing(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    org_id, owner_id, _admin_id, _analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    response = client.patch(f"/organization/members/{uuid.uuid4()}", json={"role": "admin"})

    assert response.status_code == 404
    assert _security_messages(outbox) == []


# ------------------------------------------------- provider credentials ----


def test_writing_a_provider_credential_notifies_without_naming_the_credential(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    """The single most important assertion in this file. These messages go to
    every admin's inbox and through a third-party sending provider — a
    strictly worse place for a BYOK key than Supabase Vault, where the real
    one lives. The notice names the provider; nothing more."""
    org_id, owner_id, _admin_id, _analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)
    secret = "sk_live_do_not_email_this_anywhere"

    response = client.put(
        "/organization/providers/hunter_combined_enrichment", json={"credential": secret}
    )
    if response.status_code >= 400:
        pytest.skip(f"provider credential storage unavailable here: {response.text}")

    messages = _security_messages(outbox)
    assert messages
    body = "\n".join(m.text_content + m.html_content + m.subject for m in messages)
    assert "hunter_combined_enrichment" in body
    assert secret not in body
    assert "sk_live" not in body


def test_deleting_a_provider_credential_notifies(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    org_id, owner_id, _admin_id, _analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)
    created = client.put(
        "/organization/providers/hunter_combined_enrichment", json={"credential": "sk_test_x"}
    )
    if created.status_code >= 400:
        pytest.skip(f"provider credential storage unavailable here: {created.text}")
    outbox.sent.clear()

    response = client.delete("/organization/providers/hunter_combined_enrichment")
    assert response.status_code == 204, response.text

    messages = _security_messages(outbox)
    assert messages
    assert "deleted" in messages[0].text_content


def test_deleting_a_provider_that_was_never_configured_sends_nothing(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    org_id, owner_id, _admin_id, _analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    response = client.delete("/organization/providers/apollo_person_enrichment")

    assert response.status_code == 404
    assert _security_messages(outbox) == []


# -------------------------------------------------------- failure modes ----


def test_an_unresolvable_actor_still_produces_a_notice(
    app_state: AppState,
    db_conn: psycopg.Connection,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    known_emails: dict[UUID, str],
    outbox: FakeEmailSender,
) -> None:
    """ "Someone changed your provider credential" is still the alert worth
    sending when the Auth Admin lookup can't name them — and an actor whose
    id resolves to nothing is itself worth seeing."""
    org_id, owner_id, _admin_id, analyst_id = org_with_admins
    del known_emails[owner_id]
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    client.patch(f"/organization/members/{analyst_id}", json={"role": "admin"})

    messages = _security_messages(outbox)
    assert messages
    assert str(owner_id) in messages[0].text_content


def test_a_failing_notifier_does_not_fail_the_request(
    app_state: AppState,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A role change that half-succeeds — applied in the database, reported
    as a 500 — is worse than a missed email: the caller has no idea which of
    the two happened, and the obvious response (retry) makes it worse."""
    org_id, owner_id, _admin_id, analyst_id = org_with_admins

    def explode() -> EmailNotifier:
        raise RuntimeError("email provider down")

    monkeypatch.setattr(security_notifications, "get_notifier", explode)
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    response = client.patch(f"/organization/members/{analyst_id}", json={"role": "admin"})

    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"


def test_the_notice_is_recorded_in_the_audit_log(
    app_state: AppState,
    db_conn: psycopg.Connection,
    org_with_admins: tuple[UUID, UUID, UUID, UUID],
    outbox: FakeEmailSender,
) -> None:
    """Proving a notice was sent needs a durable record; the FakeEmailSender's
    list disappears with the process. Reuses the append-only audit log M4
    already writes, rather than a second bookkeeping table."""
    org_id, owner_id, _admin_id, analyst_id = org_with_admins
    client = _client_as(app_state, organization_id=org_id, user_id=owner_id)

    client.patch(f"/organization/members/{analyst_id}", json={"role": "admin"})

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT payload FROM organization_audit_events "
            "WHERE organization_id = %s AND event_type = 'security.notice_sent'",
            (org_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0][0]["event"] == "member_role_changed"
    assert rows[0][0]["recipients"] >= 1
