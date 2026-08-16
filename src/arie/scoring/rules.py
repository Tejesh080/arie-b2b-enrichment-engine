"""ICP scoring rules — the single source of truth for what a good lead is.

This module is shared by two callers that must never diverge:

  * the **oracle**, which applies these rules to latent ground truth, and
  * the **production scorer**, which applies them to observed (partial, noisy)
    evidence.

Having one ruleset is what makes "decision agreement with the oracle" a
meaningful metric. If the oracle used different rules, the benchmark would be
measuring rule mismatch rather than the controller's acquisition behaviour.

A consequence worth stating plainly: given complete and correct facts, the
scorer reproduces the oracle by construction. That is intentional. What is under
test is *which facts the controller chooses to buy*, not the arithmetic.
"""

from __future__ import annotations

from typing import Any

from arie.core.types import Decision, ScoreBreakdown

RULES_VERSION = "icp-1.0.0"

# Decision boundaries. The gap between them is the band where a lead is
# genuinely borderline *even with full information* — this is what gives the
# human-escalation rate an honest floor rather than one engineered to zero.
QUALIFY_THRESHOLD = 65.0
REJECT_THRESHOLD = 55.0

# --- component weights -------------------------------------------------------

_SIZE_POINTS: tuple[tuple[int, int, float], ...] = (
    (1, 10, 2.0),
    (11, 50, 10.0),
    (51, 200, 20.0),
    (201, 1000, 18.0),
    (1001, 10**9, 8.0),
)

_INDUSTRY_POINTS: dict[str, float] = {
    "software": 15.0,
    "fintech": 15.0,
    "healthtech": 13.0,
    "ecommerce": 12.0,
    "logistics": 8.0,
    "manufacturing": 7.0,
    "education": 5.0,
    "nonprofit": 2.0,
}

_SENIORITY_POINTS: dict[str, float] = {
    "c_level": 20.0,
    "vp": 18.0,
    "director": 14.0,
    "manager": 8.0,
    "ic": 2.0,
}

_FUNCTION_POINTS: dict[str, float] = {
    "data": 15.0,
    "engineering": 14.0,
    "operations": 9.0,
    "marketing": 5.0,
    "sales": 5.0,
    "finance": 4.0,
    "other": 2.0,
}

_INTENT_MAX_POINTS = 20.0
_TRIGGER_POINTS = 10.0

# Fields the scorer knows how to consume. Anything else in a facts dict is
# ignored rather than silently contributing.
SCORED_FIELDS: tuple[str, ...] = (
    "employee_count",
    "industry",
    "title_seniority",
    "title_function",
    "buying_intent",
    "recent_trigger_event",
    "disqualifying_flag",
)


def _size_points(employee_count: int) -> float:
    for lo, hi, pts in _SIZE_POINTS:
        if lo <= employee_count <= hi:
            return pts
    return 0.0


def score_facts(facts: dict[str, Any]) -> ScoreBreakdown:
    """Score a fact bundle.

    Missing facts contribute **zero**, not a neutral prior. This is deliberate:
    an unknown buying-intent signal should not be credited as if it were
    positive. It biases unenriched leads toward rejection, which is both the
    realistic business behaviour and the mechanism that creates genuine
    false-negative risk for a policy that stops enriching too early.
    """
    components: dict[str, float] = {}

    employee_count = facts.get("employee_count")
    components["company_size"] = (
        _size_points(int(employee_count)) if employee_count is not None else 0.0
    )

    industry = facts.get("industry")
    components["industry"] = _INDUSTRY_POINTS.get(str(industry), 0.0) if industry else 0.0

    seniority = facts.get("title_seniority")
    components["seniority"] = _SENIORITY_POINTS.get(str(seniority), 0.0) if seniority else 0.0

    function = facts.get("title_function")
    components["function"] = _FUNCTION_POINTS.get(str(function), 0.0) if function else 0.0

    intent = facts.get("buying_intent")
    components["buying_intent"] = (
        max(0.0, min(1.0, float(intent))) * _INTENT_MAX_POINTS if intent is not None else 0.0
    )

    trigger = facts.get("recent_trigger_event")
    components["trigger_event"] = _TRIGGER_POINTS if trigger else 0.0

    # A disqualifying signal is not a penalty to be outweighed — it is absolute.
    # Modelling it as a large negative weight would let a strong enough lead
    # score past a known blocker, which is wrong.
    if facts.get("disqualifying_flag") is True:
        components = dict.fromkeys(components, 0.0)
        components["disqualified"] = 0.0
        return ScoreBreakdown(total_score=0.0, components=components, model_version=RULES_VERSION)

    total = sum(components.values())
    return ScoreBreakdown(total_score=total, components=components, model_version=RULES_VERSION)


def decide(total_score: float) -> Decision:
    """Map a score to an action.

    The middle band is not "we lack data" — it is "this lead is genuinely
    borderline". Distinguishing those two cases is the confidence model's job,
    not the scorer's.
    """
    if total_score >= QUALIFY_THRESHOLD:
        return Decision.AUTO_ROUTE
    if total_score < REJECT_THRESHOLD:
        return Decision.REJECT
    return Decision.ESCALATE_HUMAN
