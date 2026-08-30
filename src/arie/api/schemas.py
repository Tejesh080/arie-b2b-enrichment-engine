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
from arie.core.types import LeadStatus
from arie.icp_profiles import InvalidICPConfigError, validate_config
from arie.identity.normalize import normalize_domain, normalize_email
from arie.organizations import InvalidOrganizationSettingsError, validate_timezone


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


class ReceiptProvidersResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    called: list[ReceiptProviderCallResponse]
    not_called: list[str]


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
    onboarding_completed_at: datetime | None
    created_at: datetime
    updated_at: datetime


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
