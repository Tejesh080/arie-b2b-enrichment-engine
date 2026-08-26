"""The Reference ICP for Live V1 — one named, versioned profile, in one place.

**What this is.** A declarative statement of the customer profile the live path
is currently pointed at: B2B software/SaaS, 50-1,000 employees, US/UK/AU/CA,
sold to revenue-side leaders. It is named ``REFERENCE_ICP_V1`` rather than
``ICP`` because it is *a* reference profile chosen to make the live path
concrete — not a claim about every business, and not a fitted result. Swapping
it is a config edit here, not a hunt through provider adapters.

**What this is NOT: a second scorer.** ``arie.scoring.rules`` remains the only
thing that turns facts into a number, and this module never competes with it.
:func:`assess` returns a *descriptive* fit report — which stated criteria are
met, missed, or unobservable — for a human reviewing a live lead. Nothing in
the decision path consumes it, and nothing may: a second scoring surface that
disagrees with the first is worse than no second surface.

The two are related, and the relationship is worth stating plainly: this
profile is the *specification* that ``arie.scoring.rules``' weights approximate.
Where they disagree, the weights win at decision time and the disagreement is a
recalibration finding — exactly the sort of thing the live-mode guard
(``arie.live.safety``) exists to keep from acting autonomously on.

**Two deliberate representation choices, both lossy in a stated way.**

*Functions.* The brief's ``revenue_operations`` and ``growth`` have no weight
in ``arie.scoring.rules._FUNCTION_POINTS``. Introducing them as canonical
values would make ARIE's own highest-intent functions score 0.0 — a
known-negative reading of its best leads, the precise bug this whole layer
exists to prevent. They fold into ``operations`` and ``marketing``
(``arie.normalization.taxonomy``'s alias tables do the folding), and
:data:`REFERENCE_ICP_V1` records both the intent and the fold.

*Seniority.* The brief's ``head`` and ``executive`` fold onto the scorer's
ladder as ``director`` and ``c_level`` respectively. See the taxonomy module
for why ``head`` maps down rather than up.

**Geography and disqualifiers are declared even where unobservable.** No
provider ARIE currently calls returns a country, and none returns a reliable
"is this person a student/freelancer" flag. Those criteria stay in the profile
and :func:`assess` reports them as ``UNOBSERVABLE`` rather than quietly
dropping them or — far worse — inventing a ``disqualifying_flag`` from a guess.
An unobservable criterion is a known gap; a fabricated one is a wrong answer.
The one disqualifier that *is* genuinely observable, a free/personal email
domain, is derived from ``arie.identity.normalize.FREE_EMAIL_DOMAINS``, which
the ingestion path already computes for its own reasons.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from arie.identity.normalize import FREE_EMAIL_DOMAINS
from arie.normalization.taxonomy import (
    CANONICAL_FUNCTIONS,
    CANONICAL_INDUSTRIES,
    CANONICAL_SENIORITIES,
)
from arie.scoring.rules import is_unknown

__all__ = [
    "REFERENCE_ICP_V1",
    "CriterionVerdict",
    "ICPAssessment",
    "ICPCriterion",
    "ReferenceICP",
    "assess",
    "free_email_disqualifier",
]


class CriterionVerdict(StrEnum):
    """How one ICP criterion reads against a lead's known facts."""

    MET = "met"
    MISSED = "missed"
    """Known evidence, and it falls outside the profile. A real negative."""
    UNKNOWN = "unknown"
    """The field is observable in principle but was never learned for this lead."""
    UNOBSERVABLE = "unobservable"
    """No provider in the current live configuration can supply this at all.
    Structurally different from UNKNOWN: more enrichment would not help."""


@dataclass(frozen=True)
class ICPCriterion:
    name: str
    verdict: CriterionVerdict
    detail: str


