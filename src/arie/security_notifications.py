"""Security-notice emails (Productization M6 Part 16, completing the
`EmailNotifier.send_security_notice` template that shipped unwired).

**What this is for.** Four actions in this product can hand someone else
control, or take it away, and none of them are visible on any screen a
victim would happen to be looking at: writing or replacing a BYOK provider
credential, deleting one, changing a member's role, and removing a member.
An attacker who obtains one admin session can do all four silently. The
audit log records them — but nobody reads an audit log they have no reason
to open. An email is the only one of these signals that arrives without
being asked for, which is the entire point.

**Every active owner and admin is notified, including the actor.** Excluding
the actor would be exactly backwards: when the action is malicious, the actor
*is* the attacker, and the one thing a legitimate admin's copy provides is
"I did not do this". A duplicate in your own inbox is a small price.

**Never deduplicated.** Usage warnings dedupe because a threshold crossed
twice in a month is the same fact told twice
(:mod:`arie.usage_notifications`). A second credential change is a second
credential change; collapsing them would hide the one that mattered.

Best-effort and non-blocking, by the same convention every notification path
in this codebase follows: callers invoke this *after* their own transaction
has committed, on a fresh connection, and nothing here can raise into the
request. A missed security email is bad; a failed role change because an
email provider was down is worse, and leaves the caller unsure which of the
two actually happened.

**No secret ever reaches a message here.** The credential notices name the
*provider*, never the credential — not even a prefix or a length. That is
not a hypothetical concern: these messages go to every admin's inbox and
through a third-party sending provider, which is a strictly worse place for
a BYOK key than Supabase Vault, where the real one lives.
"""

from __future__ import annotations

import logging
from uuid import UUID

import psycopg

from arie.audit import SYSTEM_ACTOR_ID, record_event
from arie.email import get_notifier
from arie.members import list_members
from arie.organizations import get_organization
from arie.supabase_admin import get_user_email

__all__ = [
    "notify_member_removed",
    "notify_member_role_changed",
    "notify_provider_credential_deleted",
    "notify_provider_credential_set",
]

_LOGGER = logging.getLogger("arie.security_notifications")


def _actor_label(actor_user_id: UUID) -> str:
    """The actor's email if the Auth Admin API can resolve it, else their id.

    Falls back rather than skipping the notice: "someone changed your
    provider credential" is still the alert worth sending when the identity
    lookup is unavailable, and an unresolvable id is itself worth seeing.
    """
    return get_user_email(actor_user_id) or f"user {actor_user_id}"


def _notify(conn: psycopg.Connection, *, organization_id: UUID, message: str, event: str) -> None:
    try:
        organization = get_organization(conn, organization_id=organization_id)
        if organization is None:
            return
        recipients = [
            member
            for member in list_members(conn, organization_id=organization_id)
            if member.role in ("owner", "admin")
        ]
        emails = [
            email
            for email in (get_user_email(member.user_id) for member in recipients)
            if email is not None
        ]
        if not emails:
            return

        notifier = get_notifier()
        for email in emails:
            notifier.send_security_notice(
                to_email=email, organization_name=organization.name, message=message
            )
        record_event(
            conn,
            organization_id=organization_id,
            actor_user_id=SYSTEM_ACTOR_ID,
            event_type="security.notice_sent",
            payload={"event": event, "recipients": len(emails)},
        )
        conn.commit()
    except Exception:
        _LOGGER.exception("security notification failed for organization %s", organization_id)


def notify_provider_credential_set(
    conn: psycopg.Connection, *, organization_id: UUID, provider: str, actor_user_id: UUID
) -> None:
    _notify(
        conn,
        organization_id=organization_id,
        event="provider_credential_set",
        message=(
            f"A provider credential for {provider} was added or replaced by "
            f"{_actor_label(actor_user_id)}. If you did not expect this, rotate that "
            f"provider's key at the provider and review your organization's members."
        ),
    )


def notify_provider_credential_deleted(
    conn: psycopg.Connection, *, organization_id: UUID, provider: str, actor_user_id: UUID
) -> None:
    _notify(
        conn,
        organization_id=organization_id,
        event="provider_credential_deleted",
        message=(
            f"The provider credential for {provider} was deleted by "
            f"{_actor_label(actor_user_id)}. Live enrichment through that provider will "
            f"stop until a new credential is configured."
        ),
    )


def notify_member_role_changed(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    target_user_id: UUID,
    new_role: str,
    actor_user_id: UUID,
) -> None:
    target = get_user_email(target_user_id) or f"user {target_user_id}"
    _notify(
        conn,
        organization_id=organization_id,
        event="member_role_changed",
        message=(
            f"{target} was given the {new_role} role by {_actor_label(actor_user_id)}. "
            f"If you did not expect this, change it back and review your organization's members."
        ),
    )


def notify_member_removed(
    conn: psycopg.Connection, *, organization_id: UUID, target_user_id: UUID, actor_user_id: UUID
) -> None:
    target = get_user_email(target_user_id) or f"user {target_user_id}"
    _notify(
        conn,
        organization_id=organization_id,
        event="member_removed",
        message=(
            f"{target} was removed from this organization by {_actor_label(actor_user_id)}. "
            f"They no longer have access to its leads, receipts, or settings."
        ),
    )
