"""Organization invitations (Productization M4 Part 2). Owns every piece of
`organization_invitations` (`migrations/0024_organization_invitations.sql`):
generating an invitation, listing/revoking, and accepting one against a
*verified* Supabase identity.

**Identity is entirely Supabase Auth's** — this module never touches a
password. `accept_invitation` takes an already-verified `(user_id, email)`
pair (`arie.auth.resolve_verified_identity`'s output) and cross-checks the
email against the invitation's target; it does not itself decode a token or
know what a JWT is.

**Token handling mirrors `arie.apikeys` exactly**: a `secrets.token_urlsafe
(32)` raw token, SHA-256 hashed (no salt/slow-KDF — correct for 256 bits of
real randomness), shown to the caller exactly once at creation time and
never persisted or logged in raw form. Unlike an API key there is no
`key_prefix` — an invitation token is never looked up or displayed by a
short identifying fragment after creation, only ever presented whole by
whoever holds the invite link, so a direct `WHERE token_hash = ...` lookup
(itself index-backed by the column's `UNIQUE` constraint) is simpler and
sufficient.
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.audit import record_event
from arie.auth import ROLES
from arie.billing.plans import MemberQuotaExceededError, enforce_member_quota
from arie.identity.normalize import normalize_email

__all__ = [
    "DEFAULT_EXPIRY",
    "DuplicateInvitationError",
    "GeneratedInvitation",
    "InvalidInvitationRoleError",
    "InvitationExpiredError",
    "InvitationNotFoundError",
    "InvitationRecord",
    "MemberQuotaExceededError",
    "MismatchedInvitationEmailError",
    "accept_invitation",
    "create_invitation",
    "list_invitations",
    "mark_invitation_email_status",
    "revoke_invitation",
    "send_invitation_email",
]

DEFAULT_EXPIRY = timedelta(days=7)
"""How long a created invitation stays acceptable. Not configurable per
invitation in V1 — the brief asks for expiry to exist and be enforced, not
for a per-invite custom window."""


class InvalidInvitationRoleError(ValueError):
    """`role` is not one of `arie.auth.ROLES` — the same guard
    `organization_members`' own CHECK constraint enforces at the DB layer,
    checked here first so a bad role never reaches a query."""


class DuplicateInvitationError(ValueError):
    """A pending invitation already exists for this `(organization_id,
    email)` — either caught by `create_invitation`'s own pre-check, or (a
    genuine concurrent-request race) by `idx_organization_invitations_pending
    _unique` itself; both surface identically to the caller."""


class InvitationNotFoundError(Exception):
    """No *acceptable* invitation matches the presented token — covers "no
    such token," "already accepted" (replay), and "revoked" identically, the
    same IDOR-safe shape `arie.apikeys.InvalidApiKeyError` uses: telling
    those apart would tell a caller holding a stale or guessed token more
    than a legitimate holder of a live one ever needs to know."""


class InvitationExpiredError(Exception):
    """The token is real and was still `pending`, but its `expires_at` has
    passed. Reported distinctly (unlike the cases above) because whoever
    presents an expired-but-real token already holds real, non-guessed proof
    they were actually invited — telling them to ask for a fresh invitation
    is useful, not an information leak to an unauthenticated guesser."""

    def __init__(self, invitation_id: UUID) -> None:
        self.invitation_id = invitation_id
        super().__init__(f"invitation {invitation_id} has expired")


class MismatchedInvitationEmailError(Exception):
    """The caller's verified Supabase email does not match the invitation's
    target email. Reported distinctly for the same reason as
    :class:`InvitationExpiredError`: reaching this branch already required a
    real, valid Supabase JWT and a real, still-pending invitation token —
    there is nothing left to protect by staying vague."""


@dataclass(frozen=True)
class InvitationRecord:
    invitation_id: UUID
    organization_id: UUID
    email_normalized: str
    role: str
    status: str
    invited_by_user_id: UUID
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    email_status: str
    """`pending`/`sent`/`failed` — the *delivery* status of the invitation
    email, independent of `status` (the invitation's own accept/revoke/expire
    lifecycle). An invitation is fully usable even if `email_status ==
    'failed'`; see :func:`send_invitation_email`."""
    email_error: str | None
    email_sent_at: datetime | None


@dataclass(frozen=True)
class GeneratedInvitation:
    raw_token: str
    """Shown to the caller exactly once. Never persisted or logged."""
    record: InvitationRecord


def _row_to_record(row: Mapping[str, Any]) -> InvitationRecord:
    return InvitationRecord(
        invitation_id=row["invitation_id"],
        organization_id=row["organization_id"],
        email_normalized=row["email_normalized"],
        role=row["role"],
        status=row["status"],
        invited_by_user_id=row["invited_by_user_id"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        accepted_at=row["accepted_at"],
        revoked_at=row["revoked_at"],
        email_status=row["email_status"],
        email_error=row["email_error"],
        email_sent_at=row["email_sent_at"],
    )


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


_SELECT_PENDING_FOR_EMAIL = """
    SELECT invitation_id FROM organization_invitations
    WHERE organization_id = %(organization_id)s AND email_normalized = %(email_normalized)s
      AND status = 'pending'
