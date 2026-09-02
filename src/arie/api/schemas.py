"""Request and response models for the ingestion API.

Validation reuses ``arie.identity.normalize`` rather than restating the rules in
Pydantic constraints. That matters more than it looks: the normalizer is what
decides whether two spellings of an address are the same person, so a validator
that accepted anything the normalizer would later reject — or vice versa — would
put the API's idea of a valid lead and the identity resolver's idea of one out
of step. Calling the same function keeps them the same function.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arie.api.ingest import LeadIngestCommand
from arie.api.receipt import DecisionReceipt
from arie.apikeys import SCOPES
from arie.approval.workflow import ReviewAction
from arie.auth import ROLES
from arie.billing.service import PURCHASABLE_PLANS
from arie.copilot import CopilotIntent, CopilotResponse, LeadCopilotResponse
from arie.core.types import LeadStatus
from arie.feedback import FeedbackReason, FeedbackRecord, FeedbackSentiment
from arie.icp_profiles import InvalidICPConfigError, validate_config
from arie.identity.normalize import normalize_domain, normalize_email
from arie.intelligence.explanation import ExplanationOutcome, LeadExplanation
from arie.intelligence.schemas import BusinessProfileDraft, TargetingObjective
from arie.organizations import EXECUTION_MODES, InvalidOrganizationSettingsError, validate_timezone
from arie.recommendations import (
    ConfidenceBand,
    CustomerPriority,
    LeadRecommendation,
    NextAction,
    ResearchStatus,
)
from arie.research import Materiality, ResearchReasonCode, ResearchTargetField
from arie.research_acquisition import ResearchExecutionResult, ResearchPlanResult


class IngestLeadRequest(BaseModel):
    """One inbound lead. ``email`` is the only genuinely required identity field.

    Company identity is optional because it is recoverable: with no
    ``company_domain``, resolution falls back to the email's own domain, and
    with no usable domain at all (a free-mail sender) it falls back to
    ``company_name``. Requiring a domain up front would reject leads the
    resolver can handle perfectly well.
    """

    source: Annotated[str, Field(min_length=1, max_length=100)]
    """Which upstream system sent this — half of the deduplication key."""

    email: Annotated[str, Field(min_length=3, max_length=320)]

    external_ref: Annotated[str | None, Field(default=None, max_length=200)] = None
    """The upstream system's own id for this record — the other half of the
    deduplication key. Omitting it means this lead cannot be deduplicated and
    every delivery creates a new one."""

    company_domain: Annotated[str | None, Field(default=None, max_length=253)] = None
    company_name: Annotated[str | None, Field(default=None, max_length=200)] = None
    full_name: Annotated[str | None, Field(default=None, max_length=200)] = None
    title: Annotated[str | None, Field(default=None, max_length=200)] = None

    budget_usd_cap: Annotated[Decimal | None, Field(default=None, gt=0)] = None
    """Per-lead spend ceiling. Defaults to ``PolicyConfig.lead_budget_usd_cap``."""

    mode: Literal["normal", "shadow"] = "normal"
    """Post-M1 P5. ``"shadow"`` tells ARIE to compute its full recommendation
    (evidence, cost, confidence, stop reason) without taking any authoritative
    action — no autonomous routing, no human review opened, nothing an n8n
    outcome-sync consumer would treat as finalized. Fixed at creation: a
    redelivery of the same ``(source, external_ref)`` with a different `mode`
    does not change it, the same "first write wins" rule every other optional
    ingestion field already follows — the persisted value is always in the
    response's `is_shadow`."""

    @field_validator("email")
    @classmethod
    def _email_is_normalizable(cls, value: str) -> str:
        normalize_email(value)  # raises ValueError -> 422, with the reason attached
        return value

    @field_validator("company_domain")
    @classmethod
    def _domain_is_normalizable(cls, value: str | None) -> str | None:
        if value is not None:
            normalize_domain(value)
        return value

    def to_command(self, *, organization_id: UUID) -> LeadIngestCommand:
        """`organization_id` comes from the caller's `AuthContext`, never from
        the request body — a client cannot ingest a lead into an organization
        it wasn't authenticated against."""
        return LeadIngestCommand(
            source=self.source,
            email=self.email,
            organization_id=organization_id,
            external_ref=self.external_ref,
            company_domain=self.company_domain,
            company_name=self.company_name,
            full_name=self.full_name,
            title=self.title,
            budget_usd_cap=self.budget_usd_cap,
            is_shadow=self.mode == "shadow",
        )


class IngestLeadResponse(BaseModel):
    lead_id: UUID
    status: LeadStatus
    created: bool
    """False if ``(source, external_ref)`` already existed. The response is
    otherwise identical, and the HTTP status distinguishes them: 201 for a lead
    this request created, 200 for one it matched."""
    company_id: UUID
    person_id: UUID
    job_id: UUID
    job_created: bool
    job_requeued: bool
    """True if this delivery found the job permanently failed (``dead_letter``)
    and reset it to ``pending`` with a fresh attempt budget, rather than
    finding a live or completed job untouched."""
    is_shadow: bool
    """The persisted shadow flag — may differ from this request's own `mode`
    if `(source, external_ref)` already existed under a different mode."""


class LeadCostResponse(BaseModel):
    """Spend so far, read through ``v_lead_cost``."""

    provider_cost_usd: Decimal
    model_cost_usd: Decimal
    total_cost_usd: Decimal
    provider_calls: int
    cache_hits: int
    provider_latency_ms: int


class LeadResponse(BaseModel):
    lead_id: UUID
    status: LeadStatus
    version: int
    source: str
    external_ref: str | None
    company_id: UUID | None
    person_id: UUID | None
    budget_usd_cap: Decimal
    is_shadow: bool
    created_at: datetime
    updated_at: datetime
    cost: LeadCostResponse


class HealthResponse(BaseModel):
    status: str
    """``"ok"``, ``"degraded"`` (database reachable, schema not fully applied
    yet), or ``"down"`` (database unreachable)."""
    database: bool
    schema_ready: bool
    """Every migration in ``migrations/`` has a matching ``schema_migrations``
    row. False while a deploy's migration step is still running, or hasn't
    run — a state that looks identical to ``database: True`` alone but needs
    a different fix (wait for migrations, not restart the process)."""


