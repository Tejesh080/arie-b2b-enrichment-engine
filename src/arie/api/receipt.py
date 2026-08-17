"""The Decision Receipt — a faithful, read-only rendering of one lead's decision.

Answers, from persisted state only, "why did ARIE stop spending money and make
this decision?" Two sources feed it, deliberately kept separate:

* **Durable, lead-scoped tables** — ``provider_calls``, ``human_reviews``, the
  `human_review:decided` ``lead_events`` row — read live, because they are
  already correct: a call's cost/status/cache_hit never changes after it is
  written, and a review's own compare-and-swap already gives it one true
  final state.
* ``decision_receipts`` (0008) — a snapshot written once, inside
  ``arie.jobs.handlers.compute_score``'s own work transaction, of the handful
  of facts that are *not* safely reconstructable later: score bounds, the
  policy/scorer/calibration identifiers in effect, and which evidence source
  won each field. ``evidence`` (0001_init.sql) is keyed by company/person, not
  by lead, and is shared and mutated by every other lead at that company —
  reading it "now" would describe today's cache, not what was known at
  decision time.

**Never conflates three distinct concepts**, per the module's callers:
recommendation (``decision_receipts.decision`` — frozen, what the policy
concluded), autonomous action (``decision_receipts.autonomous`` plus whether a
``human_reviews`` row exists at all), and final outcome
(``leads.status``/``human_reviews.final_decision`` — live, what actually
happened, possibly after a human overrode the recommendation).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import psycopg
from psycopg.rows import dict_row

from arie.api.reads import fetch_lead
from arie.core.types import LeadStatus
from arie.ledger.store import LeadCost, PostgresCostLedger
from arie.providers.catalog import ALL_PROVIDERS
from arie.scoring.rules import QUALIFY_THRESHOLD, REJECT_THRESHOLD
from arie.statemachine.transitions import FAILURE

RECEIPT_VERSION = "1"

_STOP_REASON_EXPLANATIONS: dict[str, str] = {
    "decision_settled": (
        "Given everything observed so far, no additional evidence could change this "
        "decision — the reachable score range no longer crosses a decision boundary. "
        "This reflects the facts collected, not certainty that they are correct."
    ),
    "confidence_reached": (
        "The calibrated confidence model judged this decision reliable enough to act on "
        "without a human, based on the evidence collected so far."
    ),
    "all_providers_called": (
        "Every available data provider was called; there was no further evidence left to purchase."
    ),
}


def stop_reason_explanation(reason_code: str) -> str:
    return _STOP_REASON_EXPLANATIONS.get(reason_code, f"Processing stopped: {reason_code}.")


@dataclass(frozen=True)
class ReceiptDecision:
    recommended_action: str
    """What the policy concluded — ``Decision`` enum value, frozen at decision time."""
    autonomous: bool
    final_status: LeadStatus
    """The lead's live status — may reflect a human's override of `recommended_action`."""
    human_override: bool
    """True only once a human has responded with a `final_decision` that differs from
    `original_decision` — the same comparison `v_escalation_rate.human_overrode` makes."""


@dataclass(frozen=True)
class ReceiptScoreBounds:
    lower: float
    upper: float


@dataclass(frozen=True)
class ReceiptScore:
    value: float
    threshold_qualify: float
    threshold_reject: float
    bounds: ReceiptScoreBounds
    confidence: float
    tau: float


@dataclass(frozen=True)
class ReceiptStopping:
    reason_code: str
    explanation: str


@dataclass(frozen=True)
class ReceiptCost:
    provider_cost_usd: Decimal
    model_cost_usd: Decimal
    total_cost_usd: Decimal
    budget_usd_cap: Decimal


@dataclass(frozen=True)
class ReceiptEvidenceItem:
    field: str
    source: str
    confidence: float
    contested: bool


@dataclass(frozen=True)
class ReceiptEvidence:
    cache_hits: int
    provider_calls: int
    """Billable calls made — cache hits excluded, matching `LeadCost.provider_calls`."""
    items: tuple[ReceiptEvidenceItem, ...]
    """The winning source per field, as resolved at decision time. Empty until a
    decision has been made."""
    unknown_fields: tuple[str, ...]
    """Fields still unknown when the decision was made — what ARIE decided it didn't
    need to find out."""


@dataclass(frozen=True)
class ReceiptProviderCall:
    provider: str
    status: str
    cost_usd: Decimal
    latency_ms: int | None
    cache_hit: bool


@dataclass(frozen=True)
class ReceiptProviders:
    called: tuple[ReceiptProviderCall, ...]
    not_called: tuple[str, ...]
    """Catalogue providers with no `provider_calls` row for this lead — a set
    difference against the static, cheapest-first catalogue order, not a claim that
    each one was individually evaluated and rejected. See `stopping` for why the
    acquisition loop stopped before reaching them."""


@dataclass(frozen=True)
class ReceiptHumanReview:
    required: bool
    reviewer: str | None
    original_decision: str | None
    action: str | None
    """The reviewer's verb (approve/reject/edit), read from the `human_review:decided`
    lead_event — `None` while the review is still pending."""
    final_decision: str | None
    responded_at: datetime | None


@dataclass(frozen=True)
class ReceiptVersions:
    policy: str
    scorer: str
    confidence_calibration: str


@dataclass(frozen=True)
class DecisionReceipt:
    receipt_version: str
    lead_id: UUID
    status: str
    """"pending" (no decision yet), "processing_failed" (dead-lettered before a
    decision), or "decided"."""
    lead_status: LeadStatus
    created_at: datetime | None
    """When the decision was made — `None` while `status` is not "decided"."""

    decision: ReceiptDecision | None
    score: ReceiptScore | None
    stopping: ReceiptStopping | None
    versions: ReceiptVersions | None

    cost: ReceiptCost
    evidence: ReceiptEvidence
    providers: ReceiptProviders
    human_review: ReceiptHumanReview | None


_SELECT_DECISION_RECEIPT = """
    SELECT decision, autonomous, confidence, tau, score_value, score_lower, score_upper,
           stop_reason, policy_name, scorer_version, confidence_calibration,
           evidence_snapshot, created_at
    FROM decision_receipts
    WHERE lead_id = %(lead_id)s
