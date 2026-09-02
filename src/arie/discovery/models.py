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
    "MAX_WEBSITE_PAGES",
    "BuyerCandidate",
    "BuyerSignal",
    "CandidateScreeningBatch",
    "CandidateScreeningItem",
    "DiscoveryCandidate",
    "DiscoveryFunnel",
    "DiscoveryRun",
    "DiscoveryRunStatus",
    "DiscoverySearchPlan",
    "DiscoverySuitability",
    "EmailStatus",
    "Opportunity",
    "OpportunityNextAction",
    "RawDiscoveryCandidate",
    "ScreeningClass",
    "SearchQueryItem",
    "VerificationStatus",
    "VerifiedCompanyFacts",
]

MAX_SEARCH_QUERIES = 8
MAX_SCREENING_BATCH = 20
MAX_WEBSITE_PAGES = 2


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


class VerificationStatus(StrEnum):
    """What happened when ARIE tried to check a candidate against its own
    public website — see `arie.discovery.website_verification`. A candidate
    a screening pass liked can still end here as `REJECTED`: cheap screening
    reads a search snippet, this reads the company's own site."""

    VERIFIED = "verified"
    """The site was fetched and its content plausibly supports the company
    description — proceed to promotion."""
    REJECTED = "rejected"
    """The site was fetched and clearly contradicts the fit — e.g. not a
    real business, or obviously the wrong kind of company. Never promoted."""
    UNAVAILABLE = "unavailable"
    """The fetch or extraction failed (timeout, no content, malformed
    response). The candidate still survives — an unreachable website is not
    evidence of a poor fit, the same unknown-vs-negative rule
    `arie.scoring.rules` applies everywhere else in this product."""
    SKIPPED = "skipped"
    """Never attempted — reserved for a candidate that didn't reach this
    stage at all (discarded by cheap screening)."""


class DiscoverySuitability(StrEnum):
    """How much *real public evidence* supports treating a discovered company
    as a prospect — see `arie.discovery.suitability`, which decides it
    deterministically, and `arie.discovery.opportunity`, which applies it as
    a ceiling on the priority the (simulated-in-this-mode) scorer produced.

    Deliberately separate from `VerificationStatus`: that says what happened
    when ARIE tried to fetch a website, this says what the fetched evidence
    means for this particular customer's target."""

    SUPPORTED = "supported"
    """Real website evidence is consistent with the target. The only state
    that may keep a positive priority, and the only state that may spend a
    buyer-lookup call."""
    UNCERTAIN = "uncertain"
    """No usable real evidence either way. Never a rejection — but never a
    confident recommendation either, because everything still arguing for it
    is simulated."""
    CONTRADICTED = "contradicted"
    """Real evidence argues against it: a directory or content platform, the
    wrong geography, or a business selling what the customer sells."""


class EmailStatus(StrEnum):
    """How sure ARIE is that a buyer's email actually reaches them — read
    from the provider's own verification signal, never invented. See
    `arie.discovery.buyer_search`."""

    VERIFIED = "verified"
    LIKELY = "likely"
    UNVERIFIED = "unverified"
    NONE = "none"
    """No email was returned for this person at all."""


class OpportunityNextAction(StrEnum):
    """The discovery-specific next-action vocabulary. Mirrors
    `arie.recommendations.NextAction` wherever that vocabulary already fits
    — `Opportunity.next_action` is typed as plain `str` precisely so this
    projection can add the one state that vocabulary has no room for
    (`VERIFY_CONTACT_METHOD`: a real, named buyer with no usable email)
    without touching the core enum every other lead in the product reads."""

    CONTACT_NOW = "contact_now"
    EMAIL_FIRST = "email_first"
    FIND_DECISION_MAKER = "find_decision_maker"
    VERIFY_CONTACT_METHOD = "verify_contact_method"
    RESEARCH_MORE = "research_more"
    NURTURE = "nurture"
    SKIP = "skip"
    HUMAN_REVIEW = "human_review"


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


