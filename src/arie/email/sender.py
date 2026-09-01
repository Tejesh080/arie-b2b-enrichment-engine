"""The transport-level email contract (Productization M6 Part 13). Deliberately
tiny — one message shape, one `send` method — so `arie.email.notifications
.EmailNotifier` (which owns *what* each notification says) never has to
change when the transport does, and a new provider is one new class here,
never a change to any call site.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

__all__ = ["DeliveryResult", "EmailMessage", "EmailSender"]


@dataclass(frozen=True)
class EmailMessage:
    to_email: str
    subject: str
    text_content: str
    html_content: str
    category: str
    """A short label (`"invitation"`, `"review_required"`, ...) for logging
    and metrics only — never sensitive, safe to put in a trace attribute or
    a log line unlike the message body itself, which may name a real
    person/company."""


@dataclass(frozen=True)
class DeliveryResult:
    delivered: bool
    provider_message_id: str | None = None
    error: str | None = None
    """A short, sanitized failure reason — never a raw provider exception
    `str()` that could carry request/response internals. Safe to put in
    `organization_invitations.email_error` or an audit payload."""


class EmailSender(Protocol):
    def send(self, message: EmailMessage) -> DeliveryResult: ...
