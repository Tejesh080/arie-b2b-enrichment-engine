"""Domain types for the discovery loop: what a search plan looks like, what a
raw search result becomes, and what a customer ultimately sees.

Two kinds of type live here. The LLM-facing ones (`SearchQueryItem`,
`DiscoverySearchPlan`, `CandidateScreeningItem`, `CandidateScreeningBatch`)
follow the same discipline as `arie.intelligence.schemas.BusinessProfileDraft`
— `extra="forbid"`, capped lengths, no field a model could use to select a
provider, spend money, or claim a fact ARIE didn't independently verify. The
rest are plain persistence/projection dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "MAX_SCREENING_BATCH",
    "MAX_SEARCH_QUERIES",
    "BuyerSignal",
    "CandidateScreeningBatch",
    "CandidateScreeningItem",
    "DiscoveryCandidate",
    "DiscoveryFunnel",
    "DiscoveryRun",
    "DiscoveryRunStatus",
    "DiscoverySearchPlan",
    "Opportunity",
    "RawDiscoveryCandidate",
    "ScreeningClass",
    "SearchQueryItem",
]

MAX_SEARCH_QUERIES = 8
MAX_SCREENING_BATCH = 20


class DiscoveryRunStatus(StrEnum):
    DRAFT = "draft"
    PLANNING = "planning"
    DISCOVERING = "discovering"
    SCREENING = "screening"
    PROMOTING = "promoting"
    RESEARCHING = "researching"
    COMPLETE = "complete"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ScreeningClass(StrEnum):
    """A discovery candidate's cheap first-pass classification. Never the
    final ARIE priority — see `Opportunity.priority` for that, which only
    exists once the existing scorer has run on a promoted lead."""

    PROMISING = "promising"
    POSSIBLE = "possible"
    UNLIKELY = "unlikely"
    INSUFFICIENT_INFO = "insufficient_info"


# --------------------------------------------------------------------- LLM --


class SearchQueryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(max_length=120)
    rationale: str = Field(max_length=200)


class DiscoverySearchPlan(BaseModel):
    """What the model may say: search intent, nothing else. No field names a
    provider, a URL, or a budget — `arie.discovery.search_planning` decides
    all of that deterministically from this plan's `queries` alone."""

    model_config = ConfigDict(extra="forbid")

    queries: list[SearchQueryItem] = Field(min_length=1, max_length=MAX_SEARCH_QUERIES)


class CandidateScreeningItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(max_length=64)
    screening_class: Literal["promising", "possible", "unlikely", "insufficient_info"]
    short_reason: str = Field(max_length=160)
    matching_traits: list[str] = Field(default_factory=list, max_length=3)


class CandidateScreeningBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    results: list[CandidateScreeningItem] = Field(max_length=MAX_SCREENING_BATCH)


# ------------------------------------------------------------- persistence --


@dataclass(frozen=True)
class RawDiscoveryCandidate:
    """One row a `DiscoveryProvider.search` call returned — provenance
    intact, nothing normalised or judged yet."""

    company_name: str
    url: str
    snippet: str
    source_provider: str
    search_query: str


@dataclass(frozen=True)
class DiscoveryCandidate:
    """A raw result after canonicalisation, carried through screening and
    (for a survivor) promotion. `domain` is the identity key — see
    `arie.discovery.dedupe`."""

    candidate_id: UUID
    run_id: UUID
    organization_id: UUID
    company_name: str
    domain: str
    source_url: str
    snippet: str
    source_provider: str
    search_query: str
    screening_class: ScreeningClass | None = None
    screening_reason: str | None = None
    promoted_lead_id: UUID | None = None
    created_at: datetime | None = None


