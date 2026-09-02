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
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from arie.discovery.models import MAX_SEARCH_QUERIES, DiscoverySearchPlan, SearchQueryItem
from arie.llm.provider import LLMPurpose
from arie.llm.service import LLMService
from arie.llm.structured import UntrustedBlock

__all__ = ["SearchPlanResult", "build_fallback_queries", "generate_search_plan", "normalize_query"]

MAX_DESCRIPTION_CHARS = 2000

_INSTRUCTIONS = f"""You are ARIE's market discovery planner. A business owner \
has described what they sell and who they want to reach. Propose search \
queries a web search engine could run to find real companies that might be a \
good fit.

RULES

1. Propose at most {MAX_SEARCH_QUERIES} queries. Fewer, distinct queries beat \
many near-duplicates — do not propose five phrasings of the same idea.

2. Each query should target a genuinely different angle: a different kind of \
company, a different way of describing the target market, or a different \
sub-segment. Vary vocabulary meaningfully, not just word order.

3. If a market or geography was given, include it naturally in the query text \
where it would actually help a search engine, not in every single query \
mechanically.

4. A query is search phrasing only — never a URL, never a company name you \
already know, never an instruction to a tool. You are not choosing how the \
search runs, only what to search for.

5. State a one-sentence rationale for each query: what kind of company it is \
meant to surface."""


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

    deduped = _dedupe_queries(result.value.queries)
    if not deduped:
        return SearchPlanResult(plan=fallback, llm_used=False, cost_usd=result.cost_usd)
    return SearchPlanResult(
        plan=DiscoverySearchPlan(queries=deduped), llm_used=True, cost_usd=result.cost_usd
    )
