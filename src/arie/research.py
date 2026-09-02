"""Is another piece of evidence worth acquiring at all? — M7 Slice 5.

The question this module exists to answer, deterministically, before anything
else is allowed to run: given what ARIE already knows about a lead, is there
one specific missing fact that could actually change the outcome, and — if
so — is ARIE allowed to go buy it right now?

**The LLM never appears in this file.** Materiality (:func:`analyze_materiality`)
is arithmetic ARIE already has — the same reachable-bounds-vs-threshold
comparison ``arie.scoring.engine.ScoreBounds.settled_decision`` uses to decide
when to stop buying evidence in the first place, just applied per candidate
field instead of in aggregate. Authorization (:func:`authorize_research`) is a
sequence of existing-service lookups (budget, entitlement, credential,
suppression) reduced to one decision. Both are pure functions of primitive
inputs — no database, no provider, no model — so every rule is unit-testable
without a live pipeline call. ``arie.intelligence.research_planning`` is the
only place a model is consulted, and only for wording/selection *after* this
module has already decided a fact is worth asking about.

**Narrow candidate set, on purpose.** :class:`ResearchTargetField` covers only
the four fields ARIE's own providers can genuinely establish today
(``employee_count``, ``industry``, ``title_seniority``, ``title_function``).
``buying_intent``, ``recent_trigger_event``, and ``disqualifying_flag`` are
deliberately excluded — M5's truthfulness rule (arie.jobs.handlers) already
established that ARIE does not synthesize evidence it cannot actually source,
and this module inherits that rule rather than relaxing it for research.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from arie.organizations import SIMULATED

__all__ = [
    "CANDIDATE_LIVE_PROVIDERS",
    "CANDIDATE_SIMULATED_PROVIDERS",
    "DETERMINISTIC_QUESTIONS",
    "FieldMateriality",
    "Materiality",
    "MaterialityAnalysis",
    "ResearchAuthorizationContext",
    "ResearchDecision",
    "ResearchReasonCode",
    "ResearchTargetField",
    "analyze_materiality",
    "authorize_research",
    "select_research_target",
]


class ResearchTargetField(StrEnum):
    """The only fields a research plan may ever name. Mirrors four of
    ``arie.scoring.rules.SCORED_FIELDS`` exactly — the ones an existing
    provider (simulated or live) can actually answer. See this module's
    docstring for why the other three scored fields are absent."""

    EMPLOYEE_COUNT = "employee_count"
    INDUSTRY = "industry"
    TITLE_SENIORITY = "title_seniority"
    TITLE_FUNCTION = "title_function"


class Materiality(StrEnum):
    MATERIAL = "material"
    NON_MATERIAL = "non_material"
    ALREADY_RESOLVED = "already_resolved"


class ResearchReasonCode(StrEnum):
    """Deterministic vocabulary for why a plan was or wasn't approved —
    Advanced Details material, not customer-facing prose on its own."""

    NO_RESEARCH_NEEDED = "no_research_needed"
    DECISION_ALREADY_CLEAR = "decision_already_clear"
    FIELD_ALREADY_KNOWN = "field_already_known"
    MISSING_FIELD_CANNOT_CHANGE_DECISION = "missing_field_cannot_change_decision"
    NO_SUPPORTED_SOURCE = "no_supported_source"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_NOT_CONFIGURED = "provider_not_configured"
    OVER_BUDGET = "over_budget"
    ENTITLEMENT_BLOCKED = "entitlement_blocked"
    SUPPRESSED_RECENT_FAILURE = "suppressed_recent_failure"
    EXECUTION_MODE_BLOCKED = "execution_mode_blocked"
    RESEARCH_APPROVED = "research_approved"
    LLM_UNAVAILABLE = "llm_unavailable"
    NO_USEFUL_QUESTION = "no_useful_question"


# ----------------------------------------------------- provider capability --
#
# Field -> candidate providers, cheapest first. Declarative, reused rather
# than duplicated: names and per-field coverage are read from the actual
# catalogues (`arie.providers.catalog` for simulated, the three live adapter
# modules for real), not retyped here.

CANDIDATE_SIMULATED_PROVIDERS: dict[ResearchTargetField, tuple[str, ...]] = {
    ResearchTargetField.EMPLOYEE_COUNT: (
        "internal_crm",
        "firmographics_basic",
        "firmographics_premium",
        "deep_research",
    ),
    ResearchTargetField.INDUSTRY: (
        "internal_crm",
        "dns_web",
        "firmographics_basic",
        "firmographics_premium",
        "deep_research",
    ),
    ResearchTargetField.TITLE_SENIORITY: ("inbound_payload", "contact_enrich"),
    ResearchTargetField.TITLE_FUNCTION: ("inbound_payload", "contact_enrich"),
}
"""Ordered by `arie.providers.catalog.CATALOG`'s own cheapest-first sequence,
filtered to providers whose `provides_fields` includes this field."""

CANDIDATE_LIVE_PROVIDERS: dict[ResearchTargetField, tuple[str, ...]] = {
    ResearchTargetField.EMPLOYEE_COUNT: ("abstract_company_enrichment",),
    ResearchTargetField.INDUSTRY: ("abstract_company_enrichment",),
    ResearchTargetField.TITLE_SENIORITY: ("hunter_combined_enrichment", "apollo_person_enrichment"),
    ResearchTargetField.TITLE_FUNCTION: ("hunter_combined_enrichment", "apollo_person_enrichment"),
}
"""Mirrors `arie.live.providers.REGISTERED_LIVE_PROVIDER_NAMES`'s own
cheapest-first order (Abstract, then Hunter, then Apollo) — see
`arie.providers.live_abstract.PROVIDES_FIELDS` / `hunter_contract
.HUNTER_PROVIDES_FIELDS` / `apollo_contract.APOLLO_PROVIDES_FIELDS`, the
actual declarations this table transcribes."""

DETERMINISTIC_QUESTIONS: dict[ResearchTargetField, str] = {
    ResearchTargetField.EMPLOYEE_COUNT: "Approximately how many employees does this company have?",
    ResearchTargetField.INDUSTRY: "What industry does this company operate in?",
    ResearchTargetField.TITLE_SENIORITY: "How senior is this contact within their organization?",
    ResearchTargetField.TITLE_FUNCTION: "What functional area does this contact work in?",
}
"""Used with zero LLM calls whenever exactly one field is material — Part Y's
cost-discipline rule that ARIE must not pay for wording it already knows."""


@dataclass(frozen=True)
class FieldMateriality:
    field: ResearchTargetField
    materiality: Materiality
    ceiling_points: float
    """This field's maximum possible contribution under the organization's
    active scoring config (`arie.scoring.rules.ScoringConfig.max_field_points`)
    — the basis for the crossing check below, and shown in Advanced Details."""


@dataclass(frozen=True)
class MaterialityAnalysis:
    decision_already_clear: bool
    """True when the reachable score range (`arie.scoring.engine.ScoreBounds`,
    already computed and frozen on the receipt) sits entirely on one side of
    the qualify/reject thresholds, or entirely inside the borderline band —
    the identical "no purchasable evidence can change this" condition
    `arie.scoring.engine.ScoreBounds.settled_decision` already expresses for
    the *aggregate* of every unknown field. No unknown field is material when
    this is true, regardless of its own individual ceiling."""
    fields: tuple[FieldMateriality, ...]

    @property
    def material_fields(self) -> tuple[FieldMateriality, ...]:
        return tuple(f for f in self.fields if f.materiality is Materiality.MATERIAL)


def analyze_materiality(
    *,
    score_value: float,
    threshold_qualify: float,
    threshold_reject: float,
    bounds_lower: float,
    bounds_upper: float,
    known_fields: frozenset[str],
    field_ceilings: Mapping[str, float],
) -> MaterialityAnalysis:
    """Which of the four candidate fields could still change this lead's
    decision, given only arithmetic ARIE already computed at decision time.

    `bounds_lower`/`bounds_upper` come straight off the receipt
    (`ReceiptScore.bounds`) — the *aggregate* reachable range across every
    field that was unknown at decision time, disqualifier included. A single
    candidate field's own ceiling is compared against `score_value` alone,
    never against the aggregate bounds: the aggregate already reflects every
    unknown field's combined best case, so re-using it per field would credit
    one field with contributions that are actually other fields'.
    """
    decision_already_clear = (
        bounds_lower >= threshold_qualify
        or bounds_upper < threshold_reject
        or (bounds_lower >= threshold_reject and bounds_upper < threshold_qualify)
    )
    # `bounds_lower < score_value` only happens when an unknown disqualifier
    # pinned the floor to zero (`arie.scoring.engine.compute_bounds`) — while
    # that gate is unresolved, no additive field among the four candidates
    # can independently prove the lead safe to route, no matter its ceiling.
    disqualifier_pinned = bounds_lower < score_value

    results: list[FieldMateriality] = []
    for target in ResearchTargetField:
        ceiling = field_ceilings.get(str(target), 0.0)
        if str(target) in known_fields:
            materiality = Materiality.ALREADY_RESOLVED
        elif decision_already_clear or disqualifier_pinned:
            materiality = Materiality.NON_MATERIAL
        else:
            best_case = score_value + ceiling
            crosses_reject = score_value < threshold_reject <= best_case
            crosses_qualify = score_value < threshold_qualify <= best_case
            materiality = (
                Materiality.MATERIAL
                if (crosses_reject or crosses_qualify)
                else Materiality.NON_MATERIAL
            )
        results.append(
            FieldMateriality(field=target, materiality=materiality, ceiling_points=ceiling)
        )

    return MaterialityAnalysis(decision_already_clear=decision_already_clear, fields=tuple(results))


def select_research_target(analysis: MaterialityAnalysis) -> ResearchTargetField | None:
    """The single best next fact, or `None` if nothing is material.

    Deterministic ranking: larger decision impact (ceiling) first, then
    `ResearchTargetField`'s own declaration order as a stable tiebreak. ARIE
    always proposes exactly one target — never a list — per this module's
    "one best fact" identity; a caller wanting to explain *why* this one over
    another reads `ceiling_points` off `analysis.material_fields`.
    """
    material = analysis.material_fields
    if not material:
        return None
    ranked = sorted(
        material,
        key=lambda f: (-f.ceiling_points, list(ResearchTargetField).index(f.field)),
    )
    return ranked[0].field


@dataclass(frozen=True)
class ResearchAuthorizationContext:
    """Everything `authorize_research` needs, pre-fetched by the caller from
    existing services — kept primitive-typed so authorization stays a pure,
    directly testable function or every one of Part B's reason codes."""

    target_field: ResearchTargetField
    materiality: Materiality
    decision_already_clear: bool
    candidate_providers: tuple[str, ...]
    """Cheapest-first providers capable of this field, before availability
    is applied — `()` means no source exists in this deployment at all."""
    unavailable_providers: Mapping[str, str]
    """`{provider: reason}` for a candidate not usable by this organization
    right now — `arie.live.provider_availability.UNAVAILABILITY_REASONS`
    vocabulary for live mode; always empty for simulated mode, which has no
    organization-configuration gate."""
    suppressed_providers: frozenset[str]
    """Candidates in a recent-failure cooldown (`arie.live.outcome_cache`) —
    live mode only."""
    execution_mode: str
    entitled_live: bool
    """Irrelevant (and ignored) when `execution_mode == arie.organizations.SIMULATED`."""
    estimated_cost_usd: Decimal
    lead_spent_usd: Decimal
    lead_budget_cap_usd: Decimal
    org_modeled_spend_remaining_usd: Decimal


