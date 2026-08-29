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
from arie.live.budget import DAILY_BUDGET_EXHAUSTED, PER_LEAD_BUDGET_EXHAUSTED
from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
from arie.live.safety import LIVE_GUARD_REASON
from arie.live.strategy import LIVE_POLICY_NAMES as LIVE_POLICY_NAME_SET
from arie.providers.catalog import ALL_PROVIDERS
from arie.scoring.rules import QUALIFY_THRESHOLD, REJECT_THRESHOLD
from arie.statemachine.transitions import FAILURE

LIVE_PROVIDER_NAMES: tuple[str, ...] = REGISTERED_LIVE_PROVIDER_NAMES
"""Kept as a module-level name for existing callers/tests; the definition now
lives in ``arie.live.providers`` so the spend caps and this receipt cannot
disagree about which providers are real. Which receipts count as live is
likewise centralised: ``arie.live.strategy.LIVE_POLICY_NAMES`` covers the
optimized and evaluation strategies plus the legacy single-provider name that
stored rows still carry."""

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
    "no_domain_available": (
        "Live provider mode. No data provider could be called for this lead at all — none "
        "of them had an identifier of the kind it needs (typically a company domain this "
        "lead never resolved one for). The decision reflects whatever evidence was already "
        "known, not certainty that none exists."
    ),
    "provider_failed": (
        "A live data provider failed to respond usably (a timeout, or a transport or API "
        "error). No evidence was purchased from it. The decision reflects only what was "
        "already known — this lead was not assessed on complete information, and the "
        "failure is recorded against the provider rather than against the lead. Other "
        "providers may still have answered; see the per-provider list above."
    ),
    "provider_unavailable": (
        "A live data provider was deliberately not called because it recently reported its "
        "account allowance exhausted (a credit/quota error) and is in a temporary cooldown. "
        "No call was made and nothing was spent on it; acquisition continued with the other "
        "providers. The decision reflects the evidence that was obtainable without it."
    ),
    "evaluation_complete": (
        "This lead ran under the private provider-evaluation strategy, which deliberately "
        "consults overlapping data providers for the same lead so their coverage, quality, "
        "and agreement can be measured against each other. Acquisition ended because every "
        "provider had been consulted (or visibly skipped for cache, budget, or cooldown "
        "reasons) — not because confidence was reached. Evaluation runs are never "
        "autonomous and their per-lead spend runs under a separate, explicit budget."
    ),
    PER_LEAD_BUDGET_EXHAUSTED: (
        "Enriching this lead any further would exceed its per-lead live spend cap, so no "
        "provider was called. The decision reflects only the evidence already held. A lead "
        "reaching this limit is unusual and worth investigating."
    ),
    DAILY_BUDGET_EXHAUSTED: (
        "The account-wide daily live spend cap is exhausted, so no provider was called for "
        "this lead. The decision reflects only the evidence already held. Acquisition "
        "resumes on the next UTC day, or when the cap is raised."
    ),
}


def stop_reason_explanation(reason_code: str) -> str:
    return _STOP_REASON_EXPLANATIONS.get(reason_code, f"Processing stopped: {reason_code}.")


@dataclass(frozen=True)
class ReceiptDecision:
    recommended_action: str
    """What the policy concluded — ``Decision`` enum value, frozen at decision time."""
    autonomous: bool
    """Whether ARIE *acted* without a human. Not "whether the model was confident
    enough to" — under the Live V1 autonomy guard those two come apart, and
    `autonomy_guard` below says when."""
    final_status: LeadStatus
    """The lead's live status — may reflect a human's override of `recommended_action`."""
    human_override: bool
    """True only once a human has responded with a `final_decision` that differs from
    `original_decision` — the same comparison `v_escalation_rate.human_overrode` makes."""
    autonomy_guard: str | None = None
    """Why autonomous action was withheld regardless of the recommendation, or
    `None` when no guard applied.

    Live V1 Foundation. Derived from `policy_name` rather than stored: every
    live-mode receipt was produced under the guard, so a column would record
    the same constant on every such row. A reviewer seeing an escalated lead
    with `recommended_action="auto_route"` needs this to distinguish "ARIE was
    unsure" from "ARIE was sure, and is not yet permitted to act on real-provider
    evidence" — see `arie.live.safety`."""


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
    suppressed_reason: str | None = None
    """``None`` for a real call and for an ordinary evidence-cache hit (an
    actual field value, reused). ``'recent_miss'``/``'recent_partial'``
    (migration 0011) when this zero-cost row instead means "a recent settled
    outcome already answered this, so nothing was asked" — the distinction
    ``arie.live.outcome_cache`` exists to keep truthful: a suppressed call
    reused a *fact that nothing new was found*, not a value."""


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
    review_id: UUID
    """Enough for a caller to act on the review — `GET /reviews/{review_id}` for its
    current `lead_version`, then `POST /reviews/{review_id}/decision` — without any
    other way to discover the id (there is no `GET /leads/{lead_id}/reviews`)."""
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

    shadow: bool
    """Post-M1 P5. True for a lead ingested with `mode="shadow"` — `decision`
    is ARIE's full recommendation, but `lead_status`/`decision.final_status`
    will never be an authoritative routing outcome (AUTO_ROUTED/AWAITING_HUMAN/
    MANUAL_REVIEW/SYNCED) and `human_review` will always be `None`: shadow
    evaluation never opens a real review. See `arie.jobs.handlers`' shadow
    branch for what is and isn't suppressed."""

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
    SELECT provider, status, cost_usd, latency_ms, cache_hit, suppressed_reason
    FROM provider_calls
    WHERE lead_id = %(lead_id)s
    ORDER BY requested_at
"""

_SELECT_HUMAN_REVIEW = """
    SELECT review_id, reviewer, original_decision, final_decision, responded_at
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
            suppressed_reason=row["suppressed_reason"],
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
            review_id=row["review_id"],
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
    human_review, human_override = _human_review(conn, lead_id)

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(_SELECT_DECISION_RECEIPT, {"lead_id": lead_id})
        receipt_row = cur.fetchone()

    # Which catalogue "not_called" is a set difference against depends on which
    # policy actually ran — the 8-provider simulated catalogue for a corpus
    # lead, or the one real provider for a live-mode lead. Getting this wrong
    # would claim the 7 simulated providers were "available but not called"
    # for a live lead that never had access to any of them, or vice versa.
    # Before a decision exists (no `receipt_row`) neither catalogue is known
    # yet, so this keeps the pre-P5 behaviour (ALL_PROVIDERS) rather than
    # guessing.
    catalogue = (
        LIVE_PROVIDER_NAMES
        if receipt_row is not None and receipt_row["policy_name"] in LIVE_POLICY_NAME_SET
        else ALL_PROVIDERS
    )
    not_called = tuple(name for name in catalogue if name not in called_names)

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
            shadow=lead.is_shadow,
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
        shadow=lead.is_shadow,
        decision=ReceiptDecision(
            recommended_action=receipt_row["decision"],
            autonomous=receipt_row["autonomous"],
            final_status=lead.status,
            human_override=human_override,
            autonomy_guard=(
                LIVE_GUARD_REASON if receipt_row["policy_name"] in LIVE_POLICY_NAME_SET else None
            ),
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
