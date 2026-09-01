"""What the model is allowed to say about a customer's targeting, and nothing else.

:class:`BusinessProfileDraft` is the entire interface between a customer's free
text and ARIE's scoring configuration — the same role
``arie.llm.schema.ExtractedSignal`` plays for M1's signal extraction, and shaped
by the same principle. There is no field for a point value, no field for a
threshold, no field for a scoring dimension name, and ``extra="forbid"``
throughout. A model that wanted to set its own weights has nowhere to put the
number; a model that invented a seventh scoring dimension would fail validation
rather than silently widen the scorer.

**Canonical values are enumerated, not free text, wherever ARIE already has a
vocabulary.** Industries, seniorities and functions come from
``arie.normalization.taxonomy``, spelled out here as ``Literal`` types so they
reach the model as a real JSON-Schema ``enum`` rather than as a sentence asking
it politely. ``tests/unit/test_intelligence_schemas.py`` pins each list against
the taxonomy's own frozensets, so a value added there and forgotten here fails a
test instead of quietly becoming unreachable.

Free-form strings survive only where ARIE genuinely has no vocabulary —
"multi-location gym", "operates its own warehouse". Those are bounded in count
and length and are advisory: they inform what a human reviewer sees and what
later slices may research, and they contribute nothing to a score. That is the
honest position, and it is stated rather than implied — a customer reading
"positive indicators" should not believe an unobservable one is being scored.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "CANONICAL_FUNCTION_VALUES",
    "CANONICAL_INDUSTRY_VALUES",
    "CANONICAL_SENIORITY_VALUES",
    "EMPLOYEE_BANDS",
    "SCORING_DIMENSIONS",
    "BandPreference",
    "BusinessProfileDraft",
    "CanonicalFunction",
    "CanonicalIndustry",
    "CanonicalSeniority",
    "EmployeeBand",
    "PreferenceLevel",
    "ScoringDimension",
    "TargetingObjective",
]


class TargetingObjective(StrEnum):
    """What the customer is optimising for.

    Five values, not fifteen. Each one has to make a *different* deterministic
    difference (to generated preferences and, later, to research appetite) or it
    is a label pretending to be a setting. ``CUSTOM`` exists so a customer whose
    goal is none of the four is not forced to misdescribe it, and it behaves
    exactly like ``BEST_PROSPECTS`` — stated here rather than left for someone to
    discover.

    An objective never overrides an explicit customer constraint. It breaks ties
    and sets thresholds; a customer who said "avoid solo traders" gets that
    honoured whichever objective they picked.
    """

    BEST_PROSPECTS = "best_prospects"
    MAXIMIZE_BUY_LIKELIHOOD = "maximize_buy_likelihood"
    HIGH_VALUE = "high_value"
    MINIMIZE_WASTED_OUTREACH = "minimize_wasted_outreach"
    CUSTOM = "custom"


class PreferenceLevel(StrEnum):
    """How much one scoring dimension matters, relative to the others.

    Ordinal, closed, and deliberately coarse. The model is being asked a
    question it can actually answer from a business description ("does seniority
    matter more than company size here?"), not one it cannot ("how many of the
    hundred points should seniority get?"). The conversion to points is
    ``arie.intelligence.normalization``'s, and it is arithmetic.
    """

    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class BandPreference(StrEnum):
    """How a customer feels about one company-size band."""

    PREFERRED = "preferred"
    ACCEPTABLE = "acceptable"
    AVOID = "avoid"


class EmployeeBand(StrEnum):
    """The fixed company-size lattice, matching the reference ICP's bands exactly.

    Fixed rather than model-chosen because the bands are a *scoring* structure,
    not an interpretation: ``arie.icp_profiles.validate_config`` needs contiguous
    non-overlapping ranges with sane bounds, and a model inventing "20-35
    employees" would produce a config that is legal but arbitrary, differently
    arbitrary on every generation. The model says which of these five it wants;
    ARIE says what they are worth.
    """

    MICRO = "employees_1_10"
    SMALL = "employees_11_50"
    MID = "employees_51_200"
    LARGE = "employees_201_1000"
    ENTERPRISE = "employees_1001_plus"


EMPLOYEE_BANDS: dict[EmployeeBand, tuple[int, int]] = {
    EmployeeBand.MICRO: (1, 10),
    EmployeeBand.SMALL: (11, 50),
    EmployeeBand.MID: (51, 200),
    EmployeeBand.LARGE: (201, 1000),
    EmployeeBand.ENTERPRISE: (1001, 1_000_000_000),
}
"""Transcribed from ``arie.icp_profiles.REFERENCE_CONFIG``'s
``employee_count_bands``. Pinned against it by a unit test so the two cannot
drift — a generated profile and the reference profile should disagree about
what a band is *worth*, never about where it starts."""


class ScoringDimension(StrEnum):
    """The six additive scoring dimensions, named exactly as
    ``arie.scoring.rules.SCORED_FIELDS`` names them.

    Not extensible from here. M3's configuration contract is that an
    organization profile supplies new *values* for these six, never new fields —
    ``arie.scoring.rules``' own comment calls that out as what keeps the config
    "structured validated configuration rather than a rule language". A seventh
    dimension would be a scorer change, and M7 does not rewrite the scorer.
    """

    EMPLOYEE_COUNT = "employee_count"
    INDUSTRY = "industry"
    TITLE_SENIORITY = "title_seniority"
    TITLE_FUNCTION = "title_function"
    BUYING_INTENT = "buying_intent"
    RECENT_TRIGGER_EVENT = "recent_trigger_event"


SCORING_DIMENSIONS: tuple[ScoringDimension, ...] = tuple(ScoringDimension)
"""Declaration order, which is also the deterministic tie-break order the
normaliser uses when distributing a remainder. Fixing it here rather than
sorting alphabetically at the point of use keeps "which dimension wins a tie"
one visible decision instead of an emergent property of a sort key."""


# Spelled out rather than derived from `taxonomy.CANONICAL_*` at import time for
# two reasons. A `frozenset` has no order, so a derived `Literal` would produce a
# JSON Schema whose `enum` ordering changed between runs — and the schema is
# prompt content, so that would silently break prompt caching and make a fake
# provider's token counts irreproducible. And `unknown` must not appear: it is
# the absence of a value, and offering it to a model as a *preference* invites
# "prefers companies whose industry is unknown", which is not a targeting
# statement. `tests/unit/test_intelligence_schemas.py` asserts each tuple equals
# its taxonomy frozenset minus `unknown`.
CanonicalIndustry = Literal[
    "agriculture",
    "construction",
    "ecommerce",
    "education",
    "energy",
    "financial_services",
    "fintech",
    "government",
    "healthcare",
    "healthtech",
    "hospitality",
    "logistics",
    "manufacturing",
    "media",
    "nonprofit",
    "other",
    "professional_services",
    "real_estate",
    "retail",
    "software",
    "telecom",
]

CanonicalSeniority = Literal["c_level", "vp", "director", "manager", "ic"]

CanonicalFunction = Literal[
    "data", "engineering", "operations", "marketing", "sales", "finance", "other"
]

CANONICAL_INDUSTRY_VALUES: tuple[str, ...] = (
    "agriculture",
    "construction",
    "ecommerce",
    "education",
    "energy",
    "financial_services",
    "fintech",
    "government",
    "healthcare",
    "healthtech",
    "hospitality",
    "logistics",
    "manufacturing",
    "media",
    "nonprofit",
    "other",
    "professional_services",
    "real_estate",
    "retail",
    "software",
    "telecom",
)
CANONICAL_SENIORITY_VALUES: tuple[str, ...] = ("c_level", "vp", "director", "manager", "ic")
CANONICAL_FUNCTION_VALUES: tuple[str, ...] = (
    "data",
    "engineering",
    "operations",
    "marketing",
    "sales",
    "finance",
    "other",
)


class BusinessProfileDraft(BaseModel):
    """ARIE's structured reading of "what do you sell" and "who do you want".

    Every list is length-capped and every string is character-capped, so a
    malfunctioning or manipulated model cannot turn one generation into an
    unbounded document. The caps are generous enough for a real business and
    tight enough that hitting one is a signal rather than an inconvenience.

    Nothing here is authoritative about scoring. This is an *interpretation*,
    shown to a human, editable by that human, and converted to a scoring
    configuration by ``arie.intelligence.normalization`` — which re-derives every
    number from these fields and takes none of them on trust.
    """

    model_config = ConfigDict(extra="forbid")

    offering_summary: str = Field(
        max_length=300,
        description="One sentence naming what the customer sells, in their own "
        "terms. Never a claim about their company that their text did not state.",
    )
    plain_english_summary: str = Field(
        max_length=800,
        description="Two or three sentences a non-technical business owner would "
        "recognise as a fair summary of who they said they want to reach.",
    )

    ideal_company_types: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Kinds of business worth contacting, in the customer's own "
        "language (e.g. 'multi-location gym', 'supplement distributor').",
    )
    preferred_industries: list[CanonicalIndustry] = Field(
        default_factory=list,
        max_length=8,
        description="Canonical industries that fit best. Choose only from the "
        "listed values; if none fits, leave this empty rather than guessing.",
    )
    acceptable_industries: list[CanonicalIndustry] = Field(
        default_factory=list,
        max_length=8,
        description="Canonical industries worth contacting but not ideal.",
    )

    employee_band_preferences: dict[EmployeeBand, BandPreference] = Field(
        default_factory=dict,
        description="How the customer feels about each company-size band. Omit a "
        "band the customer said nothing about rather than guessing.",
    )

    preferred_seniorities: list[CanonicalSeniority] = Field(
        default_factory=list,
        max_length=5,
        description="Seniority levels of the people worth reaching. Owners and "
        "founders are c_level.",
    )
    acceptable_seniorities: list[CanonicalSeniority] = Field(default_factory=list, max_length=5)
    preferred_functions: list[CanonicalFunction] = Field(
        default_factory=list,
        max_length=7,
        description="Job functions worth reaching. Purchasing, procurement and "
        "buying sit under operations.",
    )
    acceptable_functions: list[CanonicalFunction] = Field(default_factory=list, max_length=7)
    preferred_titles: list[str] = Field(
        default_factory=list,
        max_length=10,
        description="Actual job titles the customer named or clearly implied. "
        "Advisory: titles are matched through seniority and function, not "
        "scored literally.",
    )

    preferred_geographies: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Places the customer said they sell into. Advisory only — "
        "ARIE stores geography but does not score it.",
    )
    preferred_company_characteristics: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="Observable business traits that make a company a better "
        "fit (e.g. 'operates more than one location').",
    )

    positive_indicators: list[str] = Field(default_factory=list, max_length=8)
    negative_indicators: list[str] = Field(default_factory=list, max_length=8)
    hard_disqualifiers: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Things the customer said make a company not worth "
        "contacting at all. Only include what they actually ruled out.",
    )
    research_worthy_unknowns: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="Facts that would most change a decision if they were known. "
        "Recorded for a later milestone; nothing acts on them yet.",
    )

    relative_preferences: dict[ScoringDimension, PreferenceLevel] = Field(
        default_factory=dict,
        description="How much each scoring dimension matters, relative to the "
        "others. Not point values — ARIE decides those.",
    )


def _capped(values: list[str], *, limit: int, length: int) -> list[str]:
    """Trim and cap a free-text list. Used by the canonicaliser, not by Pydantic.

    Pydantic rejects an over-long list; this exists for the *editing* path,
    where a human's pasted text is worth trimming rather than refusing.
    """
    cleaned = [v.strip()[:length] for v in values if v and v.strip()]
    seen: dict[str, None] = {}
    for value in cleaned:
        seen.setdefault(value, None)
    return list(seen)[:limit]
