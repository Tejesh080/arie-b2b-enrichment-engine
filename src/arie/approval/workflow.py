"""The human review workflow: request creation and decision submission.

Built *around* the existing `human_reviews` table (0001_init.sql) and the
DECISION <-> AWAITING_HUMAN edge of `arie.statemachine` — no new tables, and
the only schema change is one partial unique index (0006_human_review_
workflow.sql). Mirrors `arie.api.ingest`'s shape deliberately: functions take
a caller-provided connection, compose more than one write, and do not commit
— the caller decides the transaction boundary, exactly as ingestion composes
identity resolution, the lead row, and its first job into one commit.

Two entry points:

- `request_review` — moves a lead DECISION -> AWAITING_HUMAN and opens the
  pending `human_reviews` row atomically. Called in production by
  `arie.jobs.handlers`' compute_score handler, whose escalation branch is
  exactly this: a non-autonomous decision (or one the policy itself labelled
  escalate_human) becomes a pending review in the same work transaction.
- `submit_decision` — the human's answer. See its docstring for the
  idempotency and conflict-safety guarantees, which are the point of this
  module.

**Preserves the is_settled/confidence distinction by construction, not by
restating it.** Neither function recomputes or inspects score bounds or
calibrated confidence — `original_decision` is an opaque label the caller
(eventually a `finalize_decision` handler with access to the real
`DecisionOutcome`) already derived. This module only ever compares it against
what the human decided; recomputing "was the model actually right" here would
be exactly the boundary M0's scoring/confidence code owns, not M1's review
layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.core.types import LeadStatus
from arie.observability.tracing import get_tracer, set_attributes, traced
from arie.statemachine.apply import apply_transition
from arie.statemachine.transitions import next_status

_TRACER = get_tracer("arie.approval")


class ReviewAction(StrEnum):
    """What a reviewer does. Distinct from the `final_decision` vocabulary
    (`_FINAL_DECISION` below) on purpose — this is the human-facing verb; the
    domain still speaks in the decision labels the state graph
    (`arie.statemachine.transitions.HUMAN_REVIEW_OUTCOMES`) and
    `v_escalation_rate`'s override detection already understand."""

    APPROVE = "approve"
    REJECT = "reject"
    EDIT = "edit"


_FINAL_DECISION: dict[ReviewAction, str] = {
    ReviewAction.APPROVE: "auto_route",
    ReviewAction.REJECT: "reject",
    ReviewAction.EDIT: "manual_review",
}
"""Maps a reviewer's action to the decision-label vocabulary `human_reviews.
original_decision`/`final_decision` already use (see the hand-written
fixtures in tests/integration/test_cost_ledger_integration.py, written before
this module existed) and that `HUMAN_REVIEW_OUTCOMES` is keyed by. Every value
here must be a valid `HUMAN_REVIEW_OUTCOMES` key -- pinned by
test_every_review_action_maps_to_a_human_review_outcome."""


class ReviewNotFoundError(LookupError):
    def __init__(self, review_id: UUID) -> None:
        self.review_id = review_id
        super().__init__(f"no human review {review_id}")


class ReviewConflictError(RuntimeError):
    """Raised when a review already carries a *different* recorded decision.

    Distinct from `arie.statemachine.apply.OptimisticConcurrencyError`: this
    is a conflict on the review's own one-time-answerable state, not on the
    lead's version. The API layer maps both to 409, but they mean different
    things -- see `submit_decision`'s docstring.
    """

    def __init__(self, review_id: UUID) -> None:
        self.review_id = review_id
        super().__init__(
            f"review {review_id} was already decided differently — refusing to overwrite"
        )


@dataclass(frozen=True)
class PendingReview:
    review_id: UUID
    created: bool
    """False if the lead already had a pending review and this call returned
    it rather than opening a second one (0006's partial unique index)."""


@dataclass(frozen=True)
class ReviewRecord:
    review_id: UUID
    lead_id: UUID
    requested_at: datetime
    reviewer: str | None
    original_decision: str | None
    final_decision: str | None
    notes: str | None
    responded_at: datetime | None
    lead_status: LeadStatus
    lead_version: int
    """The lead's *current* version, read fresh alongside the review -- not a
    snapshot from when the review was opened. A client submitting a decision
    needs the version to submit *now*, matching the optimistic-concurrency
    contract every other write in this codebase uses."""

    @property
    def is_pending(self) -> bool:
        return self.responded_at is None


@dataclass(frozen=True)
class DecisionResult:
    review_id: UUID
    lead_id: UUID
    action: ReviewAction
    final_decision: str
    reviewer: str
    notes: str | None
    responded_at: datetime
    lead_status: LeadStatus
    lead_version: int
    already_applied: bool
    """True if this call found an existing, identical decision already
    recorded and returned it rather than applying anything a second time --
    the idempotent-retry path. False the one time a submission actually
    changes anything."""


