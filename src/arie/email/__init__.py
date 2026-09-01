"""Transactional email (Productization M6 Part 13). `get_notifier()` is the
one place the rest of this codebase should reach for an
:class:`~arie.email.notifications.EmailNotifier` — it picks
:class:`~arie.email.ahasend.AhaSendEmailSender` when
`arie.config.EMAIL.configured`, else
:class:`~arie.email.fake.FakeEmailSender`, so nothing outside this package
has to branch on configuration itself.
"""

from __future__ import annotations

from arie.config import EMAIL
from arie.email.ahasend import AhaSendEmailSender
from arie.email.fake import FakeEmailSender
from arie.email.notifications import EmailNotifier
from arie.email.sender import DeliveryResult, EmailMessage, EmailSender

__all__ = [
    "AhaSendEmailSender",
    "DeliveryResult",
    "EmailMessage",
    "EmailNotifier",
    "EmailSender",
    "FakeEmailSender",
    "get_notifier",
]


def get_notifier() -> EmailNotifier:
    sender: EmailSender = AhaSendEmailSender() if EMAIL.configured else FakeEmailSender()
    return EmailNotifier(sender)