"""

_INVITATION_COLUMNS = """
    invitation_id, organization_id, email_normalized, role, status,
    invited_by_user_id, created_at, expires_at, accepted_at, revoked_at,
    email_status, email_error, email_sent_at
"""

_INSERT_INVITATION = f"""
    INSERT INTO organization_invitations (
        organization_id, email_normalized, role, token_hash, invited_by_user_id, expires_at
    ) VALUES (
        %(organization_id)s, %(email_normalized)s, %(role)s, %(token_hash)s,
        %(invited_by_user_id)s, %(expires_at)s
    )
    RETURNING {_INVITATION_COLUMNS}
"""

_SELECT_ALL_FOR_ORG = f"""
    SELECT {_INVITATION_COLUMNS} FROM organization_invitations
    WHERE organization_id = %(organization_id)s
    ORDER BY created_at DESC
"""

_SELECT_BY_ID = f"""
    SELECT {_INVITATION_COLUMNS} FROM organization_invitations
    WHERE invitation_id = %(invitation_id)s AND organization_id = %(organization_id)s
"""

_REVOKE = f"""
    UPDATE organization_invitations
    SET status = 'revoked', revoked_at = now()
    WHERE invitation_id = %(invitation_id)s AND organization_id = %(organization_id)s
      AND status = 'pending'
    RETURNING {_INVITATION_COLUMNS}
"""

_SELECT_BY_TOKEN_HASH_FOR_UPDATE = f"""
    SELECT {_INVITATION_COLUMNS} FROM organization_invitations
    WHERE token_hash = %(token_hash)s
    FOR UPDATE
"""

_MARK_ACCEPTED = """
    UPDATE organization_invitations SET status = 'accepted', accepted_at = now()
    WHERE invitation_id = %(invitation_id)s
"""

_MARK_EXPIRED = """
    UPDATE organization_invitations SET status = 'expired'
    WHERE invitation_id = %(invitation_id)s
"""

_SELECT_ACTIVE_MEMBERSHIP = """
    SELECT 1 FROM organization_members
    WHERE organization_id = %(organization_id)s AND user_id = %(user_id)s AND status = 'active'
"""

_UPSERT_MEMBERSHIP = """
    INSERT INTO organization_members (organization_id, user_id, role, status, updated_at)
    VALUES (%(organization_id)s, %(user_id)s, %(role)s, 'active', now())
    ON CONFLICT (organization_id, user_id) DO UPDATE
    SET role = EXCLUDED.role, status = 'active', updated_at = now()
