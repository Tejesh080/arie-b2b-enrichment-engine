"""Relative business preferences in, a legal scoring configuration out.

This is the module that makes it safe to let a language model near ARIE's
targeting. Everything here is a pure function of a
:class:`~arie.intelligence.schemas.BusinessProfileDraft` and a
:class:`~arie.intelligence.schemas.TargetingObjective`. No model output reaches
past it: a draft says *seniority matters more than company size*, and this
module — not the model — decides that seniority is therefore worth 26 points
and company size 15.

**The exactly-100.0 constraint is arithmetic, not hope.**
``arie.icp_profiles.validate_config`` requires the six field ceilings to sum to
exactly ``WEIGHT_SUM_TARGET``. :func:`allocate_ceilings` guarantees it by
construction: it allocates *integers* that sum to 100 using the largest-
remainder method, and integers are exactly representable as floats, so the
resulting total is 100.0 with no drift to tolerate. There is no rounding pass
afterwards that could reintroduce error, and no path by which a draft produces
a config that ``validate_config`` rejects — ``tests/unit/test_intelligence
_normalization.py`` asserts that over the whole preference space.

**Ties break in a fixed, documented order.** When two dimensions have equal
remainders and only one spare point, it goes to whichever appears first in
``SCORING_DIMENSIONS`` — declaration order, which is ``SCORED_FIELDS`` order.
Arbitrary, but *stably* arbitrary: the same draft produces the same
configuration on every machine and every run, which is what makes a confirmed
profile reproducible and a generated preview honest about what confirming it
will do.

**Thresholds come from the objective, never from the model.** A model asked for
a qualify threshold would supply a plausible number with nothing behind it, and
that number would silently decide which leads a customer contacts. The four
objectives map to four fixed pairs, anchored on the reference ICP's own 65/55.
"""

from __future__ import annotations

from typing import Any

from arie.icp_profiles import WEIGHT_SUM_TARGET
from arie.intelligence.schemas import (
    EMPLOYEE_BANDS,
    SCORING_DIMENSIONS,
    BandPreference,
    BusinessProfileDraft,
    EmployeeBand,
    PreferenceLevel,
    ScoringDimension,
    TargetingObjective,
)

__all__ = [
    "ACCEPTABLE_FRACTION",
    "DEFAULT_PREFERENCE",
    "OBJECTIVE_THRESHOLDS",
    "PREFERENCE_WEIGHTS",
    "allocate_ceilings",
    "build_scoring_config",
    "describe_allocation",
]

_TOTAL = int(WEIGHT_SUM_TARGET)
"""100. Integer on purpose — the whole allocation runs in whole points so the
final sum is exactly 100.0 rather than 100.00000000000001. A non-integral
``WEIGHT_SUM_TARGET`` would break that assumption loudly at import, which is
better than breaking it quietly at validation."""

PREFERENCE_WEIGHTS: dict[PreferenceLevel, int] = {
    PreferenceLevel.NONE: 0,
    PreferenceLevel.LOW: 1,
    PreferenceLevel.MEDIUM: 2,
    PreferenceLevel.HIGH: 4,
    PreferenceLevel.CRITICAL: 7,
}
"""Superlinear, so the levels mean something. On a linear 1-2-3-4 scale,
"critical" for one dimension and "low" for another differ by 4x; here they
differ by 7x, which is closer to what a business owner means when they say one
thing matters and another barely does. The exact numbers are a modelling
choice, recorded here rather than buried in the allocator."""

DEFAULT_PREFERENCE = PreferenceLevel.MEDIUM
"""What a dimension the draft said nothing about is worth. Medium rather than
none: an omission is "the customer did not mention it", not "the customer does
not care", and zeroing a dimension on silence would make a lead's seniority
irrelevant because nobody thought to bring it up."""

ACCEPTABLE_FRACTION = 0.5
"""An "acceptable" category scores half of a "preferred" one, rounded down.
Half rather than a tuned number because there is nothing to tune it against —
no outcome data exists yet — and a made-up 0.62 would imply a precision this
has no basis for. Slice 7's feedback loop is where a figure like this earns a
different value."""

OBJECTIVE_THRESHOLDS: dict[TargetingObjective, tuple[float, float]] = {
    TargetingObjective.BEST_PROSPECTS: (65.0, 55.0),
    TargetingObjective.MAXIMIZE_BUY_LIKELIHOOD: (72.0, 60.0),
    TargetingObjective.HIGH_VALUE: (70.0, 58.0),
    TargetingObjective.MINIMIZE_WASTED_OUTREACH: (75.0, 62.0),
    TargetingObjective.CUSTOM: (65.0, 55.0),
}
"""(qualify, reject) per objective. ``BEST_PROSPECTS`` and ``CUSTOM`` are the
reference ICP's own 65/55 — a customer who picks the default objective gets the
decision boundary ARIE has always used, so the generated profile differs from
the reference one only in *what it values*, not in how hard it is to pass.
The other three raise the bar, most for the objective that is explicitly about
not wasting outreach."""