class VerifiedCompanyFacts(BaseModel):
    """What the model may say about a company's own website — nothing it
    could use to score, rank, or act. See
    `arie.discovery.website_verification`. Every field is a *reading* of the
    fetched page text, never an inference the page didn't support: a model
    that wants to report an employee count the page never stated has nowhere
    to put it — `employee_size_clue` is a string ("a small team", "50+
    people"), not an int, precisely so a plausible-looking fabricated number
    can't slip through as if it were counted."""

    model_config = ConfigDict(extra="forbid")

    business_relevance: Literal[
        "clearly_relevant", "plausible", "clearly_irrelevant", "insufficient_content"
    ]
    """The one field that can downgrade a screening verdict — see
    `arie.discovery.website_verification.verify_candidate`. `insufficient_
    content` is a correct, honest answer when the page said too little to
    judge either way, not a failure."""
    company_name_on_site: str | None = Field(default=None, max_length=80)
    """The name the site calls *itself* (masthead, logo alt text, "About
    <name>", copyright line) — Discovery Quality Fix 1's "verified site
    identity" step. `None` whenever the pages never state one; a page title
    is not an answer, and `arie.discovery.company_identity` rejects one that
    reads like a headline anyway."""
    business_description: str = Field(max_length=400)
    industry_category: str = Field(max_length=100)
    customer_type: Literal["b2b", "b2c", "both", "unclear"]
    multi_location_signal: bool | None = None
    operational_complexity_signal: bool | None = None
    employee_size_clue: str | None = Field(default=None, max_length=100)
    products_services: list[str] = Field(default_factory=list, max_length=8)
    geography_clue: str | None = Field(default=None, max_length=100)
    reasoning: str = Field(max_length=300)
    """One sentence citing what the page actually said — the human-readable
    justification for `business_relevance`, shown to a customer who asks why
    a company was rejected after looking promising in search results."""


# ------------------------------------------------------------- persistence --