"""

_SELECT_PROVIDER_CALLS = """
    SELECT provider, status, cost_usd, latency_ms, cache_hit
    FROM provider_calls
    WHERE lead_id = %(lead_id)s
    ORDER BY requested_at
"""

_SELECT_HUMAN_REVIEW = """
    SELECT reviewer, original_decision, final_decision, responded_at
    FROM human_reviews
    WHERE lead_id = %(lead_id)s
    ORDER BY requested_at DESC
    LIMIT 1
"""

_SELECT_HUMAN_REVIEW_ACTION = """
    SELECT payload
    FROM lead_events
    WHERE lead_id = %(lead_id)s AND event_type = 'human_review:decided'
    ORDER BY event_id DESC
    LIMIT 1
"""


def _provider_calls(conn: psycopg.Connection, lead_id: UUID) -> tuple[ReceiptProviderCall, ...]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_PROVIDER_CALLS, {"lead_id": lead_id})
        rows = cur.fetchall()
    return tuple(
        ReceiptProviderCall(
            provider=row["provider"],
            status=row["status"],
            cost_usd=row["cost_usd"] if row["cost_usd"] is not None else Decimal(0),
            latency_ms=row["latency_ms"],
            cache_hit=row["cache_hit"],
        )
        for row in rows
    )


def _human_review(
    conn: psycopg.Connection, lead_id: UUID
) -> tuple[ReceiptHumanReview | None, bool]:
    """The lead's most recent review, and whether it represents a human override."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_HUMAN_REVIEW, {"lead_id": lead_id})
        row = cur.fetchone()
    if row is None:
        return None, False

    action: str | None = None
    if row["responded_at"] is not None:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_SELECT_HUMAN_REVIEW_ACTION, {"lead_id": lead_id})
            action_row = cur.fetchone()
        if action_row is not None:
            action = action_row["payload"].get("action")

    human_override = (
        row["responded_at"] is not None
        and row["final_decision"] is not None
        and row["original_decision"] is not None
        and row["final_decision"] != row["original_decision"]
    )

    return (
        ReceiptHumanReview(
            required=True,
            reviewer=row["reviewer"],
            original_decision=row["original_decision"],
            action=action,
            final_decision=row["final_decision"],
            responded_at=row["responded_at"],
        ),
        human_override,
    )