@dataclass(frozen=True)
class DiscoveryFunnel:
    """The number ARIE's own accounting exists to answer: how did N raw
    results become the shortlist a customer sees. Every field here is a count
    or a modelled cost read back from the same ledgers every other feature
    already writes to — never invented."""

    search_queries: int = 0
    raw_candidates: int = 0
    unique_companies: int = 0
    screened: int = 0
    promising: int = 0
    possible: int = 0
    unlikely: int = 0
    insufficient_info: int = 0
    promoted_to_leads: int = 0
    research_candidates: int = 0
    research_calls: int = 0
    buyer_lookups: int = 0
    final_opportunities: int = 0
    llm_calls: int = 0
    llm_cost_usd: Decimal = Decimal(0)
    provider_calls: int = 0
    provider_cost_usd: Decimal = Decimal(0)

    def as_dict(self) -> dict[str, object]:
        return {
            "search_queries": self.search_queries,
            "raw_candidates": self.raw_candidates,
            "unique_companies": self.unique_companies,
            "screened": self.screened,
            "promising": self.promising,
            "possible": self.possible,
            "unlikely": self.unlikely,
            "insufficient_info": self.insufficient_info,
            "promoted_to_leads": self.promoted_to_leads,
            "research_candidates": self.research_candidates,
            "research_calls": self.research_calls,
            "buyer_lookups": self.buyer_lookups,
            "final_opportunities": self.final_opportunities,
            "llm_calls": self.llm_calls,
            "llm_cost_usd": str(self.llm_cost_usd),
            "provider_calls": self.provider_calls,
            "provider_cost_usd": str(self.provider_cost_usd),
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DiscoveryFunnel:
        """`raw` is whatever `discovery_runs.funnel` JSONB deserialized to —
        untyped by construction, hence `Any` here rather than propagating
        that uncertainty into every field below."""
        if not raw:
            return cls()

        def _int(key: str) -> int:
            return int(raw.get(key, 0))

        return cls(
            search_queries=_int("search_queries"),
            raw_candidates=_int("raw_candidates"),
            unique_companies=_int("unique_companies"),
            screened=_int("screened"),
            promising=_int("promising"),
            possible=_int("possible"),
            unlikely=_int("unlikely"),
            insufficient_info=_int("insufficient_info"),
            promoted_to_leads=_int("promoted_to_leads"),
            research_candidates=_int("research_candidates"),
            research_calls=_int("research_calls"),
            buyer_lookups=_int("buyer_lookups"),
            final_opportunities=_int("final_opportunities"),
            llm_calls=_int("llm_calls"),
            llm_cost_usd=Decimal(str(raw.get("llm_cost_usd", "0"))),
            provider_calls=_int("provider_calls"),
            provider_cost_usd=Decimal(str(raw.get("provider_cost_usd", "0"))),
        )


@dataclass(frozen=True)
class DiscoveryRun:
    run_id: UUID
    organization_id: UUID
    profile_version: int | None
    status: DiscoveryRunStatus
    requested_opportunity_count: int
    market: str | None
    max_candidates: int
    created_by_user_id: UUID | None
    error_detail: str | None
    funnel: DiscoveryFunnel
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


# --------------------------------------------------------------- customer --


@dataclass(frozen=True)
class BuyerSignal:
    """What ARIE learned about who to contact — never a fabricated name.
    `name_known` is `False` in every path this pivot ships today (no live
    person-search provider is enabled this session); the field exists so a
    later slice that wires a real Hunter/Apollo person search has somewhere
    honest to put one, rather than this shape needing to change shape then."""

    seniority: str | None
    function: str | None
    name_known: bool
    source: str | None
    confidence: float | None


@dataclass(frozen=True)
class Opportunity:
    """The customer-facing payoff: one promoted, scored discovery candidate,
    projected through the exact same `arie.recommendations` machinery every
    other lead in the product already uses. Nothing about scoring, priority,
    or next-action is computed twice — see `arie.discovery.opportunity`."""

    candidate_id: UUID
    lead_id: UUID
    company_name: str
    domain: str
    priority: str
    next_action: str
    score: float | None
    confidence: float | None
    short_reason: str
    key_evidence: list[str]
    missing_information: list[str]
    buyer: BuyerSignal | None
    research_performed: bool
    discovery_source: str
    source_url: str
    search_query: str = ""