class WorkerHealthResponse(BaseModel):
    """`GET /healthz/worker` (Productization M6 Part 28). Deliberately
    separate from `HealthResponse` — a worker fleet being down must never
    change `/healthz`'s own status code, or a Better Stack monitor on the
    API's liveness would flap for a problem the API process cannot fix."""

    model_config = ConfigDict(from_attributes=True)

    healthy: bool
    active_workers: int
    most_recent_heartbeat_at: datetime | None


class ReviewResponse(BaseModel):
    """One human review, joined with the lead's live status and version."""

    review_id: UUID
    lead_id: UUID
    requested_at: datetime
    reviewer: str | None
    original_decision: str | None
    final_decision: str | None
    notes: str | None
    responded_at: datetime | None
    is_pending: bool
    lead_status: LeadStatus
    lead_version: int
    """Pass this back as `expected_lead_version` when submitting a decision —
    the same optimistic-concurrency contract `GET /leads/{lead_id}` implies
    for every other write in this API."""


class ReviewDecisionRequest(BaseModel):
    """One reviewer's answer to a pending review.

    ``action`` is the human-facing verb (approve/reject/edit); ``arie.
    approval.workflow`` translates it into the decision-label vocabulary the
    state graph and ``v_escalation_rate`` already use — this request never
    speaks that vocabulary directly.
    """

    action: ReviewAction
    reviewer: Annotated[str, Field(min_length=1, max_length=200)]
    notes: Annotated[str | None, Field(default=None, max_length=2000)] = None
    expected_lead_version: Annotated[int, Field(ge=1)]
    """The lead's `version` at the time this review was read (`GET
    /reviews/{review_id}`). A stale value fails with 409, matching every
    other optimistic-concurrency write in this API."""

    @model_validator(mode="after")
    def _edit_requires_notes(self) -> ReviewDecisionRequest:
        if self.action == ReviewAction.EDIT and not (self.notes and self.notes.strip()):
            raise ValueError("action 'edit' requires non-empty notes explaining the override")
        return self


class ReviewDecisionResponse(BaseModel):
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
    """True if this exact decision was already recorded by an earlier,
    identical submission — the idempotent-retry path, not an error."""


# --------------------------------------------------------------- receipt --
#
# One-to-one with `arie.api.receipt`'s dataclasses — `from_attributes=True`
# lets `ReceiptResponse.model_validate(receipt)` convert the dataclass tree
# directly rather than this module restating every field assignment.


class ReceiptDecisionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    recommended_action: str
    autonomous: bool
    final_status: LeadStatus
    human_override: bool
    autonomy_guard: str | None = None
    """Live V1 Foundation. Additive and nullable: `None` for every simulated
    receipt, which is every receipt an existing consumer has ever seen, so no
    client breaks on it."""


class ReceiptScoreBoundsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lower: float
    upper: float


class ReceiptScoreResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    value: float
    threshold_qualify: float
    threshold_reject: float
    bounds: ReceiptScoreBoundsResponse
    confidence: float
    tau: float


class ReceiptStoppingResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    reason_code: str
    explanation: str


class ReceiptCostResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider_cost_usd: Decimal
    model_cost_usd: Decimal
    total_cost_usd: Decimal
    budget_usd_cap: Decimal


class ReceiptEvidenceItemResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    field: str
    source: str
    confidence: float
    contested: bool


class ReceiptEvidenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    cache_hits: int
    provider_calls: int
    items: list[ReceiptEvidenceItemResponse]
    unknown_fields: list[str]


class ReceiptProviderCallResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    status: str
    cost_usd: Decimal
    latency_ms: int | None
    cache_hit: bool
    suppressed_reason: str | None = None
    credential_source: str | None = None
    """Productization M5 — see `arie.api.receipt.ReceiptProviderCall.
    credential_source`. Additive and nullable: absent on every receipt an
    existing client has ever seen."""
    actual_cost_usd: Decimal | None = None
    """Productization M5 — see `arie.api.receipt.ReceiptProviderCall.
    actual_cost_usd`."""


class ReceiptProvidersResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    called: list[ReceiptProviderCallResponse]
    not_called: list[str]
    unavailable: dict[str, str] = Field(default_factory=dict)
    """Productization M5 — see `arie.api.receipt.ReceiptProviders.
    unavailable`."""


class ReceiptHumanReviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    review_id: UUID
    required: bool
    reviewer: str | None
    original_decision: str | None
    action: str | None
    final_decision: str | None
    responded_at: datetime | None


class ReceiptVersionsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    policy: str
    scorer: str
    confidence_calibration: str
    icp_profile_id: UUID | None = None
    """Productization M3 — see `arie.api.receipt.ReceiptVersions.icp_profile_id`."""
    icp_profile_version: int | None = None


class ReceiptResponse(BaseModel):
    """`GET /leads/{lead_id}/receipt` — see `arie.api.receipt.DecisionReceipt`
    for what each field can and can't truthfully claim."""

    model_config = ConfigDict(from_attributes=True)

    receipt_version: str
    lead_id: UUID
    status: str
    """"pending", "processing_failed", or "decided" — see `DecisionReceipt.status`."""
    lead_status: LeadStatus
    created_at: datetime | None
    shadow: bool
    """Post-M1 P5 — see `arie.api.receipt.DecisionReceipt.shadow`."""
    execution_mode: str | None = None
    """Productization M5 — see `arie.api.receipt.DecisionReceipt.
    execution_mode`. Additive and nullable: `None` for every receipt an
    existing client has ever seen."""

    decision: ReceiptDecisionResponse | None
    score: ReceiptScoreResponse | None
    stopping: ReceiptStoppingResponse | None
    versions: ReceiptVersionsResponse | None

    cost: ReceiptCostResponse
    evidence: ReceiptEvidenceResponse
    providers: ReceiptProvidersResponse
    human_review: ReceiptHumanReviewResponse | None

    @classmethod
    def from_receipt(cls, receipt: DecisionReceipt) -> ReceiptResponse:
        return cls.model_validate(receipt)


