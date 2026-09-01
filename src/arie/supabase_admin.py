"""Resolves a Supabase Auth `user_id` to its account email via the Supabase
Auth Admin API (`GET {SUPABASE_URL}/auth/v1/admin/users/{user_id}`,
`service_role`-authenticated) — Productization M6, needed because no table in
this database stores a member's email; Supabase Auth owns identity entirely
(see `arie.auth`'s own module docstring), and every existing lookup
(`resolve_auth_context`, `resolve_verified_identity`) only ever sees the
email of the *current request's own caller*, from their JWT — never another
member's.

**The one place `SUPABASE_SERVICE_ROLE_KEY` is used in this codebase.** That
key can read/write any Supabase Auth user — treat a nonzero
`arie.config.SUPABASE_AUTH.service_role_key` with the same care as a database
superuser credential. Every caller here is a best-effort notification lookup
(`arie.billing.service`, `arie.jobs.handlers`'s review-escalation path,
usage-warning checks) — a failed or unconfigured lookup returns `None` and
the caller skips sending that email, it never raises into a job or a webhook.
"""

from __future__ import annotations

import logging
from uuid import UUID

import httpx

from arie.config import SUPABASE_AUTH

__all__ = ["get_user_email"]

_LOGGER = logging.getLogger("arie.supabase_admin")
_TIMEOUT_SECONDS = 10.0


def get_user_email(user_id: UUID) -> str | None:
    """The account email for `user_id`, or `None` if Supabase isn't
    configured, the lookup fails, or the account has no email (phone-based
    auth, which this application doesn't offer as a sign-in method but a
    defensive `None` costs nothing).
    """
    if not SUPABASE_AUTH.configured or not SUPABASE_AUTH.service_role_key:
        return None
    url = f"{SUPABASE_AUTH.url}/auth/v1/admin/users/{user_id}"
    try:
        response = httpx.get(
            url,
            headers={
                "apikey": SUPABASE_AUTH.service_role_key,
                "Authorization": f"Bearer {SUPABASE_AUTH.service_role_key}",
            },
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError:
        _LOGGER.warning("supabase admin user lookup failed (transport error)")
        return None
    if response.status_code != 200:
        _LOGGER.warning("supabase admin user lookup returned HTTP %s", response.status_code)
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    email = body.get("email") if isinstance(body, dict) else None
    return email if isinstance(email, str) and email else None
