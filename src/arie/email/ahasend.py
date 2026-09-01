"""The production `EmailSender` — AhaSend's REST API
(`https://api.ahasend.com/v2/accounts/{account_id}/messages`, Bearer-token
authenticated). One HTTP call per message, never raising: a delivery failure
is a normal, expected outcome for a mail API (bad address, provider outage,
rate limit) and every caller in this codebase treats email as best-effort —
see `arie.invitations`' own "invitation may exist even if email delivery
fails" contract. This class is the boundary where that failure becomes a
:class:`~arie.email.sender.DeliveryResult` instead of an exception.
"""

from __future__ import annotations

import logging

import httpx

from arie.config import EMAIL
from arie.email.sender import DeliveryResult, EmailMessage, EmailSender

__all__ = ["AhaSendEmailSender"]

_LOGGER = logging.getLogger("arie.email.ahasend")
_BASE_URL = "https://api.ahasend.com/v2"


class AhaSendEmailSender(EmailSender):
    def __init__(self, *, timeout_seconds: float | None = None) -> None:
        self._timeout = timeout_seconds if timeout_seconds is not None else EMAIL.timeout_seconds

    def send(self, message: EmailMessage) -> DeliveryResult:
        if not EMAIL.configured:
            return DeliveryResult(delivered=False, error="email provider not configured")
        url = f"{_BASE_URL}/accounts/{EMAIL.ahasend_account_id}/messages"
        payload = {
            "from": {"email": EMAIL.from_email, "name": EMAIL.from_name},
            "recipients": [{"email": message.to_email}],
            "subject": message.subject,
            "text_content": message.text_content,
            "html_content": message.html_content,
        }
        try:
            response = httpx.post(
                url,
                json=payload,
                headers={"Authorization": f"Bearer {EMAIL.ahasend_api_key}"},
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            _LOGGER.warning(
                "email send failed (category=%s): %s", message.category, type(exc).__name__
            )
            return DeliveryResult(delivered=False, error=f"transport error: {type(exc).__name__}")

        if response.status_code >= 400:
            _LOGGER.warning(
                "email provider rejected message (category=%s, status=%s)",
                message.category,
                response.status_code,
            )
            return DeliveryResult(
                delivered=False, error=f"provider returned HTTP {response.status_code}"
            )

        message_id = None
        try:
            body = response.json()
            if isinstance(body, dict):
                data = body.get("data")
                if isinstance(data, list) and data and isinstance(data[0], dict):
                    message_id = data[0].get("id")
        except ValueError:
            pass
        return DeliveryResult(delivered=True, provider_message_id=message_id)