# ----------------------------------------------------------- recommendation --
#
# M7 Slice 4. The customer-facing surface — see `arie.recommendations` for why
# every field below is derived deterministically from a `DecisionReceipt`
# rather than by a model. `ReceiptResponse` above remains available under
# "Advanced Details"; this is what a customer sees first.


class LeadRecommendationResponse(BaseModel):
    lead_id: UUID
    priority: CustomerPriority
    next_action: NextAction
    machine_decision: str | None
    score: float | None
    confidence: float | None
    confidence_band: ConfidenceBand | None
    short_reason: str
    key_evidence: list[str]
    missing_information: list[str]
    research_status: ResearchStatus
    explanation_status: str
    profile_version: int | None
    shadow: bool
    execution_mode: str | None

    @classmethod
    def from_recommendation(cls, recommendation: LeadRecommendation) -> LeadRecommendationResponse:
        return cls(
            lead_id=recommendation.lead_id,
            priority=recommendation.priority,
            next_action=recommendation.next_action,
            machine_decision=recommendation.machine_decision,
            score=recommendation.score,
            confidence=recommendation.confidence,
            confidence_band=recommendation.confidence_band,
            short_reason=recommendation.short_reason,
            key_evidence=recommendation.key_evidence,
            missing_information=recommendation.missing_information,
            research_status=recommendation.research_status,
            explanation_status=recommendation.explanation_status,
            profile_version=recommendation.profile_version,
            shadow=recommendation.shadow,
            execution_mode=recommendation.execution_mode,
        )


class EvidenceGroundedClaimResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    text: str
    evidence_ids: list[UUID]
    hypothesis: bool


class LeadExplanationResponse(BaseModel):
    """`POST /leads/{lead_id}/explanation` — the AI-authored (or deterministic
    fallback) "why", cited to evidence ids a caller can cross-reference against
    Advanced Details. `source` is never hidden from the client: a customer is
    always told whether they are reading AI prose or ARIE's built-in fallback,
    per the M7 Slice 4 brief's truthfulness rule."""

    summary: str
    claims: list[EvidenceGroundedClaimResponse]
    missing_information: list[str]
    hypothesis_notes: list[str]
    source: str
    """`"ai"` or `"deterministic"` — see `arie.intelligence.explanation.ExplanationOutcome`."""
    unavailable_reason: str | None = None

    @classmethod
    def from_outcome(cls, outcome: ExplanationOutcome) -> LeadExplanationResponse:
        explanation: LeadExplanation = outcome.explanation
        return cls(
            summary=explanation.summary,
            claims=[
                EvidenceGroundedClaimResponse.model_validate(claim) for claim in explanation.claims
            ],
            missing_information=explanation.missing_information,
            hypothesis_notes=explanation.hypothesis_notes,
            source=outcome.source,
            unavailable_reason=outcome.unavailable_reason,
        )


# ------------------------------------------------------------------ feedback --
#
# M7 Slice 4, Part I. An observation on a recommendation, never a mutation —
# see `migrations/0036_lead_recommendation_feedback.sql`.


class SubmitFeedbackRequest(BaseModel):
    sentiment: FeedbackSentiment
    reason: FeedbackReason | None = None
    note: Annotated[str | None, Field(default=None, max_length=2000)] = None


class FeedbackResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    feedback_id: UUID
    lead_id: UUID
    profile_version: int | None
    recommendation_priority: str
    recommendation_next_action: str
    sentiment: str
    reason: str | None
    note: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_record(cls, record: FeedbackRecord) -> FeedbackResponse:
        return cls.model_validate(record)


# ------------------------------------------------------------------ research --
#
# M7 Slice 5. A plan proposes; execution recomputes authorization from
# scratch and, if approved, performs one simulated provider call — see
# `arie.research_acquisition`'s own module docstring for why this never
# rewrites the immutable Decision Receipt.


class ResearchPlanResponse(BaseModel):
    """`POST /leads/{lead_id}/research-plan`'s response — a proposal, never
    an action. `approved` tells the client whether `POST /leads/{lead_id}
    /research` would currently succeed for `target_field`; the client never
    computes that itself."""

    target_field: ResearchTargetField | None
    question: str | None
    rationale: str | None
    materiality: Materiality | None
    decision_already_clear: bool
    candidate_sources: list[str]
    estimated_cost_usd: Decimal | None
    reason_code: ResearchReasonCode
    detail: str
    approved: bool
    llm_used: bool

    @classmethod
    def from_result(cls, result: ResearchPlanResult) -> ResearchPlanResponse:
        return cls(
            target_field=result.target_field,
            question=result.question,
            rationale=result.rationale,
            materiality=result.materiality,
            decision_already_clear=result.decision_already_clear,
            candidate_sources=list(result.candidate_sources),
            estimated_cost_usd=result.estimated_cost_usd,
            reason_code=result.reason_code,
            detail=result.detail,
            approved=result.approved,
            llm_used=result.llm_used,
        )


class ExecuteResearchRequest(BaseModel):
    """`POST /leads/{lead_id}/research`. Only `target_field` is client-
    supplied — provider, cost, and approval are always recomputed server-side
    from current state, never trusted from the request."""

    target_field: ResearchTargetField


class ResearchPreviewResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    score: float
    bounds_lower: float
    bounds_upper: float
    likely_outcome: str


class ResearchExecutionResponse(BaseModel):
    approved: bool
    reason_code: ResearchReasonCode
    detail: str
    target_field: ResearchTargetField | None
    provider: str | None
    found_value: Any | None
    cost_usd: Decimal
    preview: ResearchPreviewResponse | None

    @classmethod
    def from_result(cls, result: ResearchExecutionResult) -> ResearchExecutionResponse:
        return cls(
            approved=result.approved,
            reason_code=result.reason_code,
            detail=result.detail,
            target_field=result.target_field,
            provider=result.provider,
            found_value=result.found_value,
            cost_usd=result.cost_usd,
            preview=(
                ResearchPreviewResponse.model_validate(result.preview)
                if result.preview is not None
                else None
            ),
        )