@dataclass(frozen=True)
class ResearchDecision:
    approved: bool
    reason_code: ResearchReasonCode
    detail: str
    chosen_provider: str | None = None
    estimated_cost_usd: Decimal | None = None


_REASON_DETAIL: dict[ResearchReasonCode, str] = {
    ResearchReasonCode.DECISION_ALREADY_CLEAR: (
        "Given everything already known, no additional fact could change this recommendation."
    ),
    ResearchReasonCode.FIELD_ALREADY_KNOWN: "This information is already known for this lead.",
    ResearchReasonCode.MISSING_FIELD_CANNOT_CHANGE_DECISION: (
        "Even a best-case answer for this field could not change the recommendation."
    ),
    ResearchReasonCode.NO_SUPPORTED_SOURCE: "No configured data source can answer this question.",
    ResearchReasonCode.PROVIDER_UNAVAILABLE: (
        "The data source for this question is temporarily unavailable."
    ),
    ResearchReasonCode.PROVIDER_NOT_CONFIGURED: (
        "The data source for this question isn't configured for this organization."
    ),
    ResearchReasonCode.OVER_BUDGET: (
        "More information could help, but research is unavailable because the research "
        "budget has been reached."
    ),
    ResearchReasonCode.ENTITLEMENT_BLOCKED: "This plan does not include live research.",
    ResearchReasonCode.SUPPRESSED_RECENT_FAILURE: (
        "This data source recently failed for this company or contact and is temporarily paused."
    ),
    ResearchReasonCode.EXECUTION_MODE_BLOCKED: (
        "Research can be planned but not yet performed automatically in this mode."
    ),
    ResearchReasonCode.RESEARCH_APPROVED: "Research is available for this lead.",
}


