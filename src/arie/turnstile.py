"""Cloudflare Turnstile server-side verification (Productization M6 Part 12)
— abuse protection for self-service organization provisioning
(`POST /organizations`). Supabase Auth's own signup/password-reset pages
have their own, separate CAPTCHA integration configured directly in the
Supabase Dashboard (an external setup step — see `docs/m6-operations.md`);
this module is ARIE's own defense-in-depth on the one provisioning endpoint
this backend controls, guarding the case a valid-but-scripted Supabase
session tries to mint organizations in a loop.

**Deliberately never a silent bypass in a configured environment.** When
`arie.config.TURNSTILE.configured` is `False` (no Cloudflare account/site set
up yet), :func:`verify_turnstile_token` always returns `True` — a documented
dev/CI default, not a security decision this module makes quietly; see the
config class's own docstring and Part 12's explicit "do not stop M6, do not
silently bypass CAPTCHA in production" instruction. Once configured, a
missing or invalid token is always rejected.
"""

from __future__ import annotations

import logging

import httpx

from arie.config import TURNSTILE

__all__ = ["verify_turnstile_token"]

_LOGGER = logging.getLogger("arie.turnstile")
_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
_TIMEOUT_SECONDS = 10.0


def verify_turnstile_token(token: str | None, *, remote_ip: str | None = None) -> bool:
    """`True` if `token` is a valid, unexpired Turnstile response for the
    configured site — or if Turnstile isn't configured at all (dev/CI
    bypass). `False` for a missing token, an invalid one, or a verification
    request that itself fails (fail-closed once configured: a Cloudflare
    outage should not silently wave every signup through).
    """
    if not TURNSTILE.configured:
        return True
    if not token:
        return False

    data = {"secret": TURNSTILE.secret_key, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip

    try:
        response = httpx.post(_VERIFY_URL, data=data, timeout=_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except (httpx.HTTPError, ValueError):
        _LOGGER.warning("turnstile verification request failed")
        return False
    return bool(body.get("success", False))
