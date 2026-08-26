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
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from arie.api.ingest import LeadIngestCommand
from arie.api.receipt import DecisionReceipt
from arie.approval.workflow import ReviewAction
from arie.core.types import LeadStatus
from arie.identity.normalize import normalize_domain, normalize_email


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

    def to_command(self) -> LeadIngestCommand:
        return LeadIngestCommand(
            source=self.source,
            email=self.email,
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
