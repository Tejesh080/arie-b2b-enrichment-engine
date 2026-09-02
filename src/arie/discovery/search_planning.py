"""Turning a targeting profile into search intent — Discovery Pivot Phase 2.

The model proposes `DiscoverySearchPlan.queries`; it never supplies a
provider, a URL, or a budget. `arie.discovery.providers` decides how each
query gets executed. A model outage or an unconfigured provider degrades to
`build_fallback_queries`'s deterministic templates — a discovery run must
never fail just because the model is unavailable, the same standing M7 rule
`arie.intelligence.targeting` follows for profile generation, except here
failure degrades to a usable plan instead of an error the caller has to show.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from arie.discovery.models import MAX_SEARCH_QUERIES, DiscoverySearchPlan, SearchQueryItem
from arie.discovery.suitability import offering_category_terms
from arie.llm.provider import LLMPurpose
from arie.llm.service import LLMService
from arie.llm.structured import UntrustedBlock

__all__ = [
    "SearchPlanResult",
    "build_fallback_queries",
    "generate_search_plan",
    "normalize_query",
    "targets_the_seller",
]

MAX_DESCRIPTION_CHARS = 2000

_INSTRUCTIONS = f"""You are ARIE's market discovery planner. A business owner \
has described what they sell and who they want to reach. Propose search \
queries that will surface the websites of REAL COMPANIES THAT COULD BUY FROM \
THEM.

THE ONE MISTAKE THAT MATTERS

Searching the seller's own vocabulary finds other sellers. A query built from \
what this business *offers* ("AI automation Australia", "custom software \
development Sydney") returns the marketing pages and blog posts of competing \
vendors, agencies and consultancies writing about that topic — not a single \
company that would buy it. Every query you write must describe the BUYER: \
what industry they are in, what they physically do, where they operate, how \
big they are.

RULES

1. Propose at most {MAX_SEARCH_QUERIES} queries. Fewer, distinct queries beat \
many near-duplicates — do not propose five phrasings of the same idea.

2. Describe the target customer, never the seller's product. Name an \
industry, a trade, a sector or a kind of operation ("commercial plumbing \
contractors", "third-party logistics operators", "aged care providers", \
"food manufacturers"). Do NOT put the seller's technology or service words \
into a query — no "AI", "automation", "software development", "custom \
software", "digital transformation", "IT services", "consulting", or any \
other phrase describing what the seller does for a living.

3. Never write a query that asks for a ranked list, a comparison or a vendor \
roster: no "top", "best", "leading", "reviews", "vs", "providers", \
"agencies", "vendors", "companies that offer". Those return directories and \
listicles, which are not companies.

4. Prefer phrasing that returns a company's own website: an industry plus a \
place, optionally plus a size or operational signal ("multi-site", "family \
owned", "wholesale", "fleet", "distribution centre").

5. Each query should target a genuinely different angle — a different \
industry or sub-segment, not a reworded version of the last one.

6. If a market or geography was given, include it naturally where it helps a \
search engine, not mechanically in every query.

7. A query is search phrasing only — never a URL, never a company name you \
already know, never an instruction to a tool.

8. State a one-sentence rationale for each query: what kind of BUYER it is \
meant to surface."""


_MARKETING_QUERY_TERMS = (
    "top",
    "best",
    "leading",
    "reviews",
    "review",
    "vs",
    "ranking",
    "ranked",
    "listicle",
    "vendors",
    "vendor",
    "providers",
    "provider",
    "agencies",
    "agency",
    "consultants",
    "consultancy",
    "consulting",
    "firms",
)
"""Query words that reliably return directories, listicles and vendor
marketing rather than a company's own site — Discovery Quality Fix 5/6, the
deterministic backstop to the planner's instructions."""


def _boundary_match(haystack: str, term: str) -> bool:
    return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", haystack) is not None


def targets_the_seller(query: str, seller_terms: Sequence[str]) -> bool:
    """`True` when a query is written in the seller's own service vocabulary,
    or in listicle/vendor-roster phrasing. Such a query searches for people
    who *do what the seller does*; the whole point of discovery is to find
    people who need it done."""
    normalized = normalize_query(query)
    if any(_boundary_match(normalized, term) for term in _MARKETING_QUERY_TERMS):
        return True
    return any(_boundary_match(normalized, term) for term in seller_terms)


