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
from typing import Annotated
from uuid import UUID

from pydantic import BaseModel, Field, field_validator, model_validator

from arie.api.ingest import LeadIngestCommand
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
    created_at: datetime
    updated_at: datetime
    cost: LeadCostResponse


class HealthResponse(BaseModel):
    status: str
    database: bool


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