# ------------------------------------------------------------------ api keys --
#
# Productization M2A. `arie.apikeys.SCOPES` stays the single source of truth
# for the allowed scope vocabulary (also enforced by the database's own CHECK
# constraint, migrations/0017_organization_api_keys.sql) — validated here so a
# typo'd scope is rejected as 422 rather than surfacing as a 500 from the
# database constraint.


class CreateApiKeyRequest(BaseModel):
    """`POST /api-keys` — mint a new machine credential for this organization.

    Requires an owner/admin JWT session (`AuthContext.is_org_admin`); an
    API-key-authenticated request is refused regardless of its own scopes —
    no scope grants organization management, on purpose.
    """

    label: Annotated[str, Field(min_length=1, max_length=200)]
    scopes: list[str] = Field(default_factory=list)
    """Empty is valid and deliberately not the default a caller should want —
    a key with no scopes authenticates but can perform no data-plane action,
    which is a safe (if useless) starting point, never an error."""

    @field_validator("scopes")
    @classmethod
    def _scopes_are_known(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - set(SCOPES))
        if unknown:
            raise ValueError(f"unknown scope(s) {unknown} — must be one of {list(SCOPES)}")
        return value


class ApiKeyResponse(BaseModel):
    """One API key's metadata. Never the raw key, never its hash — see
    `arie.apikeys.ApiKeyRecord`, which this mirrors field for field."""

    model_config = ConfigDict(from_attributes=True)

    key_id: UUID
    label: str
    key_prefix: str
    scopes: list[str]
    created_by_user_id: UUID
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None


class ApiKeyCreatedResponse(ApiKeyResponse):
    """`POST /api-keys`'s response — the one and only place the raw key is
    ever shown. Not retrievable afterward by any endpoint; losing it means
    revoking the key and creating a new one."""

    raw_key: str


# Productization M3. `arie.icp_profiles.validate_config` stays the single
# source of truth for the actual rules (weight sums, threshold ordering, band
# well-formedness) — reused here via a model validator rather than restated as
# separate Pydantic constraints, the same discipline this module's own
# docstring states for `arie.identity.normalize`.


class EmployeeCountBandInput(BaseModel):
    min_employees: Annotated[int, Field(ge=0)]
    max_employees: Annotated[int, Field(ge=0)]
    points: Annotated[float, Field(ge=0)]


class ICPProfileConfigInput(BaseModel):
    """The `config` an organization submits for a new ICP profile version —
    mirrors `arie.icp_profiles.REFERENCE_CONFIG`'s shape field for field.
    """

    qualify_threshold: float
    reject_threshold: float
    employee_count_bands: Annotated[list[EmployeeCountBandInput], Field(min_length=1)]
    industry_points: dict[str, float] = Field(default_factory=dict)
    seniority_points: dict[str, float] = Field(default_factory=dict)
    function_points: dict[str, float] = Field(default_factory=dict)
    buying_intent_weight: float
    trigger_event_weight: float
    target_geographies: list[str] = Field(default_factory=list)
    """Advisory only — no evidence field supplies geography today, so this
    never affects scoring. See `arie.icp_profiles`'s module docstring."""
    disqualifier_enabled: bool = True

    @model_validator(mode="after")
    def _validate_with_domain_rules(self) -> ICPProfileConfigInput:
        try:
            validate_config(self.model_dump(mode="json"))
        except InvalidICPConfigError as exc:
            raise ValueError(str(exc)) from exc
        return self


class CreateICPProfileRequest(BaseModel):
    """`POST /organization/icp` — create a new ICP profile version, which
    immediately becomes active for this organization (see
    `arie.icp_profiles.create_profile`). Requires an owner/admin JWT session.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)]
    config: ICPProfileConfigInput


class ICPProfileResponse(BaseModel):
    """One ICP profile version. Mirrors `arie.icp_profiles.ICPProfileRecord`
    field for field."""

    model_config = ConfigDict(from_attributes=True)

    profile_id: UUID
    organization_id: UUID
    version: int
    name: str
    config: dict[str, Any]
    scorer_version: str
    status: str
    created_by_user_id: UUID | None
    created_at: datetime
    activated_at: datetime
    retired_at: datetime | None


# ---------------------------------------------------------- organization settings --
#
# Productization M4 Part 1. `GET /organization` is read-gated identically to
# ICP configuration (`_require_jwt_session` — any active member); `PATCH
# /organization` requires an owner/admin JWT session
# (`_require_org_admin`), same rule as ICP writes and API keys.


class OrganizationResponse(BaseModel):
    """Mirrors `arie.organizations.OrganizationRecord` field for field."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: UUID
    name: str
    slug: str
    status: str
    timezone: str
    company_domain: str | None
    execution_mode: str
    """Productization M5 Part 14 — one of `arie.organizations.EXECUTION_MODES`.
    Read-only here; change it via `PATCH /organization/execution-mode`, a
    separate, owner/admin-only, individually audited endpoint — see that
    route and `arie.organizations.set_execution_mode`."""
    onboarding_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CreateOrganizationRequest(BaseModel):
    """`POST /organizations` (Productization M6 Part 10) — self-service
    provisioning for an already-authenticated, already email-verified
    Supabase user with no organization yet. Deliberately takes only a display
    name: `organization_id`/`slug` are always server-generated
    (`arie.provisioning.create_customer_organization`), so there is no field
    here through which a caller could attach themselves to an existing
    organization.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)]
    turnstile_token: str | None = None
    """Cloudflare Turnstile response token (Part 12) — verified server-side
    via `arie.turnstile.verify_turnstile_token`. `None` is accepted
    unconditionally only when Turnstile isn't configured at all (dev/CI); see
    that module's own docstring."""


class CreateOrganizationResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    organization_id: UUID
    slug: str


