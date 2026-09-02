"""What a customer thought of ARIE's customer-facing recommendation for one lead.

An observation, never a mutation — see ``migrations/0036_lead_recommendation_
feedback.sql`` for the invariant this module exists to keep: submitting
feedback changes no score, no profile, no decision, and calls no provider or
model. A later slice aggregates these rows into
``arie.intelligence.proposals.ProposalSource.USER_FEEDBACK``; this module only
records them.

One active row per ``(lead_id, user_id)`` — a person changing their mind about
a lead replaces the earlier verdict (``ON CONFLICT`` upsert) rather than
appending a second one, which is what "duplicate click is idempotent" and
"changing feedback" both need from the same code path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.recommendations import CustomerPriority, NextAction

__all__ = [
    "FeedbackAggregate",
    "FeedbackReason",
    "FeedbackRecord",
    "FeedbackSentiment",
    "aggregate_feedback",
    "get_feedback",
    "submit_feedback",
]


class FeedbackSentiment(StrEnum):
    POSITIVE = "positive"
    NEGATIVE = "negative"


class FeedbackReason(StrEnum):
    """A small, closed vocabulary — not free text. See migration 0036's CHECK
    constraint, which enforces the same list at the database boundary."""

    GOOD_MATCH = "good_match"
    BAD_MATCH = "bad_match"
    WRONG_PERSON = "wrong_person"
    COMPANY_TOO_SMALL = "company_too_small"
    COMPANY_TOO_LARGE = "company_too_large"
    WRONG_INDUSTRY = "wrong_industry"
    NOT_DECISION_MAKER = "not_decision_maker"
    ALREADY_CUSTOMER = "already_customer"
    NOT_INTERESTED = "not_interested"
    OTHER = "other"


@dataclass(frozen=True)
class FeedbackRecord:
    feedback_id: UUID
    organization_id: UUID
    lead_id: UUID
    user_id: UUID
    profile_version: int | None
    recommendation_priority: str
    recommendation_next_action: str
    score_snapshot: Decimal | None
    sentiment: str
    reason: str | None
    note: str | None
    created_by_user_id: UUID
    created_at: datetime
    updated_at: datetime


def _to_record(row: dict[str, Any]) -> FeedbackRecord:
    return FeedbackRecord(
        feedback_id=row["feedback_id"],
        organization_id=row["organization_id"],
        lead_id=row["lead_id"],
        user_id=row["user_id"],
        profile_version=row["profile_version"],
        recommendation_priority=row["recommendation_priority"],
        recommendation_next_action=row["recommendation_next_action"],
        score_snapshot=row["score_snapshot"],
        sentiment=row["sentiment"],
        reason=row["reason"],
        note=row["note"],
        created_by_user_id=row["created_by_user_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


_COLUMNS = """
    feedback_id, organization_id, lead_id, user_id, profile_version,
    recommendation_priority, recommendation_next_action, score_snapshot,
    sentiment, reason, note, created_by_user_id, created_at, updated_at
"""

_UPSERT = f"""
    INSERT INTO lead_recommendation_feedback (
        organization_id, lead_id, user_id, profile_version,
        recommendation_priority, recommendation_next_action, score_snapshot,
        sentiment, reason, note, created_by_user_id
    ) VALUES (
        %(organization_id)s, %(lead_id)s, %(user_id)s, %(profile_version)s,
        %(recommendation_priority)s, %(recommendation_next_action)s, %(score_snapshot)s,
        %(sentiment)s, %(reason)s, %(note)s, %(user_id)s
    )
    ON CONFLICT (lead_id, user_id) DO UPDATE SET
        profile_version = EXCLUDED.profile_version,
        recommendation_priority = EXCLUDED.recommendation_priority,
        recommendation_next_action = EXCLUDED.recommendation_next_action,
        score_snapshot = EXCLUDED.score_snapshot,
        sentiment = EXCLUDED.sentiment,
        reason = EXCLUDED.reason,
        note = EXCLUDED.note,
        updated_at = now()
    RETURNING {_COLUMNS}
