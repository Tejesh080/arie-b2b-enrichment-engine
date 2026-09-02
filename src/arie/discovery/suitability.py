"""The discovery suitability gate — Discovery Quality Fix 2/4/6/8.

The contactable-opportunity proof exposed a precedence bug, not a scoring
bug. A discovered company's score comes from the ordinary lead pipeline,
which in `simulated` provider mode is fed *simulated* firmographics; its
website evidence comes from a real Firecrawl fetch. When those two disagreed,
the simulated number won: a company ARIE could not verify at all ranked
`contact_first` at 96.8, and companies whose own websites said they *sell*
what the customer sells were ranked as buyers.

This module decides, deterministically and with no model call, how much the
*real public evidence* supports treating a discovered company as a prospect:

``SUPPORTED``
    ARIE fetched the company's own site and what it said is consistent with
    the customer's target — the only state that may keep a positive priority
    and the only state that may spend a buyer-lookup call.
``UNCERTAIN``
    No usable real evidence either way (fetch failed, or the pages said too
    little). Not a rejection — an unread website is not evidence of a poor
    fit — but never a confident recommendation either, because the only thing
    left arguing for it is simulated.
``CONTRADICTED``
    Real evidence argues against it: a directory or content platform rather
    than a company, the wrong geography, or a business whose own site says it
    sells the same thing the customer sells.

Everything here reads `VerifiedCompanyFacts` fields that came from the
company's own pages. Nothing here reads a score, and nothing here can raise a
priority — see `arie.discovery.opportunity`, where this becomes a ceiling.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arie.discovery.models import DiscoverySuitability, VerificationStatus

__all__ = [
    "SuitabilityAssessment",
    "assess_suitability",
    "is_non_business_domain",
    "offering_category_terms",
]


_NON_BUSINESS_DOMAINS = frozenset(
    {
        # Vendor directories / review aggregators — the "GoodFirms problem".
        "goodfirms.co",
        "clutch.co",
        "designrush.com",
        "sortlist.com",
        "g2.com",
        "capterra.com",
        "getapp.com",
        "softwareadvice.com",
        "trustpilot.com",
        "productreview.com.au",
        "yelp.com",
        "yellowpages.com.au",
        "truelocal.com.au",
        "crunchbase.com",
        "owler.com",
        "zoominfo.com",
        "apollo.io",
        "producthunt.com",
        "upwork.com",
        "freelancer.com",
        "fiverr.com",
        "glassdoor.com",
        "indeed.com",
        "seek.com.au",
        # Social / UGC / content platforms — never the company itself.
        "linkedin.com",
        "facebook.com",
        "instagram.com",
        "twitter.com",
        "x.com",
        "tiktok.com",
        "youtube.com",
        "reddit.com",
        "quora.com",
        "medium.com",
        "substack.com",
        "wordpress.com",
        "blogspot.com",
        "wikipedia.org",
        "github.com",
        "vocal.media",
        "pinterest.com",
        "issuu.com",
        "slideshare.net",
        # Site builders and app hosts: `koala-coyote-y4fh.squarespace.com`
        # turned up in a proof run as a candidate called "Squarespace". A
        # page on one of these belongs to *someone*, but the domain names
        # the host, not a company anyone could sell to.
        "squarespace.com",
        "wixsite.com",
        "weebly.com",
        "webflow.io",
        "netlify.app",
        "vercel.app",
        "github.io",
        "godaddysites.com",
    }
)
"""Domains that are never themselves a prospect. Matched on the candidate's
already-canonicalised domain or any parent of it, so `www.goodfirms.co/…`
and `blog.medium.com` both land here."""

_DIRECTORY_FACT_TERMS = (
    "directory",
    "listing site",
    "listings site",
    "review platform",
    "review site",
    "comparison site",
    "aggregator",
    "marketplace for",
    "b2b ratings",
    "ratings and reviews",
    "news outlet",
    "news publisher",
    "magazine",
    "media publication",
    "online publication",
)
"""What a directory or publisher says about *itself*, for the ones not on the
domain list above."""

_OFFERING_CATEGORIES: dict[str, tuple[str, ...]] = {
    "software_development": (
        "software development",
        "custom software",
        "software developer",
        "software house",
        "software agency",
        "software company",
        "software solutions",
        "software platform",
        "business software",
        "enterprise software",
        "app development",
        "application development",
        "web development",
        "web design",
        "development agency",
        "product engineering",
        "mobile app",
        "saas",
        "saas development",
        "erp software",
        "warehouse management system",
        "warehouse management systems",
    ),
    "ai_ml": (
        "ai automation",
        "ai consulting",
        "ai solutions",
        "ai development",
        "ai services",
        "ai agency",
        "artificial intelligence",
        "machine learning",
        "data science",
        "chatbot",
        "generative ai",
        "llm",
    ),
    "it_consulting": (
        "it services",
        "it consulting",
        "it solutions",
        "it support",
        "managed it",
        "digital agency",
        "digital transformation",
        "systems integrator",
        "system integration",
        "systems integration",
        "technology consulting",
        "erp implementation",
        "automation consulting",
        "process automation",
        "workflow automation",
        "rpa",
    ),
}

_SUPPLY_SIDE_FAMILY = frozenset({"software_development", "ai_ml", "it_consulting"})
"""One family for the supplier/competitor test.