# --------------------------------------------------------------------- billing --
#
# Productization M6 Parts 2-9, 17-19. `GET /billing` and the Checkout/Portal
# routes require an owner/admin JWT session (`_require_org_admin`) — Part 37's
# "viewing billing: owner/admin by default" — and are refused for an API key
# outright the same way `_require_org_admin` already refuses API keys for
# every organization-management action.


class EffectiveEntitlementsResponse(BaseModel):
    """Mirrors `arie.billing.plans.EffectiveEntitlements` field for field."""

    model_config = ConfigDict(from_attributes=True)

    plan: str
    max_leads_per_month: int
    max_csv_rows_per_upload: int
    max_modeled_spend_usd_per_month: float
    max_members: int
    live_provider_feature_allowed: bool


class OrganizationBillingResponse(BaseModel):
    """Mirrors `arie.billing.models.OrganizationBillingRecord`, minus
    `last_event_created_at` (internal webhook-ordering bookkeeping — not
    useful to a frontend)."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: UUID
    stripe_customer_id: str | None
    stripe_subscription_id: str | None
    plan: str
    status: str
    current_period_start: datetime | None
    current_period_end: datetime | None
    cancel_at_period_end: bool
    canceled_at: datetime | None
    created_at: datetime
    updated_at: datetime


class BillingResponse(BaseModel):
    """`GET /billing`'s response — the raw Stripe-mirrored subscription state
    alongside the resolved entitlements it currently maps to (Part 18: plan,
    status, period dates, cancel-at-period-end, effective entitlements)."""

    billing: OrganizationBillingResponse
    entitlements: EffectiveEntitlementsResponse


class StartCheckoutRequest(BaseModel):
    """`POST /billing/checkout`. `plan` is validated against
    `arie.billing.service.PURCHASABLE_PLANS` here so a client sending an
    unpurchasable value (`internal`, or garbage) gets a clean 422 rather than
    reaching `arie.billing.service.start_checkout`'s own runtime check. The
    actual Stripe Price id is never client-supplied — see
    `arie.billing.stripe_gateway`'s own docstring."""

    plan: str
    success_url: Annotated[str, Field(min_length=1, max_length=2000)]
    cancel_url: Annotated[str, Field(min_length=1, max_length=2000)]

    @field_validator("plan")
    @classmethod
    def _plan_is_purchasable(cls, value: str) -> str:
        if value not in PURCHASABLE_PLANS:
            raise ValueError(
                f"unknown purchasable plan {value!r} — must be one of {list(PURCHASABLE_PLANS)}"
            )
        return value


class CheckoutSessionResponse(BaseModel):
    checkout_url: str


class BillingPortalRequest(BaseModel):
    return_url: Annotated[str, Field(min_length=1, max_length=2000)] | None = None


class BillingPortalResponse(BaseModel):
    portal_url: str