"""

_SELECT_ONE = f"""
    SELECT {_COLUMNS} FROM lead_recommendation_feedback
    WHERE organization_id = %(organization_id)s AND lead_id = %(lead_id)s AND user_id = %(user_id)s
"""

_SELECT_FOR_AGGREGATE = """
    SELECT recommendation_priority, sentiment, reason, profile_version
    FROM lead_recommendation_feedback
    WHERE organization_id = %(organization_id)s
"""


def submit_feedback(
    conn: psycopg.Connection,
    *,
    organization_id: UUID,
    lead_id: UUID,
    user_id: UUID,
    sentiment: FeedbackSentiment,
    reason: FeedbackReason | None,
    note: str | None,
    priority: CustomerPriority,
    next_action: NextAction,
    profile_version: int | None,
    score_snapshot: Decimal | float | None,
) -> FeedbackRecord:
    """Record one person's verdict on the recommendation they were shown. Commits.

    `priority`/`next_action`/`profile_version`/`score_snapshot` are exactly
    what the caller showed this user — captured here rather than re-derived
    from the lead's current (possibly since-changed) state, per migration
    0036's own reasoning.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _UPSERT,
            {
                "organization_id": organization_id,
                "lead_id": lead_id,
                "user_id": user_id,
                "profile_version": profile_version,
                "recommendation_priority": str(priority),
                "recommendation_next_action": str(next_action),
                "score_snapshot": (
                    Decimal(str(score_snapshot)) if score_snapshot is not None else None
                ),
                "sentiment": str(sentiment),
                "reason": str(reason) if reason is not None else None,
                "note": note,
            },
        )
        row = cur.fetchone()
    assert row is not None
    conn.commit()
    return _to_record(row)


def get_feedback(
    conn: psycopg.Connection, *, organization_id: UUID, lead_id: UUID, user_id: UUID
) -> FeedbackRecord | None:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            _SELECT_ONE,
            {"organization_id": organization_id, "lead_id": lead_id, "user_id": user_id},
        )
        row = cur.fetchone()
    return _to_record(row) if row is not None else None


@dataclass(frozen=True)
class FeedbackAggregate:
    """Deterministic counts over every feedback row an organization has —
    the foundation a future slice's proposal-from-feedback logic reads,
    not a proposal generator itself. No model call, no LLM cost."""

    total: int
    positive: int
    negative: int
    by_priority: dict[str, dict[str, int]]
    """`{priority: {"positive": n, "negative": n}}` — lets a caller compute a
    per-priority agreement rate without a second query."""
    negative_reason_counts: dict[str, int]

    @property
    def agreement_rate(self) -> float | None:
        return (self.positive / self.total) if self.total else None


def aggregate_feedback(conn: psycopg.Connection, *, organization_id: UUID) -> FeedbackAggregate:
    """Summarize every feedback row for `organization_id`. Pure arithmetic —
    no model, no cost, and no write. Deliberately not filtered by
    `profile_version`: a caller wanting one version's numbers filters
    `by_priority`'s source rows itself once this needs that granularity."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_FOR_AGGREGATE, {"organization_id": organization_id})
        rows = cur.fetchall()

    by_priority: dict[str, dict[str, int]] = {}
    negative_reasons: dict[str, int] = {}
    positive = 0
    negative = 0
    for row in rows:
        priority = row["recommendation_priority"]
        sentiment = row["sentiment"]
        bucket = by_priority.setdefault(priority, {"positive": 0, "negative": 0})
        bucket[sentiment] = bucket.get(sentiment, 0) + 1
        if sentiment == str(FeedbackSentiment.POSITIVE):
            positive += 1
        else:
            negative += 1
            if row["reason"]:
                negative_reasons[row["reason"]] = negative_reasons.get(row["reason"], 0) + 1

    return FeedbackAggregate(
        total=len(rows),
        positive=positive,
        negative=negative,
        by_priority=by_priority,
        negative_reason_counts=negative_reasons,
    )