Category-for-category matching was too literal: the proof rerun let through a
company whose own site sells "ERP software, warehouse management systems,
wholesale distribution software" because the seller's summary matched
`software_development`/`ai_ml` while the candidate matched `it_consulting`.
Anyone selling software or technology services is on the same side of the
table as a seller of software or technology services, whichever of these
three words they happen to use for it."""
"""Coarse categories of *what a business sells*. Used on both sides of one
comparison — the customer's own offering summary, and the candidate's own
statement of what it offers — so "we build AI automation" versus a candidate
whose site sells "AI automation, custom software development" resolves to a
supplier/competitor rather than a buyer."""

_COUNTRY_ALIASES: dict[str, tuple[str, ...]] = {
    "australia": (
        "australia",
        "australian",
        "sydney",
        "melbourne",
        "brisbane",
        "perth",
        "adelaide",
        "canberra",
        "hobart",
        "darwin",
        "gold coast",
        "newcastle",
        "wollongong",
        "geelong",
        "new south wales",
        "queensland",
        "victoria, australia",
        "western australia",
        "south australia",
        "tasmania",
    ),
    "new zealand": ("new zealand", "auckland", "wellington", "christchurch"),
    "india": (
        "india",
        "indian",
        "noida",
        "gurgaon",
        "gurugram",
        "bangalore",
        "bengaluru",
        "mumbai",
        "new delhi",
        "hyderabad",
        "pune",
        "chennai",
        "ahmedabad",
        "kochi",
        "jaipur",
    ),
    "united states": (
        "united states",
        "u.s.a",
        "usa",
        "new york",
        "san francisco",
        "california",
        "texas",
        "chicago",
        "boston",
        "seattle",
        "atlanta",
        "florida",
    ),
    "united kingdom": (
        "united kingdom",
        "england",
        "london",
        "manchester",
        "birmingham",
        "scotland",
        "wales",
    ),
    "canada": ("canada", "toronto", "vancouver", "montreal"),
    "singapore": ("singapore",),
    "philippines": ("philippines", "manila", "cebu"),
    "united arab emirates": ("united arab emirates", "dubai", "abu dhabi"),
    "ireland": ("ireland", "dublin"),
    "south africa": ("south africa", "johannesburg", "cape town"),
    "germany": ("germany", "berlin", "munich"),
}
"""Only used to answer one narrow question: does the geography the company's
own site states name a *different* country than the market the customer asked
for. Absence of a clue is never a mismatch."""

_WORD_BOUNDARY_CACHE: dict[str, re.Pattern[str]] = {}


def _contains_term(haystack: str, term: str) -> bool:
    """Word-boundary containment, so `rpa` does not match "corpation" and
    `ai automation` still matches "AI automation services"."""
    pattern = _WORD_BOUNDARY_CACHE.get(term)
    if pattern is None:
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])")
        _WORD_BOUNDARY_CACHE[term] = pattern
    return pattern.search(haystack) is not None


def _categories(text: str) -> frozenset[str]:
    lowered = text.lower()
    return frozenset(
        category
        for category, terms in _OFFERING_CATEGORIES.items()
        if any(_contains_term(lowered, term) for term in terms)
    )


def _country_for(text: str) -> str | None:
    lowered = text.lower()
    for country, aliases in _COUNTRY_ALIASES.items():
        if any(_contains_term(lowered, alias) for alias in aliases):
            return country
    return None


def offering_category_terms(text: str) -> tuple[str, ...]:
    """Every vocabulary term belonging to a category `text` matches — the
    seller's own service words, which `arie.discovery.search_planning` uses to
    keep them *out* of search queries (Discovery Quality Fix 5). Searching a
    seller's own vocabulary finds other sellers writing about it."""
    return tuple(
        term for category in sorted(_categories(text)) for term in _OFFERING_CATEGORIES[category]
    )