_REQUEST_REVIEW = """
    INSERT INTO human_reviews (lead_id, reviewer, original_decision)
    VALUES (%(lead_id)s, %(reviewer)s, %(original_decision)s)
    ON CONFLICT (lead_id) WHERE responded_at IS NULL
        DO UPDATE SET lead_id = human_reviews.lead_id
    RETURNING review_id, (xmax = 0) AS created
"""

_SELECT_REVIEW = """
    SELECT hr.review_id, hr.lead_id, hr.requested_at, hr.reviewer,
           hr.original_decision, hr.final_decision, hr.notes, hr.responded_at,
           l.status AS lead_status, l.version AS lead_version
    FROM human_reviews hr
    JOIN leads l ON l.lead_id = hr.lead_id
    WHERE hr.review_id = %(review_id)s
"""

_COMPLETE_REVIEW = """
    UPDATE human_reviews
    SET reviewer = %(reviewer)s, final_decision = %(final_decision)s,
        notes = %(notes)s, responded_at = now()
    WHERE review_id = %(review_id)s AND responded_at IS NULL
    RETURNING lead_id, responded_at
"""


def request_review(
    conn: psycopg.Connection,
    *,
    lead_id: UUID,
    expected_version: int,
    original_decision: str | None,
    reviewer: str | None = None,
    reason: str | None = None,
) -> PendingReview:
    """Escalate a lead to a human, atomically. Does not commit.

    Transitions DECISION -> AWAITING_HUMAN via `apply_transition` (a stale
    `expected_version` raises `OptimisticConcurrencyError`, exactly as every
    other transition in this codebase) and opens the pending review row in
    the same transaction. A crash between the two would otherwise strand a
    lead in AWAITING_HUMAN with no review for a human to act on -- which looks
    exactly like a lead genuinely mid-review and is just as hard to notice as
    the lost-job window `arie.api.ingest`'s docstring describes for ingestion.

    `reason` (Live V1 Foundation) records *why* the escalation happened when it
    was not the ordinary "the policy was not confident enough" -- specifically
    ``arie.live.safety.LIVE_GUARD_REASON``, where a confident recommendation
    was overridden because live-mode autonomy is not yet validated. It goes
    into the `lead:escalated` event payload rather than onto `human_reviews`:
    the reason describes this one escalation event, the review row is about
    the review's lifecycle, and a nullable column that is null for every
    simulated lead ever escalated would be the wrong shape for both. `None`
    keeps the payload byte-identical to before, so no existing consumer of
    that event sees a new key.
    """
    with traced(
        _TRACER,
        "review.request",
        attributes={
            "arie.lead_id": lead_id,
            "arie.original_decision": original_decision,
            "arie.escalation_reason": reason,
        },
    ) as span:
        payload: dict[str, str | None] = {"original_decision": original_decision}
        if reason is not None:
            payload["reason"] = reason
        apply_transition(
            conn,
            lead_id=lead_id,
            expected_version=expected_version,
            new_status=LeadStatus.AWAITING_HUMAN,
            event_type="lead:escalated",
            payload=payload,
        )
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _REQUEST_REVIEW,
                {"lead_id": lead_id, "reviewer": reviewer, "original_decision": original_decision},
            )
            row = cur.fetchone()
            assert row is not None
        result = PendingReview(review_id=row["review_id"], created=row["created"])
        set_attributes(
            span, {"arie.review_id": result.review_id, "arie.review.created": result.created}
        )
        return result