"""
"""Handles both a brand-new member and one re-accepting after being
`removed` — the same row, reactivated with whatever role this invitation
grants, rather than a plain INSERT that would fail on the primary key for
the second case."""


def create_invitation(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    invited_by_user_id: UUID,
    email: str,
    role: str,
) -> GeneratedInvitation:
    """Validate, create, and commit. Raises :class:`InvalidInvitationRoleError`
    for an unrecognised role, :class:`~arie.billing.plans.MemberQuotaExceededError`
    if this organization's plan is already at its member ceiling (Productization
    M6 Part 21 — an invitation still counts against the ceiling even before
    it's accepted, since accepting is normally a formality once invited), and
    :class:`DuplicateInvitationError` if a pending invitation already exists
    for this address — checked twice, once as a pre-check (the common case, a
    clean error with no wasted round trip) and once by catching the partial
    unique index's own violation (the concurrent-request race the pre-check
    alone cannot close).
    """
    if role not in ROLES:
        raise InvalidInvitationRoleError(f"unknown role: {role!r}")
    email_normalized = normalize_email(email)
    enforce_member_quota(conn, organization_id=organization_id)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_PENDING_FOR_EMAIL,
            {"organization_id": organization_id, "email_normalized": email_normalized},
        )
        if cur.fetchone() is not None:
            raise DuplicateInvitationError(
                f"a pending invitation already exists for {email_normalized}"
            )

        raw_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(UTC) + DEFAULT_EXPIRY
        try:
            cur.execute(
                _INSERT_INVITATION,
                {
                    "organization_id": organization_id,
                    "email_normalized": email_normalized,
                    "role": role,
                    "token_hash": _hash_token(raw_token),
                    "invited_by_user_id": invited_by_user_id,
                    "expires_at": expires_at,
                },
            )
            row = cur.fetchone()
        except psycopg.errors.UniqueViolation as exc:
            raise DuplicateInvitationError(
                f"a pending invitation already exists for {email_normalized}"
            ) from exc
    assert row is not None
    record = _row_to_record(row)

    # Safe payload: id, target email, and granted role only — never the raw
    # token (see arie.audit's own contract docstring).
    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=invited_by_user_id,
        event_type="member.invited",
        payload={
            "invitation_id": str(record.invitation_id),
            "email": email_normalized,
            "role": role,
        },
    )
    conn.commit()
    return GeneratedInvitation(raw_token=raw_token, record=record)


def list_invitations(conn: psycopg.Connection, *, organization_id: UUID) -> list[InvitationRecord]:
    """Every invitation ever created for this organization, newest first —
    permanent history; nothing here is ever deleted, only status-transitioned."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_ALL_FOR_ORG, {"organization_id": organization_id})
        rows = cur.fetchall()
    return [_row_to_record(row) for row in rows]


def revoke_invitation(
    conn: psycopg.Connection, *, organization_id: UUID, invitation_id: UUID, actor_user_id: UUID
) -> InvitationRecord | None:
    """Revoke a still-pending invitation and commit. `None` if no *pending*
    invitation with this id exists for this organization — including one
    that belongs to a different organization (never even reachable by this
    query's own `organization_id` filter) or one already resolved, the same
    IDOR-safe, idempotent-on-retry shape `arie.apikeys.revoke_api_key` uses.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_REVOKE, {"invitation_id": invitation_id, "organization_id": organization_id})
        row = cur.fetchone()
    if row is None:
        return None
    record = _row_to_record(row)
    record_event(
        conn,
        organization_id=organization_id,
        actor_user_id=actor_user_id,
        event_type="invitation.revoked",
        payload={"invitation_id": str(invitation_id)},
    )
    conn.commit()
    return record


def accept_invitation(
    conn: psycopg.Connection, *, raw_token: str, verified_email: str, user_id: UUID
) -> InvitationRecord:
    """Accept an invitation on behalf of an already-authenticated, already
    email-verified Supabase user. Commits.

    `SELECT ... FOR UPDATE` on the matched row is what makes replay
    genuinely impossible rather than merely unlikely: a second concurrent
    accept attempt with the same token blocks on this lock until the first
    transaction commits (marking the row `accepted`), then sees a
    non-`pending` status and raises — never a lost-update race where both
    could momentarily believe they were first.

    Raises :class:`InvitationNotFoundError` (no matching / already-resolved
    token), :class:`InvitationExpiredError` (real, was pending, expired —
    opportunistically marks it `expired` before raising),
    :class:`MismatchedInvitationEmailError` (real, live, wrong email), or
    :class:`~arie.billing.plans.MemberQuotaExceededError` if accepting would
    exceed the organization's plan (Productization M6 Part 21) — skipped for
    a user who already holds an *active* membership (a re-invite/role-change
    accept never consumes a new seat).
    """
    token_hash = _hash_token(raw_token)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_BY_TOKEN_HASH_FOR_UPDATE, {"token_hash": token_hash})
        row = cur.fetchone()
        if row is None:
            raise InvitationNotFoundError()
        invitation = _row_to_record(row)

        if invitation.status != "pending":
            raise InvitationNotFoundError()

        if invitation.expires_at <= datetime.now(UTC):
            cur.execute(_MARK_EXPIRED, {"invitation_id": invitation.invitation_id})
            conn.commit()
            raise InvitationExpiredError(invitation.invitation_id)

        if normalize_email(verified_email) != invitation.email_normalized:
            raise MismatchedInvitationEmailError()

        cur.execute(
            _SELECT_ACTIVE_MEMBERSHIP,
            {"organization_id": invitation.organization_id, "user_id": user_id},
        )
        already_active_member = cur.fetchone() is not None
        if not already_active_member:
            enforce_member_quota(conn, organization_id=invitation.organization_id)

        cur.execute(
            _UPSERT_MEMBERSHIP,
            {
                "organization_id": invitation.organization_id,
                "user_id": user_id,
                "role": invitation.role,
            },
        )
        cur.execute(_MARK_ACCEPTED, {"invitation_id": invitation.invitation_id})

    record_event(
        conn,
        organization_id=invitation.organization_id,
        actor_user_id=user_id,
        event_type="invitation.accepted",
        payload={"invitation_id": str(invitation.invitation_id), "role": invitation.role},
    )
    conn.commit()

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_BY_ID,
            {
                "invitation_id": invitation.invitation_id,
                "organization_id": invitation.organization_id,
            },
        )
        final_row = cur.fetchone()
    assert final_row is not None
    return _row_to_record(final_row)


_MARK_EMAIL_STATUS = """
    UPDATE organization_invitations
    SET email_status = %(email_status)s, email_error = %(email_error)s,
        email_sent_at = CASE WHEN %(email_status)s = 'sent' THEN now() ELSE email_sent_at END
    WHERE invitation_id = %(invitation_id)s