@dataclass(frozen=True)
class ICPAssessment:
    """A descriptive read of one lead against the reference profile."""

    icp_name: str
    icp_version: str
    criteria: tuple[ICPCriterion, ...]

    def _count(self, verdict: CriterionVerdict) -> int:
        return sum(1 for item in self.criteria if item.verdict is verdict)

    @property
    def met(self) -> int:
        return self._count(CriterionVerdict.MET)

    @property
    def missed(self) -> int:
        return self._count(CriterionVerdict.MISSED)

    @property
    def unknown(self) -> int:
        return self._count(CriterionVerdict.UNKNOWN)

    @property
    def unobservable(self) -> int:
        return self._count(CriterionVerdict.UNOBSERVABLE)


@dataclass(frozen=True)
class ReferenceICP:
    """One named, versioned customer profile.

    Every set below holds **canonical** vocabulary
    (``arie.normalization.taxonomy``), never raw provider strings — the same
    rule the evidence store follows. A criterion written in vendor vocabulary
    would silently stop matching the day a vendor renamed a category.
    """

    name: str
    version: str

    industries: frozenset[str]
    employee_count_min: int
    employee_count_max: int
    geographies: frozenset[str]
    """ISO 3166-1 alpha-2 country codes. Declared, currently unobservable —
    see the module docstring."""

    functions: frozenset[str]
    seniorities: frozenset[str]

    observable_disqualifiers: frozenset[str]
    """Blockers ARIE can actually detect today."""
    declared_disqualifiers: frozenset[str]
    """Blockers the profile names but cannot currently observe. Kept visible
    rather than deleted, so the gap is a documented limitation instead of a
    silent omission."""

    intent_notes: Mapping[str, str]
    """The lossy folds, recorded next to the profile they distort. Keys are the
    brief's vocabulary; values say where each landed and why."""

    def __post_init__(self) -> None:
        # A typo in a canonical value would produce a criterion that can never
        # be met, silently. Cheap to catch at import.
        unknown_industries = self.industries - CANONICAL_INDUSTRIES
        unknown_functions = self.functions - CANONICAL_FUNCTIONS
        unknown_seniorities = self.seniorities - CANONICAL_SENIORITIES
        if unknown_industries or unknown_functions or unknown_seniorities:
            raise ValueError(
                "reference ICP uses non-canonical vocabulary: "
                f"industries={sorted(unknown_industries)} "
                f"functions={sorted(unknown_functions)} "
                f"seniorities={sorted(unknown_seniorities)}"
            )


REFERENCE_ICP_V1 = ReferenceICP(
    name="live-v1-reference",
    version="1.0.0",
    # B2B software / SaaS. `fintech` and `healthtech` are included because the
    # canonical taxonomy routes vertical B2B software into them — a payments
    # software company is the profile's target, and excluding it here while
    # the scorer pays it 15.0 points would put the two out of step.
    industries=frozenset({"software", "fintech", "healthtech"}),
    employee_count_min=50,
    employee_count_max=1000,
    geographies=frozenset({"US", "GB", "AU", "CA"}),
    # sales / revenue_operations / marketing / growth / operations, folded onto
    # the scorer's closed function vocabulary. See `intent_notes`.
    functions=frozenset({"sales", "operations", "marketing"}),
    # director / vp / head / executive, folded onto the scorer's ladder.
    seniorities=frozenset({"director", "vp", "c_level"}),
    observable_disqualifiers=frozenset({"free_email_domain"}),
    declared_disqualifiers=frozenset({"student", "individual_or_freelancer"}),
    intent_notes={
        "revenue_operations": "folded into canonical `operations` — the scorer has no revops weight",
        "growth": "folded into canonical `marketing` — growth is marketing-owned in this profile",
        "head": "folded into canonical `director` — conservative rung; see taxonomy module",
        "executive": "folded into canonical `c_level`",
        "geography": "declared but unobservable — no configured live provider returns a country",
        "student": "declared but unobservable — no trustworthy live source; never inferred",
        "individual_or_freelancer": "declared but unobservable — same reason as `student`",
    },
)