def _refuse(reason: ResearchReasonCode) -> ResearchDecision:
    return ResearchDecision(approved=False, reason_code=reason, detail=_REASON_DETAIL[reason])


def authorize_research(ctx: ResearchAuthorizationContext) -> ResearchDecision:
    """Every provider-spend gate ARIE has, reduced to one approve/refuse call.

    Ordered so the reason a customer sees is the most specific true blocker,
    not merely the first one checked: materiality first (nothing about
    provider state matters if the fact can't change anything), then whether a
    source exists at all, then organization-level entitlement and budget, and
    only last whether this execution mode is currently allowed to *act* on an
    approved plan — a customer whose org isn't even configured for a provider
    should be told that, not "not supported in this mode."
    """
    if ctx.decision_already_clear:
        return _refuse(ResearchReasonCode.DECISION_ALREADY_CLEAR)
    if ctx.materiality is Materiality.ALREADY_RESOLVED:
        return _refuse(ResearchReasonCode.FIELD_ALREADY_KNOWN)
    if ctx.materiality is not Materiality.MATERIAL:
        return _refuse(ResearchReasonCode.MISSING_FIELD_CANNOT_CHANGE_DECISION)
    if not ctx.candidate_providers:
        return _refuse(ResearchReasonCode.NO_SUPPORTED_SOURCE)

    available = [
        p
        for p in ctx.candidate_providers
        if p not in ctx.unavailable_providers and p not in ctx.suppressed_providers
    ]
    if not available:
        if all(p in ctx.suppressed_providers for p in ctx.candidate_providers):
            return _refuse(ResearchReasonCode.SUPPRESSED_RECENT_FAILURE)
        if all(
            ctx.unavailable_providers.get(p) == "provider_not_configured"
            for p in ctx.candidate_providers
        ):
            return _refuse(ResearchReasonCode.PROVIDER_NOT_CONFIGURED)
        return _refuse(ResearchReasonCode.PROVIDER_UNAVAILABLE)

    if ctx.execution_mode != SIMULATED and not ctx.entitled_live:
        return _refuse(ResearchReasonCode.ENTITLEMENT_BLOCKED)
    if ctx.lead_spent_usd + ctx.estimated_cost_usd > ctx.lead_budget_cap_usd:
        return _refuse(ResearchReasonCode.OVER_BUDGET)
    if ctx.org_modeled_spend_remaining_usd < ctx.estimated_cost_usd:
        return _refuse(ResearchReasonCode.OVER_BUDGET)
    if ctx.execution_mode != SIMULATED:
        # Part S: live execution stays plan-only in this slice — existing
        # live-shadow/human-only policy is not widened by adding a planner.
        return _refuse(ResearchReasonCode.EXECUTION_MODE_BLOCKED)

    return ResearchDecision(
        approved=True,
        reason_code=ResearchReasonCode.RESEARCH_APPROVED,
        detail=_REASON_DETAIL[ResearchReasonCode.RESEARCH_APPROVED],
        chosen_provider=available[0],
        estimated_cost_usd=ctx.estimated_cost_usd,
    )