"""


def mark_invitation_email_status(
    conn: psycopg.Connection, *, invitation_id: UUID, email_status: str, email_error: str | None
) -> None:
    """Record the outcome of one delivery attempt. Does not commit (matches
    `arie.audit.record_event`'s own "always inside a caller's transaction"
    contract) — :func:`send_invitation_email` is the one caller, and commits
    once after this and its audit event land together.
    """
    with conn.cursor() as cur:
        cur.execute(
            _MARK_EMAIL_STATUS,
            {
                "invitation_id": invitation_id,
                "email_status": email_status,
                "email_error": email_error,
            },
        )


def send_invitation_email(
    conn: psycopg.Connection,
    *,
    invitation: InvitationRecord,
    organization_name: str,
    inviter_email: str,
    accept_url: str,
) -> None:
    """Send the invitation email and record delivery status + an audit event
    (Productization M6 Part 14). Commits. **Never raises for a delivery
    failure** — Part 14's explicit contract is "invitation may exist even if
    email delivery fails"; the invitation itself was already created and
    committed by :func:`create_invitation` before this is ever called, so a
    failure here only means `email_status` ends up `'failed'` rather than
    `'sent'`, with the raw token already gone (never touched by this
    function — see the module docstring) and the invitation otherwise fully
    usable via a resend or by sharing the accept URL out of band.
    """
    from arie.email import get_notifier  # local: keeps this table-owning module's

    # import surface free of the email package for every caller that never
    # sends (list/revoke/accept), matching arie.billing.service's own
    # local-import discipline for optional side effects.
    notifier = get_notifier()
    result = notifier.send_invitation(
        to_email=invitation.email_normalized,
        organization_name=organization_name,
        inviter_email=inviter_email,
        role=invitation.role,
        accept_url=accept_url,
    )
    mark_invitation_email_status(
        conn,
        invitation_id=invitation.invitation_id,
        email_status="sent" if result.delivered else "failed",
        email_error=None if result.delivered else result.error,
    )
    record_event(
        conn,
        organization_id=invitation.organization_id,
        actor_user_id=invitation.invited_by_user_id,
        event_type="invitation.email_sent" if result.delivered else "invitation.email_failed",
        payload={"invitation_id": str(invitation.invitation_id)},
    )
    conn.commit()