def _evidence_items(snapshot: dict[str, Any]) -> tuple[ReceiptEvidenceItem, ...]:
    return tuple(
        ReceiptEvidenceItem(
            field=item["field"],
            source=item["source"],
            confidence=float(item["confidence"]),
            contested=bool(item["contested"]),
        )
        for item in snapshot.get("known", [])
    )


def build_receipt(
    conn: psycopg.Connection, ledger: PostgresCostLedger, lead_id: UUID
) -> DecisionReceipt | None:
    """Compose one lead's receipt. Returns `None` only for an unknown lead (404)."""
    lead = fetch_lead(conn, lead_id)
    if lead is None:
        return None

    cost: LeadCost | None = ledger.lead_cost(lead_id)
    assert cost is not None  # v_lead_cost is LEFT JOINed off leads; the row exists

    calls = _provider_calls(conn, lead_id)
    called_names = {call.provider for call in calls}
    not_called = tuple(name for name in ALL_PROVIDERS if name not in called_names)
    human_review, human_override = _human_review(conn, lead_id)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_DECISION_RECEIPT, {"lead_id": lead_id})
        receipt_row = cur.fetchone()

    receipt_cost = ReceiptCost(
        provider_cost_usd=cost.provider_cost_usd,
        model_cost_usd=cost.model_cost_usd,
        total_cost_usd=cost.total_cost_usd,
        budget_usd_cap=lead.budget_usd_cap,
    )

    if receipt_row is None:
        status = "processing_failed" if lead.status in FAILURE else "pending"
        return DecisionReceipt(
            receipt_version=RECEIPT_VERSION,
            lead_id=lead_id,
            status=status,
            lead_status=lead.status,
            created_at=None,
            decision=None,
            score=None,
            stopping=None,
            versions=None,
            cost=receipt_cost,
            evidence=ReceiptEvidence(
                cache_hits=cost.cache_hits,
                provider_calls=cost.provider_calls,
                items=(),
                unknown_fields=(),
            ),
            providers=ReceiptProviders(called=calls, not_called=not_called),
            human_review=human_review,
        )

    snapshot = receipt_row["evidence_snapshot"] or {}
    return DecisionReceipt(
        receipt_version=RECEIPT_VERSION,
        lead_id=lead_id,
        status="decided",
        lead_status=lead.status,
        created_at=receipt_row["created_at"],
        decision=ReceiptDecision(
            recommended_action=receipt_row["decision"],
            autonomous=receipt_row["autonomous"],
            final_status=lead.status,
            human_override=human_override,
        ),
        score=ReceiptScore(
            value=float(receipt_row["score_value"]),
            threshold_qualify=QUALIFY_THRESHOLD,
            threshold_reject=REJECT_THRESHOLD,
            bounds=ReceiptScoreBounds(
                lower=float(receipt_row["score_lower"]), upper=float(receipt_row["score_upper"])
            ),
            confidence=float(receipt_row["confidence"]),
            tau=float(receipt_row["tau"]),
        ),
        stopping=ReceiptStopping(
            reason_code=receipt_row["stop_reason"],
            explanation=stop_reason_explanation(receipt_row["stop_reason"]),
        ),
        versions=ReceiptVersions(
            policy=receipt_row["policy_name"],
            scorer=receipt_row["scorer_version"],
            confidence_calibration=receipt_row["confidence_calibration"],
        ),
        cost=receipt_cost,
        evidence=ReceiptEvidence(
            cache_hits=cost.cache_hits,
            provider_calls=cost.provider_calls,
            items=_evidence_items(snapshot),
            unknown_fields=tuple(snapshot.get("unknown", [])),
        ),
        providers=ReceiptProviders(called=calls, not_called=not_called),
        human_review=human_review,
    )