def normalize_query(text: str) -> str:
    """Lowercased, whitespace-collapsed — the key `_dedupe_queries` compares
    on, so `"Multi-location gyms  Australia"` and `"multi-location gyms
    australia"` collapse to one query rather than being run twice."""
    return re.sub(r"\s+", " ", text.strip().lower())


def _dedupe_queries(items: list[SearchQueryItem]) -> list[SearchQueryItem]:
    seen: set[str] = set()
    result: list[SearchQueryItem] = []
    for item in items:
        key = normalize_query(item.query)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result[:MAX_SEARCH_QUERIES]


def build_fallback_queries(
    *,
    offering_summary: str,
    ideal_company_types: tuple[str, ...],
    market: str | None,
) -> DiscoverySearchPlan:
    """Deterministic search intent with no model call — what a customer gets
    when the LLM is unavailable, disabled, or over budget. Templates off the
    targeting profile's own `ideal_company_types` where the customer supplied
    any; a generic offering-based query otherwise. Never empty: a discovery
    run always has at least one query to execute."""
    suffix = f" in {market}" if market else ""
    queries: list[SearchQueryItem] = []
    for kind in ideal_company_types[:MAX_SEARCH_QUERIES]:
        queries.append(
            SearchQueryItem(
                query=f"{kind}{suffix}"[:120],
                rationale=f"Directly from your targeting profile's '{kind}' company type.",
            )
        )
    if not queries:
        base = offering_summary.strip() or "businesses that could use this"
        queries.append(
            SearchQueryItem(
                query=f"companies that need {base}{suffix}"[:120],
                rationale="Generic fallback query built from your offering summary.",
            )
        )
    return DiscoverySearchPlan(queries=_dedupe_queries(queries))


@dataclass(frozen=True)
class SearchPlanResult:
    plan: DiscoverySearchPlan
    llm_used: bool
    cost_usd: Decimal


def generate_search_plan(
    llm: LLMService | None,
    *,
    organization_id: UUID,
    offering_summary: str,
    target_summary: str,
    ideal_company_types: tuple[str, ...],
    market: str | None,
    now: datetime,
) -> SearchPlanResult:
    """One bounded LLM call proposing search intent, or the deterministic
    fallback if `llm` is `None` or the call didn't produce a usable plan.
    Post-processing (dedupe, the `MAX_SEARCH_QUERIES` cap) runs on *both*
    paths, so a caller never has to special-case which one it got."""
    fallback = build_fallback_queries(
        offering_summary=offering_summary, ideal_company_types=ideal_company_types, market=market
    )
    if llm is None:
        return SearchPlanResult(plan=fallback, llm_used=False, cost_usd=Decimal(0))

    market_line = f"Market / geography: {market}" if market else "No specific market was given."
    result = llm.generate(
        organization_id=organization_id,
        purpose=LLMPurpose.DISCOVERY_SEARCH_PLANNING,
        model_type=DiscoverySearchPlan,
        instructions=_INSTRUCTIONS,
        now=now,
        untrusted=(
            UntrustedBlock(label="what_you_sell", text=offering_summary[:MAX_DESCRIPTION_CHARS]),
            UntrustedBlock(label="who_you_want", text=target_summary[:MAX_DESCRIPTION_CHARS]),
            UntrustedBlock(label="market", text=market_line[:200]),
        ),
    )
    if result.value is None or not result.value.queries:
        return SearchPlanResult(plan=fallback, llm_used=False, cost_usd=result.cost_usd)

    # Discovery Quality Fix 5: enforce rule 2/3 rather than trusting it. A
    # model that slips back into the seller's own vocabulary loses those
    # queries here, deterministically, before they cost a search call.
    seller_terms = offering_category_terms(offering_summary)
    buyer_facing = [
        item for item in result.value.queries if not targets_the_seller(item.query, seller_terms)
    ]
    deduped = _dedupe_queries(buyer_facing)
    if not deduped:
        return SearchPlanResult(plan=fallback, llm_used=False, cost_usd=result.cost_usd)
    return SearchPlanResult(
        plan=DiscoverySearchPlan(queries=deduped), llm_used=True, cost_usd=result.cost_usd
    )