@dataclass(frozen=True)
class RawDiscoveryCandidate:
    """One row a `DiscoveryProvider.search` call returned — provenance
    intact, nothing normalised or judged yet."""

    company_name: str
    """Already resolved by `arie.discovery.company_identity` — never the raw
    page title the search API returned (Discovery Quality Fix 1)."""
    url: str
    snippet: str
    source_provider: str
    search_query: str
    site_name: str | None = None
    """Whatever the provider reported as the *site's* own name (`og:site_name`
    and friends), kept separately from `company_name` so the resolution chain
    stays inspectable."""


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
    verification_status: VerificationStatus | None = None
    verified_facts: dict[str, Any] | None = None
    """`VerifiedCompanyFacts.model_dump()` for a `VERIFIED`/`REJECTED`
    candidate, else `None`. Stored as JSON rather than re-typed here because
    it is display-only provenance by the time it is read back — nothing
    downstream re-validates it against the schema."""
    website_verified_at: datetime | None = None
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
    excluded_non_business: int = 0
    """Deduped domains dropped before they cost anything — directories,
    aggregators and social/content platforms (`arie.discovery.suitability.
    is_non_business_domain`). Counted rather than silently discarded, so a
    customer can see search volume that never became prospects."""
    screened: int = 0
    promising: int = 0
    possible: int = 0
    unlikely: int = 0
    insufficient_info: int = 0
    website_verified: int = 0
    """Candidates whose own site was actually fetched and read — a subset of
    `promising + possible`, never of `raw_candidates`; see Part 17's cost
    control."""
    company_rejected_after_verification: int = 0
    """Screening liked these; the company's own website didn't. Proof that
    verification is a real filter, not a rubber stamp."""
    promoted_to_leads: int = 0
    research_candidates: int = 0
    research_calls: int = 0
    buyer_lookup_eligible: int = 0
    """Promoted leads that cleared the buyer-search gate (a positive-enough
    recommendation, no already-known buyer) — the denominator
    `buyer_lookups` should stay close to, and always at or under."""
    buyer_lookups: int = 0
    buyer_found: int = 0
    buyer_email_found: int = 0
    final_opportunities: int = 0
    final_contactable_opportunities: int = 0
    """The number that actually matters — see Part 16. A subset of
    `final_opportunities`: a named, ranked buyer AND a usable (verified or
    likely) email. Never equal to `final_opportunities` by construction
    unless every single opportunity cleared that bar."""
    llm_calls: int = 0
    llm_cost_usd: Decimal = Decimal(0)
    provider_calls: int = 0
    provider_cost_usd: Decimal = Decimal(0)
    website_calls: int = 0
    website_cost_usd: Decimal = Decimal(0)

    def as_dict(self) -> dict[str, object]:
        return {
            "search_queries": self.search_queries,
            "raw_candidates": self.raw_candidates,
            "unique_companies": self.unique_companies,
            "excluded_non_business": self.excluded_non_business,
            "screened": self.screened,
            "promising": self.promising,
            "possible": self.possible,
            "unlikely": self.unlikely,
            "insufficient_info": self.insufficient_info,
            "website_verified": self.website_verified,
            "company_rejected_after_verification": self.company_rejected_after_verification,
            "promoted_to_leads": self.promoted_to_leads,
            "research_candidates": self.research_candidates,
            "research_calls": self.research_calls,
            "buyer_lookup_eligible": self.buyer_lookup_eligible,
            "buyer_lookups": self.buyer_lookups,
            "buyer_found": self.buyer_found,
            "buyer_email_found": self.buyer_email_found,
            "final_opportunities": self.final_opportunities,
            "final_contactable_opportunities": self.final_contactable_opportunities,
            "llm_calls": self.llm_calls,
            "llm_cost_usd": str(self.llm_cost_usd),
            "provider_calls": self.provider_calls,
            "provider_cost_usd": str(self.provider_cost_usd),
            "website_calls": self.website_calls,
            "website_cost_usd": str(self.website_cost_usd),
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
            excluded_non_business=_int("excluded_non_business"),
            screened=_int("screened"),
            promising=_int("promising"),
            possible=_int("possible"),
            unlikely=_int("unlikely"),
            insufficient_info=_int("insufficient_info"),
            website_verified=_int("website_verified"),
            company_rejected_after_verification=_int("company_rejected_after_verification"),
            promoted_to_leads=_int("promoted_to_leads"),
            research_candidates=_int("research_candidates"),
            research_calls=_int("research_calls"),
            buyer_lookup_eligible=_int("buyer_lookup_eligible"),
            buyer_lookups=_int("buyer_lookups"),
            buyer_found=_int("buyer_found"),
            buyer_email_found=_int("buyer_email_found"),
            final_opportunities=_int("final_opportunities"),
            final_contactable_opportunities=_int("final_contactable_opportunities"),
            llm_calls=_int("llm_calls"),
            llm_cost_usd=Decimal(str(raw.get("llm_cost_usd", "0"))),
            provider_calls=_int("provider_calls"),
            provider_cost_usd=Decimal(str(raw.get("provider_cost_usd", "0"))),
            website_calls=_int("website_calls"),
            website_cost_usd=Decimal(str(raw.get("website_cost_usd", "0"))),
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
class BuyerCandidate:
    """One real person a `BuyerDiscoveryProvider` returned — see
    `arie.discovery.buyer_search`. Every field is either something the
    provider actually reported or `None`; nothing here is ever synthesised."""

    full_name: str
    title: str | None
    seniority: str | None
    """Canonical (`arie.normalization.taxonomy`), for ranking — not the raw
    provider title."""
    function: str | None
    email: str | None
    email_status: EmailStatus
    profile_url: str | None
    decision_maker: bool | None
    source: str
    confidence: float | None
    """The provider's own reported confidence for this specific match, where
    it reports one (Hunter's `confidence`, 0-100 scaled to 0-1). Not ARIE's
    ranking score."""


@dataclass(frozen=True)
class BuyerSignal:
    """What ARIE learned about who to contact for one opportunity.
    `name_known=False` means exactly what it says — no real person-search
    match, only the existing pipeline's own simulated role signal (still
    honestly labelled `source="simulated"`). `name_known=True` means a real
    `BuyerCandidate` was found and ranked; every field below then reflects
    what that provider actually returned, per `BuyerCandidate`'s own rule."""

    seniority: str | None
    function: str | None
    name_known: bool
    source: str | None
    confidence: float | None
    full_name: str | None = None
    title: str | None = None
    email: str | None = None
    email_status: EmailStatus | None = None
    profile_url: str | None = None

    @classmethod
    def from_candidate(cls, candidate: BuyerCandidate) -> BuyerSignal:
        return cls(
            seniority=candidate.seniority,
            function=candidate.function,
            name_known=True,
            source=candidate.source,
            confidence=candidate.confidence,
            full_name=candidate.full_name,
            title=candidate.title,
            email=candidate.email,
            email_status=candidate.email_status,
            profile_url=candidate.profile_url,
        )


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
    alternate_buyers: list[BuyerSignal]
    research_performed: bool
    discovery_source: str
    source_url: str
    search_query: str = ""
    verification_status: VerificationStatus | None = None
    verified_facts: dict[str, Any] | None = None
    website_verified_at: datetime | None = None
    suitability: DiscoverySuitability = DiscoverySuitability.UNCERTAIN
    """The gate that produced `priority`'s ceiling — see
    `arie.discovery.suitability`. Defaults to `UNCERTAIN` so a caller
    constructing an `Opportunity` without one can never accidentally claim
    real evidence that was never gathered."""
    suitability_reason: str | None = None

    @property
    def is_contactable(self) -> bool:
        """Part 16's real utility metric: a positive opportunity, a named
        buyer, and a usable contact channel — never just "we found a
        company" or "we found *someone* at this company"."""
        return (
            self.priority in ("contact_first", "worth_pursuing")
            and self.buyer is not None
            and self.buyer.name_known
            and self.buyer.email is not None
            and self.buyer.email_status in (EmailStatus.VERIFIED, EmailStatus.LIKELY)
        )