_FALLBACK_INDUSTRIES: tuple[str, ...] = ("software", "professional_services", "other")
_FALLBACK_SENIORITIES: tuple[str, ...] = ("c_level", "vp", "director")
_FALLBACK_FUNCTIONS: tuple[str, ...] = ("operations", "sales")
_FALLBACK_BANDS: tuple[EmployeeBand, ...] = (EmployeeBand.SMALL, EmployeeBand.MID)
"""Used only when a draft names no preferred value at all for a dimension that
has been allocated points. Points with nothing to attach them to would make the
dimension's ceiling unreachable — every lead would score zero there — and an
unreachable ceiling breaks the 0-100 scale the thresholds are calibrated on.
A broad, non-committal default is the honest recovery; the generated profile is
shown to a human before it governs anything."""


def allocate_ceilings(
    preferences: dict[ScoringDimension, PreferenceLevel],
) -> dict[ScoringDimension, int]:
    """Split exactly 100 whole points across the six dimensions.

    Largest-remainder (Hare quota): each dimension takes its exact share's
    floor, and the points left over go one each to the dimensions with the
    largest fractional remainders, ties broken by ``SCORING_DIMENSIONS`` order.
    That method is chosen over rounding-then-fixing because it cannot overshoot
    and cannot need a correction pass — the two ways an allocator quietly stops
    summing to its target.

    Relative ordering is preserved: a dimension weighted above another never
    receives fewer points than it. All-zero preferences fall back to treating
    every dimension as :data:`DEFAULT_PREFERENCE`, because a configuration where
    nothing can score is not a targeting profile, it is a broken one.
    """
    weights = {
        dimension: PREFERENCE_WEIGHTS[preferences.get(dimension, DEFAULT_PREFERENCE)]
        for dimension in SCORING_DIMENSIONS
    }
    total_weight = sum(weights.values())
    if total_weight == 0:
        weights = {
            dimension: PREFERENCE_WEIGHTS[DEFAULT_PREFERENCE] for dimension in SCORING_DIMENSIONS
        }
        total_weight = sum(weights.values())

    base = {d: (_TOTAL * w) // total_weight for d, w in weights.items()}
    remainder = {d: (_TOTAL * w) % total_weight for d, w in weights.items()}
    leftover = _TOTAL - sum(base.values())

    # Sorted by (-remainder, declaration index): biggest fractional share first,
    # ties by the fixed dimension order. `SCORING_DIMENSIONS.index` rather than
    # the enum's value so the tie-break never depends on alphabetical accident.
    order = sorted(
        SCORING_DIMENSIONS,
        key=lambda d: (-remainder[d], SCORING_DIMENSIONS.index(d)),
    )
    for dimension in order[:leftover]:
        base[dimension] += 1

    return base


def _category_points(
    preferred: list[str], acceptable: list[str], *, ceiling: int, fallback: tuple[str, ...]
) -> dict[str, float]:
    """Points per category for one map-shaped dimension.

    The map's maximum must equal `ceiling` exactly — that is what
    ``validate_config`` sums — so at least one category has to sit at the top.
    A preferred category gets the ceiling; an acceptable one gets
    :data:`ACCEPTABLE_FRACTION` of it, floored; a category named as both is
    preferred (the stronger statement wins).

    A ceiling of zero still emits the map with zero-valued entries rather than
    an empty dict. Both validate, but an empty map reads as "this was left out"
    when what happened is "this was deliberately weighted to nothing", and a
    human reviewing the generated profile should be able to tell those apart.
    """
    top = [c for c in preferred if c]
    if not top:
        top = [c for c in acceptable if c] or list(fallback)
    second = [c for c in acceptable if c and c not in top]

    points: dict[str, float] = {category: float(ceiling) for category in top}
    for category in second:
        points[category] = float(int(ceiling * ACCEPTABLE_FRACTION))
    return points


def _employee_bands(
    preferences: dict[EmployeeBand, BandPreference], *, ceiling: int
) -> list[dict[str, float]]:
    """The five-band ladder, valued by preference.

    Every band is always emitted, including avoided ones at zero. Dropping a
    band would leave a hole in the ladder, and a company whose size fell in the
    hole would score zero for size — indistinguishable, in a receipt, from a
    company whose size is unknown. Explicit zeros keep "we know, and it is worth
    nothing" separable from "we do not know".
    """
    resolved = {band: preferences.get(band, BandPreference.ACCEPTABLE) for band in EMPLOYEE_BANDS}
    if not any(p is BandPreference.PREFERRED for p in resolved.values()):
        # Nothing at the ceiling would make the size dimension unreachable.
        for band in _FALLBACK_BANDS:
            if resolved[band] is not BandPreference.AVOID:
                resolved[band] = BandPreference.PREFERRED
    if not any(p is BandPreference.PREFERRED for p in resolved.values()):
        # Every fallback band was explicitly avoided: promote the least-bad
        # remaining band rather than emitting an unreachable dimension.
        for band in EMPLOYEE_BANDS:
            if resolved[band] is BandPreference.ACCEPTABLE:
                resolved[band] = BandPreference.PREFERRED
                break
        else:
            resolved[EmployeeBand.MID] = BandPreference.PREFERRED

    value = {
        BandPreference.PREFERRED: float(ceiling),
        BandPreference.ACCEPTABLE: float(int(ceiling * ACCEPTABLE_FRACTION)),
        BandPreference.AVOID: 0.0,
    }
    return [
        {
            "min_employees": EMPLOYEE_BANDS[band][0],
            "max_employees": EMPLOYEE_BANDS[band][1],
            "points": value[resolved[band]],
        }
        for band in EMPLOYEE_BANDS
    ]


def build_scoring_config(
    draft: BusinessProfileDraft, *, objective: TargetingObjective
) -> dict[str, Any]:
    """The complete ``organization_icp_profiles.config`` a draft implies.

    Deterministic and total: every draft that passed Pydantic validation
    produces a config that passes ``arie.icp_profiles.validate_config``. Nothing
    in the returned document came from the model as a number — the model chose
    categories and relative importance, and every point value here was computed
    from those choices by the functions above.

    ``target_geographies`` is carried through because the customer said it and
    it belongs in the record, and it is *not* scored — geography is not one of
    ``arie.scoring.rules.SCORED_FIELDS``, exactly as ``arie.icp_profiles``'
    docstring describes. A surface showing it must label it advisory.

    ``disqualifier_enabled`` is left on. The draft's ``hard_disqualifiers`` are
    free text and are not compiled into rules by this slice; the toggle governs
    ARIE's existing ``disqualifying_flag`` evidence field, which is unrelated
    and should keep behaving as it always has.
    """
    ceilings = allocate_ceilings(draft.relative_preferences)
    qualify, reject = OBJECTIVE_THRESHOLDS[objective]

    return {
        "qualify_threshold": qualify,
        "reject_threshold": reject,
        "employee_count_bands": _employee_bands(
            draft.employee_band_preferences, ceiling=ceilings[ScoringDimension.EMPLOYEE_COUNT]
        ),
        "industry_points": _category_points(
            list(draft.preferred_industries),
            list(draft.acceptable_industries),
            ceiling=ceilings[ScoringDimension.INDUSTRY],
            fallback=_FALLBACK_INDUSTRIES,
        ),
        "seniority_points": _category_points(
            list(draft.preferred_seniorities),
            list(draft.acceptable_seniorities),
            ceiling=ceilings[ScoringDimension.TITLE_SENIORITY],
            fallback=_FALLBACK_SENIORITIES,
        ),
        "function_points": _category_points(
            list(draft.preferred_functions),
            list(draft.acceptable_functions),
            ceiling=ceilings[ScoringDimension.TITLE_FUNCTION],
            fallback=_FALLBACK_FUNCTIONS,
        ),
        "buying_intent_weight": float(ceilings[ScoringDimension.BUYING_INTENT]),
        "trigger_event_weight": float(ceilings[ScoringDimension.RECENT_TRIGGER_EVENT]),
        "target_geographies": list(draft.preferred_geographies),
        "disqualifier_enabled": True,
    }


_DIMENSION_LABELS: dict[ScoringDimension, str] = {
    ScoringDimension.EMPLOYEE_COUNT: "Company size",
    ScoringDimension.INDUSTRY: "Industry",
    ScoringDimension.TITLE_SENIORITY: "Contact seniority",
    ScoringDimension.TITLE_FUNCTION: "Contact's role",
    ScoringDimension.BUYING_INTENT: "Signs of buying intent",
    ScoringDimension.RECENT_TRIGGER_EVENT: "Recent trigger event",
}


def describe_allocation(config: dict[str, Any]) -> list[dict[str, Any]]:
    """A human-readable reading of what a generated config weights.

    Exists so the console can show "Contact seniority — 26 of 100 points, the
    most important thing in your profile" without a frontend re-deriving
    ceilings from point maps and getting it subtly wrong. Derived from the
    config rather than from the draft, so it describes what will actually score
    rather than what was asked for.
    """
    ceilings: dict[ScoringDimension, float] = {
        ScoringDimension.EMPLOYEE_COUNT: max(
            (band["points"] for band in config["employee_count_bands"]), default=0.0
        ),
        ScoringDimension.INDUSTRY: max(config["industry_points"].values(), default=0.0),
        ScoringDimension.TITLE_SENIORITY: max(config["seniority_points"].values(), default=0.0),
        ScoringDimension.TITLE_FUNCTION: max(config["function_points"].values(), default=0.0),
        ScoringDimension.BUYING_INTENT: config["buying_intent_weight"],
        ScoringDimension.RECENT_TRIGGER_EVENT: config["trigger_event_weight"],
    }
    ranked = sorted(
        SCORING_DIMENSIONS,
        key=lambda d: (-ceilings[d], SCORING_DIMENSIONS.index(d)),
    )
    return [
        {
            "dimension": str(dimension),
            "label": _DIMENSION_LABELS[dimension],
            "points": ceilings[dimension],
            "rank": index + 1,
        }
        for index, dimension in enumerate(ranked)
    ]
