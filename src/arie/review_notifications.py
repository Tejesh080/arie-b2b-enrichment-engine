"""Notify an organization's owners/admins when a lead reaches
`AWAITING_HUMAN` (Productization M6 Part 16). Best-effort and
non-transactional by design: `arie.approval.workflow.request_review` opens
the `human_reviews` row inside the worker's own job transaction and does not
commit it itself, so nothing here can run until *after* that transaction has
actually committed — see `arie.jobs.worker._process_one`'s own call site,
the only caller. A failure anywhere in this module is caught and logged,
never re-raised into the worker loop: a missed email is recoverable (the
reviewer still sees the lead in the console); a job that fails to complete
because a notification provider was down is not.
"""

from __future__ import annotations

import logging
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.config import FRONTEND
from arie.email import get_notifier
from arie.members import list_members
from arie.organizations import get_organization
from arie.supabase_admin import get_user_email

__all__ = ["notify_review_required"]

_LOGGER = logging.getLogger("arie.review_notifications")

_SELECT_PENDING_REVIEW = """
    SELECT review_id, organization_id, original_decision
    FROM human_reviews
    WHERE lead_id = %(lead_id)s AND responded_at IS NULL
    ORDER BY requested_at DESC
    LIMIT 1
"""

_DEDUP_INSERT = """
    INSERT INTO human_review_notifications (review_id) VALUES (%(review_id)s)
    ON CONFLICT (review_id) DO NOTHING
    RETURNING review_id
"""


def notify_review_required(pool: ConnectionPool, *, lead_id: UUID) -> None:
    """Send (at most once per review) a "lead awaiting review" email to
    every active owner/admin of the review's organization. Silently returns
    if there is no pending review for `lead_id` (a race with the review
    already being answered), if it was already notified (the dedup insert
    finds nothing to do), or if any step fails — see the module docstring.
    """
    try:
        with pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(_SELECT_PENDING_REVIEW, {"lead_id": lead_id})
                review_row = cur.fetchone()
            if review_row is None:
                return
            review_id = review_row["review_id"]
            organization_id = review_row["organization_id"]
            original_decision = review_row["original_decision"]

            with conn.cursor() as cur:
                cur.execute(_DEDUP_INSERT, {"review_id": review_id})
                already_notified = cur.fetchone() is None
            conn.commit()
            if already_notified:
                return

            organization = get_organization(conn, organization_id=organization_id)
            if organization is None:
                return
            recipients = [
                member
                for member in list_members(conn, organization_id=organization_id)
                if member.role in ("owner", "admin")
            ]

        notifier = get_notifier()
        review_url = f"{FRONTEND.base_url}/reviews/{review_id}"
        lead_reference = str(lead_id)[:8]
        summary = (
            f"Machine recommendation: {original_decision}."
            if original_decision
            else "This lead requires a human decision."
        )
        for member in recipients:
            email = get_user_email(member.user_id)
            if email is None:
                continue
            notifier.send_review_required(
                to_email=email,
                organization_name=organization.name,
                lead_reference=lead_reference,
                summary=summary,
                review_url=review_url,
            )
    except Exception:
        _LOGGER.exception("failed to send review-required notification for lead %s", lead_id)