def is_non_business_domain(domain: str) -> bool:
    """`True` for a directory, aggregator, social network or content platform
    — a page there is *about* companies, or hosted *for* companies, but is
    never itself a company a customer could sell to."""
    host = domain.strip().lower().strip(".")
    if not host:
        return False
    labels = host.split(".")
    return any(
        ".".join(labels[index:]) in _NON_BUSINESS_DOMAINS for index in range(len(labels) - 1)
    )


@dataclass(frozen=True)
class SuitabilityAssessment:
    suitability: DiscoverySuitability
    reason: str
    """One customer-safe sentence citing the evidence, shown alongside a
    downgraded opportunity so "why is this only `review`" has an answer."""


def _string_field(facts: Mapping[str, Any], key: str) -> str:
    value = facts.get(key)
    return value if isinstance(value, str) else ""


def _sequence_field(facts: Mapping[str, Any], key: str) -> tuple[str, ...]:
    value = facts.get(key)
    if not isinstance(value, Sequence) or isinstance(value, str):
        return ()
    return tuple(item for item in value if isinstance(item, str))


def assess_suitability(
    *,
    domain: str,
    verification_status: VerificationStatus | None,
    verified_facts: Mapping[str, Any] | None,
    seller_offering: str,
    market: str | None,
) -> SuitabilityAssessment:
    """The gate. Deterministic, no model call, no network call — every input
    is either a canonical domain, the customer's own offering summary, or
    facts a previous step already read off the company's own website."""
    if is_non_business_domain(domain):
        return SuitabilityAssessment(
            DiscoverySuitability.CONTRADICTED,
            f"{domain} is a directory, listing or content platform, not a company to sell to.",
        )

    if verification_status is VerificationStatus.REJECTED:
        reason = (
            _string_field(verified_facts, "reasoning")
            if verified_facts is not None
            else "The company's own website contradicted the target."
        )
        return SuitabilityAssessment(
            DiscoverySuitability.CONTRADICTED,
            reason or "The company's own website contradicted the target.",
        )

    if verified_facts is None or verification_status is not VerificationStatus.VERIFIED:
        return SuitabilityAssessment(
            DiscoverySuitability.UNCERTAIN,
            "ARIE could not read this company's own website, so nothing about "
            "this company is confirmed by public evidence yet.",
        )

    relevance = _string_field(verified_facts, "business_relevance")
    if relevance == "clearly_irrelevant":
        return SuitabilityAssessment(
            DiscoverySuitability.CONTRADICTED,
            _string_field(verified_facts, "reasoning")
            or "The company's own website contradicted the target.",
        )

    industry = _string_field(verified_facts, "industry_category")
    services = _sequence_field(verified_facts, "products_services")
    offered = f"{industry} {' '.join(services)}".strip()

    if any(_contains_term(offered.lower(), term) for term in _DIRECTORY_FACT_TERMS) or any(
        _contains_term(_string_field(verified_facts, "business_description").lower(), term)
        for term in _DIRECTORY_FACT_TERMS
    ):
        return SuitabilityAssessment(
            DiscoverySuitability.CONTRADICTED,
            "This site describes itself as a directory, listing or publication "
            "rather than a business that could buy from you.",
        )

    seller_categories = _categories(seller_offering)
    candidate_categories = _categories(offered)
    shared = bool(seller_categories & candidate_categories) or bool(
        seller_categories & _SUPPLY_SIDE_FAMILY and candidate_categories & _SUPPLY_SIDE_FAMILY
    )
    if shared:
        return SuitabilityAssessment(
            DiscoverySuitability.CONTRADICTED,
            "This company's own website says it sells the same kind of service "
            "you sell, so it is a supplier or competitor rather than a buyer.",
        )

    if market:
        clue = _string_field(verified_facts, "geography_clue")
        market_country = _country_for(market)
        clue_country = _country_for(clue) if clue else None
        if market_country and clue_country and clue_country != market_country:
            return SuitabilityAssessment(
                DiscoverySuitability.CONTRADICTED,
                f"The company's own website places it in {clue_country.title()}, "
                f"not {market_country.title()}.",
            )

    # `SUPPORTED` is an allow-list, never a fall-through: a facts document
    # with a missing or unrecognised `business_relevance` is evidence of
    # nothing, and must not inherit the benefit of the doubt that a real
    # `clearly_relevant`/`plausible` reading earns.
    if relevance not in ("clearly_relevant", "plausible"):
        return SuitabilityAssessment(
            DiscoverySuitability.UNCERTAIN,
            "The company's website was reachable but said too little to confirm "
            "it matches what you are looking for.",
        )

    return SuitabilityAssessment(
        DiscoverySuitability.SUPPORTED,
        _string_field(verified_facts, "reasoning")
        or "The company's own website is consistent with your target.",
    )