def get_review(conn: psycopg.Connection, review_id: UUID) -> ReviewRecord | None:
    """Fetch one review, joined with the lead's live status and version."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_REVIEW, {"review_id": review_id})
        row = cur.fetchone()
    if row is None:
        return None
    return ReviewRecord(
        review_id=row["review_id"],
        lead_id=row["lead_id"],
        requested_at=row["requested_at"],
        reviewer=row["reviewer"],
        original_decision=row["original_decision"],
        final_decision=row["final_decision"],
        notes=row["notes"],
        responded_at=row["responded_at"],
        lead_status=LeadStatus(row["lead_status"]),
        lead_version=row["lead_version"],
    )


def submit_decision(
    conn: psycopg.Connection,
    *,
    review_id: UUID,
    action: ReviewAction,
    reviewer: str,
    notes: str | None,
    expected_lead_version: int,
) -> DecisionResult:
    """Record a human's decision and transition the lead, atomically.

    **Idempotent by content, not by a supplied token** -- matching this
    codebase's preference for natural keys over synthetic ones (`leads`'
    `(source, external_ref)`, every `idempotency_key` derived internally
    rather than accepted from a caller). The compare-and-swap
    `UPDATE ... WHERE responded_at IS NULL` is the one moment a review can
    ever be completed. Under a genuine race, Postgres's row lock on that
    UPDATE serializes the two attempts under the default READ COMMITTED
    isolation this codebase relies on elsewhere (see `arie.statemachine.
    apply`): the second attempt blocks, then re-evaluates its WHERE clause
    against whatever the first attempt's transaction left behind once it ends.

    Two outcomes for whichever attempt loses that race:

    - **Identical** (`action`, `reviewer`, `notes`) to what's already
      recorded -- a client retry (timeout, redelivered request) resending the
      same submission. Returns the winner's result with
      `already_applied=True` rather than erroring or re-applying the
      transition a second time.
    - **Different** content -- a genuine conflicting submission (two
      reviewers, or the same reviewer changing their mind after their first
      call already landed). Raises `ReviewConflictError`; the API layer turns
      that into 409 and the client re-fetches to see what won.

    If the first attempt's transaction instead *rolls back* (its own
    `expected_lead_version` was stale -- see below), the CAS never committed,
    so a second attempt sees `responded_at IS NULL` exactly as if the first
    never happened and can legitimately win. Nothing here is left half-applied
    either way.

    `expected_lead_version` is the second, independent guard: even a
    submission that wins its own CAS still transitions the lead through
    `apply_transition`, which raises `OptimisticConcurrencyError` if the lead
    moved since the version this call was given -- the same protection every
    other writer in this codebase gets, not a special case carved out for
    review completion. Raising there unwinds the whole transaction, including
    the CAS update above, so the review reverts to genuinely pending and is
    answerable again once the client re-fetches the lead's current version.

    Raises `ReviewNotFoundError` if `review_id` doesn't exist. Does not
    commit -- the caller commits.
    """
    final_decision = _FINAL_DECISION[action]

    with traced(
        _TRACER,
        "review.submit_decision",
        attributes={
            "arie.review_id": review_id,
            "arie.review.action": str(action),
            "arie.reviewer": reviewer,
        },
    ) as span:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _COMPLETE_REVIEW,
                {
                    "review_id": review_id,
                    "reviewer": reviewer,
                    "final_decision": final_decision,
                    "notes": notes,
                },
            )
            won = cur.fetchone()

        if won is None:
            result = _resolve_lost_race(
                conn,
                review_id=review_id,
                action=action,
                final_decision=final_decision,
                reviewer=reviewer,
                notes=notes,
            )
        else:
            result = _apply_winning_decision(
                conn,
                review_id=review_id,
                lead_id=won["lead_id"],
                responded_at=won["responded_at"],
                action=action,
                final_decision=final_decision,
                reviewer=reviewer,
                notes=notes,
                expected_lead_version=expected_lead_version,
            )

        set_attributes(
            span,
            {
                "arie.lead_id": result.lead_id,
                "arie.lead.new_status": str(result.lead_status),
                "arie.review.already_applied": result.already_applied,
            },
        )
        return result


def _apply_winning_decision(
    conn: psycopg.Connection,
    *,
    review_id: UUID,
    lead_id: UUID,
    responded_at: datetime,
    action: ReviewAction,
    final_decision: str,
    reviewer: str,
    notes: str | None,
    expected_lead_version: int,
) -> DecisionResult:
    new_status = next_status(LeadStatus.AWAITING_HUMAN, outcome=final_decision)
    assert new_status is not None  # every _FINAL_DECISION value is a HUMAN_REVIEW_OUTCOMES key

    transition = apply_transition(
        conn,
        lead_id=lead_id,
        expected_version=expected_lead_version,
        new_status=new_status,
        event_type="human_review:decided",
        payload={"review_id": str(review_id), "action": str(action), "reviewer": reviewer},
    )

    return DecisionResult(
        review_id=review_id,
        lead_id=lead_id,
        action=action,
        final_decision=final_decision,
        reviewer=reviewer,
        notes=notes,
        responded_at=responded_at,
        lead_status=new_status,
        lead_version=transition.new_version,
        already_applied=False,
    )


def _resolve_lost_race(
    conn: psycopg.Connection,
    *,
    review_id: UUID,
    action: ReviewAction,
    final_decision: str,
    reviewer: str,
    notes: str | None,
) -> DecisionResult:
    existing = get_review(conn, review_id)
    if existing is None:
        raise ReviewNotFoundError(review_id)
    if existing.responded_at is None:
        # Reachable only if a review could be completed outside this CAS, or
        # across two isolation-level assumptions this module doesn't make --
        # see submit_decision's docstring. Kept as a guard, not a case this
        # code path is expected to hit.
        raise AssertionError(
            f"review {review_id}: lost the completion race but the row is still "
            "pending -- should be unreachable under READ COMMITTED"
        )
    if (
        existing.final_decision != final_decision
        or existing.reviewer != reviewer
        or existing.notes != notes
    ):
        raise ReviewConflictError(review_id)

    return DecisionResult(
        review_id=review_id,
        lead_id=existing.lead_id,
        action=action,
        final_decision=final_decision,
        reviewer=reviewer,
        notes=notes,
        responded_at=existing.responded_at,
        lead_status=existing.lead_status,
        lead_version=existing.lead_version,
        already_applied=True,
    )