class UpdateOrganizationRequest(BaseModel):
    """`PATCH /organization`. Every field is optional and, via
    `exclude_unset`, only the fields actually present in the request body
    reach `arie.organizations.update_organization` — a field simply omitted
    is left untouched, while `company_domain` sent as explicit `null` clears
    it (the only nullable field here). At least one field must be present;
    an empty body is rejected rather than silently accepted as a no-op PATCH,
    which is almost always a client mistake worth surfacing.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    timezone: str | None = None
    company_domain: Annotated[str | None, Field(max_length=253)] = None

    @field_validator("timezone")
    @classmethod
    def _timezone_is_known(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                validate_timezone(value)
            except InvalidOrganizationSettingsError as exc:
                raise ValueError(str(exc)) from exc
        return value

    @field_validator("company_domain")
    @classmethod
    def _domain_is_normalizable(cls, value: str | None) -> str | None:
        if value is not None:
            normalize_domain(value)
        return value

    @model_validator(mode="after")
    def _at_least_one_field(self) -> UpdateOrganizationRequest:
        if not self.model_fields_set:
            raise ValueError("at least one field must be provided")
        return self


class UpdateExecutionModeRequest(BaseModel):
    """`PATCH /organization/execution-mode` (Productization M5 Part 14).

    Deliberately its own request model, and its own route, rather than one
    more optional field on `UpdateOrganizationRequest` — this setting gates
    real provider spend and real evidence acquisition, a materially
    different class of consequence than a display name or timezone, and it
    gets its own audit event (`arie.organizations.set_execution_mode`) the
    generic organization-settings path doesn't produce.
    """

    execution_mode: str

    @field_validator("execution_mode")
    @classmethod
    def _execution_mode_is_known(cls, value: str) -> str:
        if value not in EXECUTION_MODES:
            raise ValueError(
                f"unknown execution_mode {value!r} — must be one of {list(EXECUTION_MODES)}"
            )
        return value


# ------------------------------------------------------- members / invitations --
#
# Productization M4 Part 2. Member/invitation *reads* (`GET /organization
# /members`, `GET /organization/invitations`) and every write below are all
# owner/admin-only (`_require_org_admin`) — unlike ICP configuration,
# membership and pending-invitation email addresses are an organization-
# management concern, the same permission tier as API keys, not something
# every active member should see or change by default.


class MemberResponse(BaseModel):
    """Mirrors `arie.members.MemberRecord` field for field."""

    model_config = ConfigDict(from_attributes=True)

    organization_id: UUID
    user_id: UUID
    role: str
    status: str
    created_at: datetime
    updated_at: datetime


class UpdateMemberRoleRequest(BaseModel):
    """`PATCH /organization/members/{user_id}`."""

    role: str

    @field_validator("role")
    @classmethod
    def _role_is_known(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"unknown role {value!r} — must be one of {list(ROLES)}")
        return value


class CreateInvitationRequest(BaseModel):
    """`POST /organization/invitations`."""

    email: Annotated[str, Field(min_length=3, max_length=320)]
    role: str

    @field_validator("email")
    @classmethod
    def _email_is_normalizable(cls, value: str) -> str:
        normalize_email(value)
        return value

    @field_validator("role")
    @classmethod
    def _role_is_known(cls, value: str) -> str:
        if value not in ROLES:
            raise ValueError(f"unknown role {value!r} — must be one of {list(ROLES)}")
        return value


class InvitationResponse(BaseModel):
    """Mirrors `arie.invitations.InvitationRecord` field for field. Never
    the raw token — see `InvitationCreatedResponse`."""

    model_config = ConfigDict(from_attributes=True)

    invitation_id: UUID
    organization_id: UUID
    email_normalized: str
    role: str
    status: str
    invited_by_user_id: UUID
    created_at: datetime
    expires_at: datetime
    accepted_at: datetime | None
    revoked_at: datetime | None
    email_status: str
    """Productization M6 Part 14 — `pending`/`sent`/`failed`, the delivery
    status of the invitation email itself. Independent of `status`: an
    invitation with `email_status == 'failed'` is still fully acceptable via
    its accept URL."""
    email_error: str | None
    email_sent_at: datetime | None


class InvitationCreatedResponse(InvitationResponse):
    """`POST /organization/invitations`'s response — the one and only place
    the raw token is ever shown, mirroring `ApiKeyCreatedResponse`. Not
    retrievable afterward by any endpoint; losing it means revoking the
    invitation and creating a new one."""

    raw_token: str


class AcceptInvitationRequest(BaseModel):
    """`POST /invitations/accept`. The token in the request body, not the
    URL — an invitation token is a bearer secret, and a URL path segment
    routinely ends up in server access logs and browser history in a way a
    POST body does not."""

    token: Annotated[str, Field(min_length=1)]


# ------------------------------------------------------------- provider configs --
#
# Productization M4 Parts 3-5. Reads open to any active member
# (`_require_jwt_session`) — the response shape below never carries a
# secret, only metadata a member is safe to see; writes (save/replace
# credential, enable/disable, remove, test) are owner/admin-only
# (`_require_org_admin`), same tier as ICP configuration writes.


class ProviderStatusResponse(BaseModel):
    """Mirrors `arie.provider_configs.ProviderStatus` field for field.
    Never a secret, never an encrypted value — see that class's own
    docstring for why every supported provider gets an entry regardless of
    whether it has been configured."""

    model_config = ConfigDict(from_attributes=True)

    provider: str
    configured: bool
    enabled: bool
    updated_at: datetime | None
    last_tested_at: datetime | None
    last_test_status: str | None
    last_test_error: str | None


class SetProviderCredentialRequest(BaseModel):
    """`PUT /organization/providers/{provider}` — save (or replace) a
    credential. Deliberately not named `api_key`: not every provider's
    credential is literally an API key in shape, and this field is the same
    for all three today."""

    credential: Annotated[str, Field(min_length=1, max_length=4000)]


class SetProviderEnabledRequest(BaseModel):
    """`PATCH /organization/providers/{provider}` — toggle without touching
    the stored credential."""

    enabled: bool


# ------------------------------------------------------------------- onboarding --
#
# Productization M4 Part 8. Read-gated identically to ICP configuration and
# batches (`_require_jwt_session`) — any active member may see setup
# progress.


class OnboardingStatusResponse(BaseModel):
    """Mirrors `arie.onboarding.OnboardingStatus` field for field."""

    model_config = ConfigDict(from_attributes=True)

    account_created: bool
    organization_configured: bool
    icp_configured: bool
    provider_configured: bool
    first_upload_completed: bool
    first_batch_processed: bool
    completed: bool
    completed_at: datetime | None


# ----------------------------------------------------------------------- limits --
#
# Productization M4 Part 9. Read-gated identically to onboarding/ICP —
# `_require_jwt_session`. No write endpoint yet: the M4 brief asks for
# visibility and server-side enforcement, not a self-service limit-editing
# UI (limits are a sensible-default ceiling, not a per-org config choice a
# member has any use for changing themselves in this milestone).


class UsageAgainstLimitsResponse(BaseModel):
    """`arie.limits.UsageAgainstLimits` plus plan/member context
    (Productization M6 Part 23) — built explicitly in the route handler
    (`GET /organization/limits`) from that dataclass and a freshly resolved
    `arie.billing.plans.EffectiveEntitlements`, rather than
    `model_validate`d off either alone, since no single object carries both.
    """

    leads_used: int
    leads_limit: int
    leads_remaining: int
    modeled_spend_used_usd: float
    modeled_spend_limit_usd: float
    modeled_spend_remaining_usd: float
    max_csv_rows_per_upload: int
    period_start: datetime
    period_end: datetime
    plan: str
    members_used: int
    members_limit: int


# --------------------------------------------------------------- CSV batches --
#
# Productization M3. `arie.batches.BatchProgress` is always computed fresh
# from `leads`/`provider_calls`/`model_calls` — never a stored counter — so
# there is no risk of this response shape reporting stale progress; see that
# module's docstring. `provider_cost_usd`/`model_cost_usd`/`total_cost_usd`
# are unlabelled numbers here exactly like `LeadCostResponse` above — the
# "modelled cost, not billed spend" caveat is UI copy the frontend already
# owns (`providerMode.ts`'s `costCaveat()`), not a second copy restated here.


class BatchProgressResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    total_rows: int
    accepted_rows: int
    rejected_rows: int
    processing_count: int
    qualified_count: int
    rejected_lead_count: int
    review_count: int
    failed_count: int
    provider_cost_usd: float
    model_cost_usd: float
    total_cost_usd: float
    is_complete: bool


class BatchResponse(BaseModel):
    """One CSV upload. Mirrors `arie.batches.BatchRecord` plus a nested,
    live-computed `progress` — see `arie.api.main._to_batch_response`, which
    builds this from two separate service calls (the same composite-response
    shape `ApiKeyCreatedResponse` already uses for the same reason)."""

    model_config = ConfigDict(from_attributes=True)

    batch_id: UUID
    organization_id: UUID
    filename: str
    total_rows: int
    accepted_rows: int
    rejected_rows: int
    created_by_user_id: UUID
    created_at: datetime
    progress: BatchProgressResponse


class BatchRowResponse(BaseModel):
    """One uploaded CSV row. Mirrors `arie.batches.BatchRowRecord`."""

    model_config = ConfigDict(from_attributes=True)

    batch_id: UUID
    row_number: int
    raw_row: dict[str, Any]
    validation_status: str
    validation_error: str | None
    lead_id: UUID | None
    lead_status: str | None

    # M7 Slice 4 — the customer-facing projection for the batch results list
    # (Part K). `None` wherever the row has no lead yet, for the identical
    # reason `lead_status` is `None`.
    priority: CustomerPriority | None = None
    next_action: NextAction | None = None
    short_reason: str | None = None
    confidence_band: ConfidenceBand | None = None


class BatchRowsPageResponse(BaseModel):
    """`GET /batches/{batch_id}/leads` — the first paginated listing endpoint
    in this API (see that route's own comment for why offset/limit, not a
    cursor)."""

    items: list[BatchRowResponse]
    limit: int
    offset: int
    total: int


class UsageSummaryResponse(BaseModel):
    """`GET /usage` — mirrors `arie.usage.UsageSummary` field for field. Cost
    fields are unlabelled numbers, exactly like `LeadCostResponse` and
    `BatchProgressResponse` above; see `arie.usage`'s own module docstring
    for why no "modelled cost" caption is added here."""

    model_config = ConfigDict(from_attributes=True)

    from_at: datetime
    to_at: datetime
    leads_processed: int
    qualified_count: int
    rejected_count: int
    review_count: int
    pending_count: int
    failed_count: int
    provider_calls: int
    cache_hits: int
    provider_cost_usd: float
    model_cost_usd: float
    total_cost_usd: float


# ------------------------------------------------------ intelligence: targeting --
#
# M7 Slice 2. `POST /intelligence/targeting/draft` interprets two free-text
# answers into a reviewable profile and changes nothing; `POST /intelligence/
# targeting/confirm` turns a reviewed draft into a new immutable ICP profile
# version. Both require an owner/admin JWT session — the same rule as `POST
# /organization/icp`, because confirming is that operation, and generating a
# draft spends the organization's AI budget. `GET /intelligence/targeting/
# vocabularies` is read-gated like every other configuration read.
#
# Note what is NOT in the confirm request: a scoring configuration. The client
# receives one in the draft response so it can show the customer what confirming
# will do, and the server recomputes it from the reviewed profile rather than
# accepting it back. See `arie.intelligence.targeting`'s module docstring.


class TargetingDraftRequest(BaseModel):
    """`POST /intelligence/targeting/draft` — the two questions, plus an objective.

    Both answers are untrusted business data and are fenced as such before they
    reach a model (`arie.llm.structured`). The length caps here are the API's
    own; `arie.intelligence.targeting.MAX_DESCRIPTION_CHARS` truncates again on
    the way into the prompt, so neither layer relies on the other.
    """

    what_you_sell: Annotated[str, Field(min_length=1, max_length=4000)]
    who_you_want: Annotated[str, Field(min_length=1, max_length=4000)]
    objective: TargetingObjective = TargetingObjective.BEST_PROSPECTS


class ScoringDimensionSummary(BaseModel):
    """One row of the plain-language reading of a generated configuration."""

    dimension: str
    label: str
    points: float
    rank: int


class TargetingDraftResponse(BaseModel):
    """A generated interpretation and what confirming it would do.

    `scoring_config` is the full technical document, for an Advanced Details
    panel; `allocation` is the same information a customer can read. Both are
    previews — nothing has changed on the server, and this response is not
    persisted anywhere.
    """

    objective: TargetingObjective
    profile: BusinessProfileDraft
    scoring_config: dict[str, Any]
    allocation: list[ScoringDimensionSummary]
    llm_provider: str | None
    llm_model: str | None
    llm_cost_usd: str
    """Modelled cost of the generation, as a string. Never a billed figure —
    see `arie.ledger.pricing`."""


class TargetingConfirmRequest(BaseModel):
    """`POST /intelligence/targeting/confirm` — make a reviewed draft active.

    Carries the profile the customer actually looked at, including any edits
    they made. Everything numeric is re-derived server-side.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)]
    objective: TargetingObjective
    profile: BusinessProfileDraft
    llm_provider: Annotated[str | None, Field(max_length=64)] = None
    llm_model: Annotated[str | None, Field(max_length=128)] = None
    """Echoed back from the draft response purely so the stored provenance can
    say which model's interpretation a human approved. Recorded, never trusted:
    no branch reads either value, and a client that lies about them changes
    nothing except its own audit trail."""