def free_email_disqualifier(canonical_email: str | None) -> bool:
    """The one reference-ICP disqualifier that is genuinely observable today.

    A free/personal mailbox is a real signal that a submission is not a
    business buyer, and ``arie.identity.normalize`` already maintains the
    domain list for its own (identity-resolution) reasons — one list, not two.

    Returns ``False`` for a missing or malformed email: absence of an address
    is not evidence of a personal one. This function deliberately returns a
    plain ``bool`` about *this one observation* and is never written into
    ``disqualifying_flag`` evidence — that field means "a checked blocker
    exists", a claim no current live provider can support.
    """
    if not canonical_email or "@" not in canonical_email:
        return False
    return canonical_email.rsplit("@", 1)[-1].strip().lower() in FREE_EMAIL_DOMAINS


def assess(
    facts: Mapping[str, Any],
    *,
    canonical_email: str | None = None,
    icp: ReferenceICP = REFERENCE_ICP_V1,
) -> ICPAssessment:
    """Describe how a lead's known facts read against ``icp``.

    Descriptive only — no score, no decision, no side effects. The distinction
    that matters is the one between ``MISSED`` (we know, and it is outside the
    profile) and ``UNKNOWN``/``UNOBSERVABLE`` (we do not know, for two
    different reasons). Collapsing those three into "does not fit" is the same
    mistake, one layer up, that the canonical taxonomy exists to prevent.
    """
    criteria: list[ICPCriterion] = [
        _categorical(facts, "industry", icp.industries),
        _headcount(facts, icp),
        _categorical(facts, "title_function", icp.functions),
        _categorical(facts, "title_seniority", icp.seniorities),
    ]

    criteria.append(
        ICPCriterion(
            name="geography",
            verdict=CriterionVerdict.UNOBSERVABLE,
            detail=(
                f"profile targets {sorted(icp.geographies)}; no configured live provider "
                "returns a country"
            ),
        )
    )

    if "free_email_domain" in icp.observable_disqualifiers:
        is_free = free_email_disqualifier(canonical_email)
        criteria.append(
            ICPCriterion(
                name="free_email_domain",
                verdict=CriterionVerdict.MISSED if is_free else CriterionVerdict.MET,
                detail=(
                    "submitted from a free/personal mailbox"
                    if is_free
                    else "not a known free/personal mailbox domain"
                ),
            )
        )

    criteria.extend(
        ICPCriterion(
            name=name,
            verdict=CriterionVerdict.UNOBSERVABLE,
            detail=icp.intent_notes.get(name, "declared but not observable in Live V1"),
        )
        for name in sorted(icp.declared_disqualifiers)
    )

    return ICPAssessment(icp_name=icp.name, icp_version=icp.version, criteria=tuple(criteria))


def _categorical(
    facts: Mapping[str, Any], field_name: str, allowed: frozenset[str]
) -> ICPCriterion:
    value = facts.get(field_name)
    if is_unknown(value):
        return ICPCriterion(
            name=field_name,
            verdict=CriterionVerdict.UNKNOWN,
            detail=f"no usable evidence; profile targets {sorted(allowed)}",
        )
    met = str(value) in allowed
    return ICPCriterion(
        name=field_name,
        verdict=CriterionVerdict.MET if met else CriterionVerdict.MISSED,
        detail=f"observed {value!r}; profile targets {sorted(allowed)}",
    )


def _headcount(facts: Mapping[str, Any], icp: ReferenceICP) -> ICPCriterion:
    value = facts.get("employee_count")
    if is_unknown(value):
        return ICPCriterion(
            name="employee_count",
            verdict=CriterionVerdict.UNKNOWN,
            detail=(
                f"no usable evidence; profile targets "
                f"{icp.employee_count_min}-{icp.employee_count_max}"
            ),
        )
    try:
        count = int(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ICPCriterion(
            name="employee_count",
            verdict=CriterionVerdict.UNKNOWN,
            detail=f"unreadable headcount {value!r}",
        )
    met = icp.employee_count_min <= count <= icp.employee_count_max
    return ICPCriterion(
        name="employee_count",
        verdict=CriterionVerdict.MET if met else CriterionVerdict.MISSED,
        detail=(
            f"observed {count}; profile targets {icp.employee_count_min}-{icp.employee_count_max}"
        ),
    )
