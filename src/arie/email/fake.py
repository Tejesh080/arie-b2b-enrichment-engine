"""A test/dev `EmailSender` that never reaches the network — the default
whenever `arie.config.EMAIL.configured` is `False` (no AhaSend credentials
set), which is every local dev environment and CI by default. Nothing sent
through this ever leaves the process."""

from __future__ import annotations

from arie.email.sender import DeliveryResult, EmailMessage, EmailSender

__all__ = ["FakeEmailSender"]


class FakeEmailSender(EmailSender):
    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    def send(self, message: EmailMessage) -> DeliveryResult:
        self.sent.append(message)
        return DeliveryResult(delivered=True, provider_message_id=f"fake-{len(self.sent)}")