class TargetingVocabulariesResponse(BaseModel):
    """`GET /intelligence/targeting/vocabularies` — the value lists a review UI
    must offer when a human edits a draft.

    Served rather than duplicated in the console so a frontend build cannot
    offer a value the schema rejects.
    """

    industries: list[str]
    seniorities: list[str]
    functions: list[str]
    objectives: list[str]
    preference_levels: list[str]
    scoring_dimensions: list[str]


# ------------------------------------------------------- intelligence: csv --
#
# M7 Slice 3. `POST /batches/mapping-preview` reads an uploaded file's columns
# and says what ARIE thinks they are; `POST /batches` gains an optional
# `mapping` field carrying a customer's confirmed answer. Both are gated the
# same way `POST /batches` already is (`_require_jwt_session`) — mapping a file
# is part of uploading it, not a configuration change.
#
# The preview is not persisted and nothing is ingested by it. It is a POST
# because it carries a file.


class MappedColumnResponse(BaseModel):
    """One source column and what ARIE thinks it holds.

    `label` is what a customer is shown; `canonical_field` is the identifier a
    correction dropdown posts back. Both are present because the console needs
    the first and the API needs the second — never show the second.
    """

    source_column: str
    canonical_field: str | None
    label: str | None
    confidence: str
    reason: str
    requires_confirmation: bool
    candidates: list[str] = Field(default_factory=list)


class CanonicalFieldResponse(BaseModel):
    """One target a column may be mapped onto, for a correction dropdown."""

    name: str
    label: str
    description: str
    required: bool


