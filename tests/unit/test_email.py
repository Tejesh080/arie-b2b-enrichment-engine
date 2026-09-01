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
