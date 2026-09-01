"""Usage-warning / limit-reached emails (Productization M6 Part 15). No new
table: dedup ("send at most once per organization per metric per billing
period") is a plain query against `organization_audit_events`
(`billing.usage_warning_sent` / `billing.limit_reached_sent`, filtered to
this calendar period) — the same append-only log Productization M4 already
writes for every other organization action, reused rather than duplicated
into a second bookkeeping table for one narrow purpose.

Best-effort and non-blocking by convention: every caller
(`arie.api.main`'s lead/batch ingestion routes) invokes this *after* its own
transaction has committed, on a fresh connection, and treats any exception
here as a no-op — a missed usage-warning email is recoverable (the Usage
page still shows the real numbers); it must never fail a lead/batch
ingestion request.
"""

from __future__ import annotations

import logging
from datetime import datetime
from uuid import UUID

import psycopg

from arie.audit import SYSTEM_ACTOR_ID, record_event
from arie.config import FRONTEND, NOTIFICATIONS
from arie.email import get_notifier
from arie.limits import get_usage_against_limits
from arie.members import list_members
from arie.organizations import get_organization
from arie.supabase_admin import get_user_email

__all__ = ["check_and_notify_usage"]

_LOGGER = logging.getLogger("arie.usage_notifications")

_ALREADY_SENT = """
    SELECT 1 FROM organization_audit_events
    WHERE organization_id = %(organization_id)s AND event_type = %(event_type)s
      AND payload->>'metric' = %(metric)s AND created_at >= %(period_start)s
    LIMIT 1
"""


def _already_sent(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    event_type: str,
    metric: str,
    period_start: datetime,
) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            _ALREADY_SENT,
            {
                "organization_id": organization_id,
                "event_type": event_type,
                "metric": metric,
                "period_start": period_start,
            },
        )
        return cur.fetchone() is not None


def check_and_notify_usage(
    conn: psycopg.Connection, *, organization_id: UUID, now: datetime
) -> None:
    """Send at most one warning and one limit-reached email per metric
    (`leads`, `modeled_spend`) per calendar month, to every active owner/
    admin. Never raises — see the module docstring.
    """
    try:
        usage = get_usage_against_limits(conn, organization_id=organization_id, now=now)
        metrics = [
            ("leads", usage.leads_used, usage.leads_limit),
            ("modeled_spend", usage.modeled_spend_used_usd, usage.modeled_spend_limit_usd),
        ]
        triggers: list[tuple[str, str, float, float]] = []
        for metric, used, limit in metrics:
            if limit <= 0:
                continue
            fraction = used / limit
            if fraction >= 1.0 and not _already_sent(
                conn,
                organization_id=organization_id,
                event_type="billing.limit_reached_sent",
                metric=metric,
                period_start=usage.period_start,
            ):
                triggers.append(("limit_reached", metric, used, limit))
            elif fraction >= NOTIFICATIONS.usage_warning_threshold and not _already_sent(
                conn,
                organization_id=organization_id,
                event_type="billing.usage_warning_sent",
                metric=metric,
                period_start=usage.period_start,
            ):
                triggers.append(("usage_warning", metric, used, limit))

        if not triggers:
            return

        organization = get_organization(conn, organization_id=organization_id)
        if organization is None:
            return
        recipients = [
            m
            for m in list_members(conn, organization_id=organization_id)
            if m.role in ("owner", "admin")
        ]
        emails = [e for e in (get_user_email(m.user_id) for m in recipients) if e is not None]
        if not emails:
            return

        notifier = get_notifier()
        billing_url = f"{FRONTEND.base_url}/settings/billing"
        for kind, metric, used, limit in triggers:
            metric_label = "leads/month" if metric == "leads" else "modeled spend/month"
            for email in emails:
                if kind == "limit_reached":
                    notifier.send_limit_reached(
                        to_email=email,
                        organization_name=organization.name,
                        metric_label=metric_label,
                        billing_url=billing_url,
                    )
                else:
                    notifier.send_usage_warning(
                        to_email=email,
                        organization_name=organization.name,
                        metric_label=metric_label,
                        used=used,
                        limit=limit,
                        percent=used / limit,
                        billing_url=billing_url,
                    )
            event_type = (
                "billing.limit_reached_sent"
                if kind == "limit_reached"
                else "billing.usage_warning_sent"
            )
            record_event(
                conn,
                organization_id=organization_id,
                actor_user_id=SYSTEM_ACTOR_ID,
                event_type=event_type,
                payload={"metric": metric},
            )
        conn.commit()
    except Exception:
        _LOGGER.exception("usage notification check failed for organization %s", organization_id)
