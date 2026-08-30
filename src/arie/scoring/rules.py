"""ICP scoring rules — the single source of truth for what a good lead is.

This module is shared by two callers that must never diverge:

  * the **oracle**, which applies these rules to latent ground truth, and
  * the **runtime scorer**, which applies them to observed (partial, noisy)
    evidence.

Having one ruleset is what makes "decision agreement with the oracle" a
meaningful metric. If the oracle used different rules, the benchmark would be
measuring rule mismatch rather than the controller's acquisition behaviour.

A consequence worth stating plainly: given complete and correct facts, the
scorer reproduces the oracle by construction. That is intentional. What is under
test is *which facts the controller chooses to buy*, not the arithmetic.

``field_points`` is the primitive everything else is built from — the total
score, the completeness weighting, the conflict magnitude between disagreeing
sources, and the reachable score bounds over unknown fields. Deriving them all
from one function means a rule change propagates everywhere at once instead of
leaving four hand-maintained copies to drift apart.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

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

# The flag that nullifies rather than penalises. Kept separate throughout
# because it does not behave like the additive fields.
DISQUALIFIER_FIELD = "disqualifying_flag"

# --- unknown vs. known-negative ----------------------------------------------
#
# The sentinel a normalization layer emits when a real provider said something
# this ruleset has no mapping for (``arie.normalization.taxonomy``). It exists
# because "0 points" is ambiguous between two states that must never be
# conflated:
#
#   * **known negative evidence** — the provider said "construction", we
#     mapped it deliberately, and construction earns nothing under this ICP.
#     The field is *known*; the score is settled to that extent.
#   * **missing/unknown evidence** — the provider said "pet grooming
#     franchises", or nothing at all. We have no idea what this lead is. The
#     field is *unknown* and can still move the score in either direction.
#
# Both contribute 0.0 to the total, and that is correct. What must differ is
# every *bounds* and *completeness* consequence: an unknown field still raises
# the reachable upper bound and still counts against completeness, while a
# known-negative one does neither. ``arie.scoring.engine`` reads
# ``is_unknown`` rather than ``value is None`` for exactly this reason.
#
# The synthetic corpus never produces this sentinel — every generated value
# comes from the closed vocabularies in ``arie.evalgen.generator`` — so nothing
# below changes a single frozen benchmark number. It is the live path's
# vocabulary, understood defensively by the scorer.
UNKNOWN = "unknown"


def is_unknown(value: Any) -> bool:
    """True when ``value`` carries no usable evidence about its field.

    ``None`` (never observed) and ``UNKNOWN`` (observed but unmappable) are the
    same thing to every consumer of this module: absence of evidence. They are
    kept as distinct *values* only so an audit trail can tell "we never asked"
    apart from "we asked and could not use the answer".
    """
    return value is None or value == UNKNOWN


def is_known(facts: Mapping[str, Any], field_name: str) -> bool:
    """Whether ``facts`` holds usable evidence for ``field_name``."""
    return not is_unknown(facts.get(field_name))


# Fields the scorer knows how to consume. Anything else in a facts dict is
# ignored rather than silently contributing.
SCORED_FIELDS: tuple[str, ...] = (
    "employee_count",
    "industry",
    "title_seniority",
    "title_function",
    "buying_intent",
    "recent_trigger_event",
    DISQUALIFIER_FIELD,
)

# Field name -> the component label reported in a score breakdown.
FIELD_TO_COMPONENT: dict[str, str] = {
    "employee_count": "company_size",
    "industry": "industry",
    "title_seniority": "seniority",
    "title_function": "function",
    "buying_intent": "buying_intent",
    "recent_trigger_event": "trigger_event",
}

ADDITIVE_FIELDS: tuple[str, ...] = tuple(FIELD_TO_COMPONENT)

# Ceiling each field can contribute. Used to bound the reachable score when a
# field is still unknown, and to weight completeness by decision relevance.
MAX_FIELD_POINTS: dict[str, float] = {
    "employee_count": max(pts for _, _, pts in _SIZE_POINTS),
    "industry": max(_INDUSTRY_POINTS.values()),
    "title_seniority": max(_SENIORITY_POINTS.values()),
    "title_function": max(_FUNCTION_POINTS.values()),
    "buying_intent": _INTENT_MAX_POINTS,
    "recent_trigger_event": _TRIGGER_POINTS,
}

MAX_TOTAL_SCORE = sum(MAX_FIELD_POINTS.values())

# Completeness weights. The disqualifier contributes no positive points but can
# nullify the score outright, so weighting it at zero would let a lead read as
# fully enriched while the single fact most able to overturn its decision was
# still unknown. It is weighted at the level of the largest scoring component —
# a modelling choice, recorded here rather than buried at a call site.
DISQUALIFIER_COMPLETENESS_WEIGHT = 20.0
COMPLETENESS_WEIGHTS: dict[str, float] = {
    **MAX_FIELD_POINTS,
    DISQUALIFIER_FIELD: DISQUALIFIER_COMPLETENESS_WEIGHT,
}


# --- organization-configurable scoring (Productization M3) ------------------
#
# The scorer is not rewritten below — every function in this module and in
# `arie.scoring.engine` keeps its existing signature and algorithm. What
# becomes overridable is only *where the numbers above come from*: a
# dynamically-scoped `ScoringConfig`, read through a contextvar, defaulting to
# exactly the reference constants already defined above. Building
# `ScoringConfig()` and activating it is therefore behaviourally invisible —
# which is what lets every caller that has never heard of organization
# profiles (the oracle, `arie.evalgen`, the confidence fitting/calibration
# stack, every `arie.policy.*` class, `arie.icp.REFERENCE_ICP_V1`) keep
# calling `score_facts`/`decide`/`field_points`/`best_case_value` with zero
# arguments and get byte-identical results, including the frozen M0 benchmark.
#
# Field *names* stay fixed — SCORED_FIELDS above is not configurable. An
# organization profile (`arie.icp_profiles`) can only supply new *values* for
# the six field weights, the two decision thresholds, and the disqualifier
# on/off toggle; that is what keeps this "structured validated configuration"
# rather than a rule language.
@dataclass(frozen=True)
class ScoringConfig:
    qualify_threshold: float = QUALIFY_THRESHOLD
    reject_threshold: float = REJECT_THRESHOLD
    size_bands: tuple[tuple[int, int, float], ...] = _SIZE_POINTS
    industry_points: Mapping[str, float] = field(default_factory=lambda: dict(_INDUSTRY_POINTS))
    seniority_points: Mapping[str, float] = field(default_factory=lambda: dict(_SENIORITY_POINTS))
    function_points: Mapping[str, float] = field(default_factory=lambda: dict(_FUNCTION_POINTS))
    intent_max_points: float = _INTENT_MAX_POINTS
    trigger_points: float = _TRIGGER_POINTS
    disqualifier_enabled: bool = True
    profile_id: UUID | None = None
    """`organization_icp_profiles.profile_id`, carried through only so a
    caller that already has the active `ScoringConfig` in hand (`arie.jobs.
    handlers`) doesn't need a second lookup to stamp a Decision Receipt's
    provenance. Never read by any function in this module."""
    profile_version: int | None = None

    @property
    def max_field_points(self) -> dict[str, float]:
        """Config-aware per-field ceilings — the dynamic counterpart of the
        module-level `MAX_FIELD_POINTS`, which keeps reflecting the reference
        config only (callers that intentionally want the reference ceiling
        regardless of what is currently active, e.g. `arie.icp`, still import
        that constant directly)."""
        return {
            "employee_count": max((pts for _, _, pts in self.size_bands), default=0.0),
            "industry": max(self.industry_points.values(), default=0.0),
            "title_seniority": max(self.seniority_points.values(), default=0.0),
            "title_function": max(self.function_points.values(), default=0.0),
            "buying_intent": self.intent_max_points,
            "recent_trigger_event": self.trigger_points,
        }

    @property
    def completeness_weights(self) -> dict[str, float]:
        return {**self.max_field_points, DISQUALIFIER_FIELD: DISQUALIFIER_COMPLETENESS_WEIGHT}


_REFERENCE_CONFIG = ScoringConfig()
_ACTIVE_CONFIG: ContextVar[ScoringConfig] = ContextVar(
    "arie_active_scoring_config", default=_REFERENCE_CONFIG
)


def active_config() -> ScoringConfig:
    """The `ScoringConfig` in effect for the current call. The reference
    config unless `use_scoring_config` is currently active higher up the same
    synchronous call stack."""
    return _ACTIVE_CONFIG.get()


@contextmanager
def use_scoring_config(config: ScoringConfig) -> Iterator[None]:
    """Dynamically scope every `rules`/`engine` function to `config` for the
    duration of the `with` block.

    This is the seam that lets an organization's ICP profile drive the
    existing, unmodified scorer/policy call graph
    (`arie.policy.production.CalibratedBoundsPolicy.run` ->
    `arie.policy.evidence_view.score_results` -> `score_resolved` ->
    `score_facts`/`decide` -> ... -> `arie.scoring.engine`'s bounds/signals)
    without changing a single one of those functions' signatures or bodies.

    **Thread-pool caveat, checked and found safe.** `contextvars` do not
    propagate into a plain `concurrent.futures.ThreadPoolExecutor` worker
    thread. This codebase has exactly one such pool inside a single lead's
    processing (`arie.jobs.handlers`'s live `evaluation_parallel`
    provider-fetch pool), and it only ever submits `provider.fetch(...)` —
    never a scoring function — so no worker thread ever reads
    `active_config`. A future call site that submits scoring work to a
    thread/process pool must wrap its callable in
    `contextvars.copy_context().run(...)` or this override silently fails to
    apply inside it.
    """
    token = _ACTIVE_CONFIG.set(config)
    try:
        yield
    finally:
        _ACTIVE_CONFIG.reset(token)


def _size_points(employee_count: float) -> float:
    for lo, hi, pts in active_config().size_bands:
        if lo <= employee_count <= hi:
            return pts
    return 0.0


def field_points(field_name: str, value: Any) -> float:
    """Points a single field contributes at a given value.

    Returns 0.0 for an unknown field, a ``None`` value, or the ``UNKNOWN``
    sentinel. The disqualifier always returns 0.0 — it is not additive, and
    folding it in as a large negative weight would let a strong enough lead
    outscore a known blocker.

    Note that 0.0 here is *not* the caller's cue that a field is unknown: a
    deliberately-mapped, genuinely-poor-fit value ("nonprofit" is 2.0, an
    unlisted-but-recognised industry family is 0.0) scores low while remaining
    known. Ask :func:`is_known` for that distinction, never the point value.
    """
    if is_unknown(value):
        return 0.0

    config = active_config()
    if field_name == "employee_count":
        try:
            return _size_points(float(value))
        except (TypeError, ValueError):
            return 0.0
    if field_name == "industry":
        return config.industry_points.get(str(value), 0.0)
    if field_name == "title_seniority":
        return config.seniority_points.get(str(value), 0.0)
    if field_name == "title_function":
        return config.function_points.get(str(value), 0.0)
    if field_name == "buying_intent":
        try:
            return max(0.0, min(1.0, float(value))) * config.intent_max_points
        except (TypeError, ValueError):
            return 0.0
    if field_name == "recent_trigger_event":
        return config.trigger_points if value else 0.0

    return 0.0


def best_case_value(field_name: str) -> Any | None:
    """The categorical value that earns ``field_name``'s ceiling under the
    currently active :class:`ScoringConfig`, or ``None`` if the field has no
    single best categorical value (a continuous field like
    ``employee_count``/``buying_intent``/``recent_trigger_event``, the
    disqualifier, or an unrecognised name).

    Exists so a caller deciding whether a still-unknown field is worth buying
    can ask "if this resolved as favorably as possible, would the
    recommendation change?" via :func:`score_facts`/:func:`decide`, without
    needing to know this module's point tables itself — the "reuse the
    scoring machinery, don't duplicate it" rule applied to acquisition
    strategy, not just to the scorer's own callers.
    """
    config = active_config()
    points_by_field: dict[str, Mapping[str, float]] = {
        "industry": config.industry_points,
        "title_seniority": config.seniority_points,
        "title_function": config.function_points,
    }
    points = points_by_field.get(field_name)
    if not points:
        return None
    return max(points, key=points.get)  # type: ignore[arg-type]


def is_disqualified(facts: Mapping[str, Any]) -> bool:
    if not active_config().disqualifier_enabled:
        return False
    return facts.get(DISQUALIFIER_FIELD) is True


def score_facts(facts: dict[str, Any]) -> ScoreBreakdown:
    """Score a fact bundle.

    Missing facts contribute **zero**, not a neutral prior. This is deliberate:
    an unknown buying-intent signal should not be credited as if it were
    positive. It biases unenriched leads toward rejection, which is both the
    realistic business behaviour and the mechanism that creates genuine
    false-negative risk for a policy that stops enriching too early.
    """
    components = {
        component: field_points(field, facts.get(field))
        for field, component in FIELD_TO_COMPONENT.items()
    }

    # A disqualifying signal is absolute, not a penalty to be outweighed.
    if is_disqualified(facts):
        components = dict.fromkeys(components, 0.0)
        components["disqualified"] = 0.0
        return ScoreBreakdown(total_score=0.0, components=components, model_version=RULES_VERSION)

    return ScoreBreakdown(
        total_score=sum(components.values()),
        components=components,
        model_version=RULES_VERSION,
    )


def decide(total_score: float) -> Decision:
    """Map a score to an action.

    The middle band is not "we lack data" — it is "this lead is genuinely
    borderline". Distinguishing those two cases is the confidence model's job,
    not the scorer's.
    """
    config = active_config()
    if total_score >= config.qualify_threshold:
        return Decision.AUTO_ROUTE
    if total_score < config.reject_threshold:
        return Decision.REJECT
    return Decision.ESCALATE_HUMAN
