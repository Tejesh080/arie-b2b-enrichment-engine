"""Transactional email (Productization M6 Part 13) — `FakeEmailSender`
records everything it's given (used as this package's default absent
AhaSend credentials), `EmailNotifier` builds the right message shape per
notification type, and `AhaSendEmailSender` is exercised hermetically via a
monkeypatched `httpx.post`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from arie import email as email_pkg
from arie.config import EmailConfig
from arie.email.ahasend import AhaSendEmailSender
from arie.email.fake import FakeEmailSender
from arie.email.notifications import EmailNotifier


@dataclass
class _FakeResponse:
    status_code: int
    _body: dict[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        return self._body or {}


def test_fake_email_sender_records_every_message() -> None:
    sender = FakeEmailSender()
    notifier = EmailNotifier(sender)

    result = notifier.send_invitation(
        to_email="new-member@example.com",
        organization_name="Acme",
        inviter_email="owner@acme.example",
        role="admin",
        accept_url="https://app.example/invitations/accept?token=abc",
    )

    assert result.delivered is True
    assert len(sender.sent) == 1
    message = sender.sent[0]
    assert message.to_email == "new-member@example.com"
    assert message.category == "invitation"
    assert "https://app.example/invitations/accept?token=abc" in message.text_content
    assert "Acme" in message.subject


def test_send_review_required_includes_the_review_url_and_summary() -> None:
    sender = FakeEmailSender()
    notifier = EmailNotifier(sender)

    notifier.send_review_required(
        to_email="reviewer@acme.example",
        organization_name="Acme",
        lead_reference="8fdb7f7d",
        summary="Machine recommendation: reject.",
        review_url="https://app.example/reviews/abc",
    )

    message = sender.sent[0]
    assert message.category == "review_required"
    assert "https://app.example/reviews/abc" in message.text_content
    assert "reject" in message.text_content


def test_send_usage_warning_and_limit_reached_have_distinct_categories() -> None:
    sender = FakeEmailSender()
    notifier = EmailNotifier(sender)

    notifier.send_usage_warning(
        to_email="owner@acme.example",
        organization_name="Acme",
        metric_label="leads/month",
        used=420,
        limit=500,
        percent=0.84,
        billing_url="https://app.example/settings/billing",
    )
    notifier.send_limit_reached(
        to_email="owner@acme.example",
        organization_name="Acme",
        metric_label="leads/month",
        billing_url="https://app.example/settings/billing",
    )

    categories = [m.category for m in sender.sent]
    assert categories == ["usage_warning", "limit_reached"]


def test_ahasend_sender_returns_delivered_false_when_not_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "arie.email.ahasend.EMAIL", EmailConfig(ahasend_api_key="", ahasend_account_id="")
    )
    sender = AhaSendEmailSender()
    notifier = EmailNotifier(sender)
    result = notifier.send_security_notice(
        to_email="owner@acme.example", organization_name="Acme", message="test"
    )
    assert result.delivered is False


def test_ahasend_sender_posts_to_the_documented_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    config = EmailConfig(
        ahasend_api_key="aha-sk-test",
        ahasend_account_id="acct_123",
        from_email="notifications@arie.example",
        from_name="ARIE",
    )
    monkeypatch.setattr("arie.email.ahasend.EMAIL", config)

    captured: dict[str, Any] = {}

    def _fake_post(
        url: str, *, json: dict[str, Any], headers: dict[str, str], timeout: float
    ) -> _FakeResponse:
        captured["url"] = url
        captured["json"] = json
        captured["headers"] = headers
        return _FakeResponse(200, {"data": [{"id": "msg_1"}]})

    monkeypatch.setattr("arie.email.ahasend.httpx.post", _fake_post)

    sender = AhaSendEmailSender()
    result = sender.send(
        email_pkg.EmailMessage(
            to_email="member@example.com",
            subject="Subject",
            text_content="text",
            html_content="<p>html</p>",
            category="invitation",
        )
    )

    assert result.delivered is True
    assert result.provider_message_id == "msg_1"
    assert captured["url"] == "https://api.ahasend.com/v2/accounts/acct_123/messages"
    assert captured["headers"]["Authorization"] == "Bearer aha-sk-test"
    assert captured["json"]["recipients"] == [{"email": "member@example.com"}]


def test_ahasend_sender_reports_a_provider_error_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "arie.email.ahasend.EMAIL",
        EmailConfig(ahasend_api_key="aha-sk-test", ahasend_account_id="acct_123"),
    )
    monkeypatch.setattr(
        "arie.email.ahasend.httpx.post",
        lambda *a, **kw: _FakeResponse(422, {"error": "bad request"}),
    )

    sender = AhaSendEmailSender()
    result = sender.send(
        email_pkg.EmailMessage(
            to_email="member@example.com",
            subject="s",
            text_content="t",
            html_content="<p>t</p>",
            category="invitation",
        )
    )
    assert result.delivered is False
    assert result.error is not None


def test_get_notifier_uses_fake_sender_when_unconfigured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(email_pkg, "EMAIL", EmailConfig(ahasend_api_key="", ahasend_account_id=""))
    notifier = email_pkg.get_notifier()
    assert isinstance(notifier._sender, FakeEmailSender)


# ------------------------------------------------------- HTML injection ----
#
# `organization_name` is tenant-controlled free text (200 characters, no
# character restrictions — `CreateOrganizationRequest`/
# `UpdateOrganizationRequest`), and since Productization M6 Part 10 anyone
# with an email address can create an organization without being invited.
# An invitation then carries that text to an arbitrary address *outside* the
# organization, from ARIE's own verified sending domain. Unescaped, that is a
# working phishing primitive with someone else's deliverability behind it.

_MARKUP_NAME = '</p><a href="https://evil.example">Reset your password</a><p>'
_IMG_PAYLOAD = '<img src=x onerror="alert(1)">'


def _only_message(sender: FakeEmailSender) -> Any:
    assert len(sender.sent) == 1
    return sender.sent[0]


def test_an_organization_name_cannot_inject_markup_into_an_invitation() -> None:
    sender = FakeEmailSender()

    EmailNotifier(sender).send_invitation(
        to_email="stranger@example.com",
        organization_name=_MARKUP_NAME,
        inviter_email="attacker@evil.example",
        role="admin",
        accept_url="https://app.example/invitations/accept?token=abc",
    )

    html = _only_message(sender).html_content
    assert '<a href="https://evil.example">' not in html
    assert "&lt;/p&gt;&lt;a href=" in html


def test_an_inviter_email_cannot_inject_markup() -> None:
    """The inviter's address comes from Supabase Auth, not from the request
    body — but it is still someone's self-chosen identifier, and treating one
    interpolated value as trusted while escaping its neighbours is how the
    next edit reintroduces the hole."""
    sender = FakeEmailSender()

    EmailNotifier(sender).send_invitation(
        to_email="stranger@example.com",
        organization_name="Acme",
        inviter_email=_IMG_PAYLOAD,
        role="admin",
        accept_url="https://app.example/accept",
    )

    assert "<img" not in _only_message(sender).html_content


def test_a_url_cannot_break_out_of_its_href_attribute() -> None:
    """`quote=True`, specifically. An unescaped `"` inside `href="..."` ends
    the attribute early and turns the rest of the value into new attributes —
    `onclick`, for instance."""
    sender = FakeEmailSender()

    EmailNotifier(sender).send_invitation(
        to_email="stranger@example.com",
        organization_name="Acme",
        inviter_email="owner@acme.example",
        role="admin",
        accept_url='https://app.example/accept" onmouseover="alert(1)',
    )

    html = _only_message(sender).html_content
    assert 'onmouseover="alert(1)"' not in html
    assert "&quot;" in html


def test_the_plain_text_half_keeps_the_raw_value() -> None:
    """Text content is never parsed as markup, and escaping it would show a
    reader `Acme &amp; Co` — a bug in the other direction."""
    sender = FakeEmailSender()

    EmailNotifier(sender).send_invitation(
        to_email="stranger@example.com",
        organization_name="Acme & Co",
        inviter_email="owner@acme.example",
        role="admin",
        accept_url="https://app.example/accept",
    )

    message = _only_message(sender)
    assert "Acme & Co" in message.text_content
    assert "Acme &amp; Co" in message.html_content


@pytest.mark.parametrize(
    "send",
    [
        pytest.param(
            lambda n, name: n.send_review_required(
                to_email="r@example.com",
                organization_name=name,
                lead_reference="lead-1",
                summary="reject, confidence 0.46",
                review_url="https://app.example/review",
            ),
            id="review_required",
        ),
        pytest.param(
            lambda n, name: n.send_usage_warning(
                to_email="r@example.com",
                organization_name=name,
                metric_label="leads/month",
                used=80,
                limit=100,
                percent=0.8,
                billing_url="https://app.example/billing",
            ),
            id="usage_warning",
        ),
        pytest.param(
            lambda n, name: n.send_limit_reached(
                to_email="r@example.com",
                organization_name=name,
                metric_label="leads/month",
                billing_url="https://app.example/billing",
            ),
            id="limit_reached",
        ),
        pytest.param(
            lambda n, name: n.send_payment_problem(
                to_email="r@example.com",
                organization_name=name,
                reason="payment failed",
                portal_url="https://app.example/portal",
            ),
            id="payment_problem",
        ),
        pytest.param(
            lambda n, name: n.send_security_notice(
                to_email="r@example.com",
                organization_name=name,
                message="A provider credential was replaced.",
            ),
            id="security_notice",
        ),
    ],
)
def test_no_template_renders_a_tenant_name_as_markup(send: Any) -> None:
    """Every template, not just the one that leaves the organization. An
    internal-only email that renders attacker markup is still an attack on
    the admin reading it."""
    sender = FakeEmailSender()

    send(EmailNotifier(sender), _IMG_PAYLOAD)

    assert "<img" not in _only_message(sender).html_content


# ------------------------------------------------------- security notices --


def test_send_security_notice_has_its_own_category_and_carries_the_message() -> None:
    sender = FakeEmailSender()

    EmailNotifier(sender).send_security_notice(
        to_email="owner@acme.example",
        organization_name="Acme",
        message="A provider credential for hunter_combined_enrichment was replaced by o@a.example.",
    )

    message = _only_message(sender)
    assert message.category == "security_notice"
    assert "hunter_combined_enrichment" in message.text_content
    assert "Acme" in message.subject