class MappingPreviewResponse(BaseModel):
    """`POST /batches/mapping-preview` — what ARIE understood, before uploading.

    `requires_confirmation` is the only field a simple client needs to branch
    on: false means the columns can be uploaded as-is, true means show the
    review screen. `field_map` is echoed so a client that changes nothing can
    post it straight back.
    """

    columns: list[MappedColumnResponse]
    field_map: dict[str, str]
    ignored_columns: list[str]
    conflicts: list[str]
    warnings: list[str]
    requires_confirmation: bool
    usable: bool
    """False when no column resolved to an email address. Ingestion requires
    one, so this tells a client not to offer Continue at all."""
    mapping_method: str
    available_fields: list[CanonicalFieldResponse]
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_cost_usd: str = "0"
    llm_unavailable_reason: str | None = None


# -------------------------------------------------- intelligence: outcomes --
#
# M7 Slice 3, Part B/C. Analysing a historical-outcomes CSV writes nothing
# except, optionally, one proposal row — and a proposal changes no scoring
# until somebody accepts it. Every write-shaped route here requires an
# owner/admin JWT session, matching targeting: a suggestion about targeting is
# a targeting matter. Reading proposals is open to any member.


class OutcomeGroupResponse(BaseModel):
    """One group's historical outcomes, against the customer's own baseline.

    Every number here was computed deterministically by
    `arie.intelligence.outcomes` — no model produced or could change one.
    `sentence` is ARIE's own associational phrasing, safe to render as-is.
    """

    dimension: str
    group_key: str
    group_label: str
    sample_size: int
    positive_count: int
    negative_count: int
    positive_rate: float
    baseline_rate: float
    rate_difference: float
    signal: str
    sentence: str


class OutcomeAnalysisResponse(BaseModel):
    """`POST /intelligence/outcomes/analyze` — what the customer's past results say.

    `proposal_id` is present only when the statistics supported a suggestion
    *and* one was stored. Its absence is a normal, honest outcome: most small
    datasets say nothing actionable.
    """

    total_rows: int
    labelled_rows: int
    positive_count: int
    negative_count: int
    baseline_rate: float
    groups: list[OutcomeGroupResponse]
    unrecognised_labels: dict[str, int]
    warnings: list[str]
    revenue_total_usd: str | None = None
    interpretation: str | None = None
    """A model's prose about the aggregates, when one was available. `None`
    means the statistics stand alone — which they do."""
    caveats: list[str] = Field(default_factory=list)
    proposal_id: UUID | None = None
    llm_provider: str | None = None
    llm_model: str | None = None
    llm_cost_usd: str = "0"


class ProposedChangeResponse(BaseModel):
    """One concrete edit a proposal suggests, with its before and after."""

    kind: str
    dimension: str
    target: str
    target_label: str
    from_value: str
    to_value: str
    rationale: str


class ProposalResponse(BaseModel):
    """One revision proposal. Mirrors `arie.intelligence.proposals.ProposalRecord`.

    `status` is the whole point: a `proposed` row has changed nothing, and a
    console must present it as a suggestion rather than as something that has
    happened.
    """

    proposal_id: UUID
    organization_id: UUID
    profile_id: UUID
    profile_version: int
    source: str
    status: str
    summary: str
    changes: list[ProposedChangeResponse]
    observations: list[str]
    caveats: list[str]
    supporting_statistics: dict[str, Any]
    evidence_strength: str
    sample_size: int
    created_at: datetime
    resolved_at: datetime | None = None
    resulting_profile_id: UUID | None = None


class AcceptProposalRequest(BaseModel):
    """`POST /intelligence/proposals/{id}/accept` — apply a suggestion.

    Carries only a name for the profile version this creates. The changes
    themselves come from the stored proposal, never from the request: a client
    that could post its own changes would be posting a targeting profile, which
    is what the targeting endpoints are for.
    """

    name: Annotated[str, Field(min_length=1, max_length=200)] = "Updated from past results"


# ------------------------------------------------------------------ copilot --
#
# M7 Slice 6 — "Ask ARIE". Read-only: neither route below ever mutates a
# profile, submits feedback, executes research, or calls a provider. See
# `arie.copilot_service`'s own module docstring for the query-plan/LLM
# architecture behind these two thin request/response shapes.


class CopilotQueryRequest(BaseModel):
    """`POST /copilot/query`. `question` is the only client-supplied input —
    everything else (intent, filters, the leads actually returned) is
    resolved server-side from the caller's own authenticated organization."""

    question: Annotated[str, Field(min_length=1, max_length=500)]


class CopilotLeadReferenceResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lead_id: UUID
    company: str | None
    contact: str | None
    priority: CustomerPriority
    score: float | None
    why: str
    next_action: NextAction


class CopilotResponseSchema(BaseModel):
    """`POST /copilot/query`'s response. `filters_applied` echoes back the
    (already-validated, already-clamped) query plan's non-default fields —
    Advanced Details material, not something a client should parse to decide
    what to render; render `leads` and `answer`."""

    answer: str
    leads: list[CopilotLeadReferenceResponse]
    intent: CopilotIntent
    result_count: int
    filters_applied: dict[str, Any]
    llm_used: bool

    @classmethod
    def from_result(cls, result: CopilotResponse) -> CopilotResponseSchema:
        return cls(
            answer=result.answer,
            leads=[CopilotLeadReferenceResponse.model_validate(lead) for lead in result.leads],
            intent=result.intent,
            result_count=result.result_count,
            filters_applied=result.filters_applied,
            llm_used=result.llm_used,
        )


class LeadCopilotRequest(BaseModel):
    """`POST /leads/{lead_id}/copilot`."""

    question: Annotated[str, Field(min_length=1, max_length=500)]


class LeadCopilotResponseSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    lead_id: UUID
    intent: CopilotIntent
    answer: str
    missing_information: list[str]
    researchable_field: ResearchTargetField | None

    @classmethod
    def from_result(cls, result: LeadCopilotResponse) -> LeadCopilotResponseSchema:
        return cls.model_validate(result)
