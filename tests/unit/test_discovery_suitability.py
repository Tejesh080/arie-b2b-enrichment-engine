"""The discovery suitability gate — `arie.discovery.suitability`.

Every case here is one the real contactable-opportunity proof produced: a
directory ranked as a prospect, a vendor whose own site sells what the
customer sells ranked `contact_first`, and an unverifiable company ranked
top of the list on simulated evidence alone.
"""

from __future__ import annotations

from typing import Any

import pytest

from arie.discovery.company_identity import (
    domain_derived_name,
    looks_like_a_company_name,
    resolve_company_name,
)
from arie.discovery.models import DiscoverySuitability, VerificationStatus
from arie.discovery.search_planning import targets_the_seller
from arie.discovery.suitability import (
    assess_suitability,
    is_non_business_domain,
    offering_category_terms,
)

_SELLER = "We build AI automation and custom software systems worth roughly $5,000-$25,000."


def _facts(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "business_relevance": "clearly_relevant",
        "business_description": "A family-owned freight business running a fleet across Queensland.",
        "industry_category": "logistics and freight",
        "customer_type": "b2b",
        "products_services": ["freight forwarding", "warehousing"],
        "geography_clue": "Brisbane, Australia",
        "reasoning": "The page states they run a Brisbane depot and a 40-truck fleet.",
    }
    base.update(overrides)
    return base


def _assess(
    *,
    domain: str = "examplefreight.com.au",
    status: VerificationStatus | None = VerificationStatus.VERIFIED,
    facts: dict[str, Any] | None = None,
    market: str | None = "Australia",
) -> tuple[DiscoverySuitability, str]:
    result = assess_suitability(
        domain=domain,
        verification_status=status,
        verified_facts=_facts() if facts is None else facts,
        seller_offering=_SELLER,
        market=market,
    )
    return result.suitability, result.reason


# ------------------------------------------------------------------ the gate --


def test_real_verified_evidence_consistent_with_the_target_is_supported() -> None:
    suitability, _ = _assess()
    assert suitability is DiscoverySuitability.SUPPORTED


def test_unreachable_website_is_uncertain_never_supported() -> None:
    """Fix 3: an unverifiable company has nothing but simulated evidence
    behind it, so it can never reach the state that keeps a high priority."""
    suitability, reason = _assess(status=VerificationStatus.UNAVAILABLE, facts=None)
    assert suitability is DiscoverySuitability.UNCERTAIN
    assert "could not read" in reason


def test_missing_facts_are_uncertain_even_when_status_claims_verified() -> None:
    suitability, _ = _assess(status=VerificationStatus.VERIFIED, facts={})
    assert suitability is DiscoverySuitability.UNCERTAIN


def test_rejected_verification_is_contradicted() -> None:
    suitability, reason = _assess(
        status=VerificationStatus.REJECTED,
        facts=_facts(business_relevance="clearly_irrelevant", reasoning="A parked domain."),
    )
    assert suitability is DiscoverySuitability.CONTRADICTED
    assert reason == "A parked domain."


@pytest.mark.parametrize(
    "domain",
    [
        "goodfirms.co",
        "www.clutch.co",
        "linkedin.com",
        "medium.com",
        "au.linkedin.com",
        "koala-coyote-y4fh.squarespace.com",
    ],
)
def test_directories_and_content_platforms_are_never_prospects(domain: str) -> None:
    """Fix 6 — GoodFirms was a top-six 'opportunity' in the previous run."""
    assert is_non_business_domain(domain)
    suitability, _ = _assess(domain=domain)
    assert suitability is DiscoverySuitability.CONTRADICTED


def test_a_real_company_domain_is_not_mistaken_for_a_directory() -> None:
    assert not is_non_business_domain("exiq.com.au")
    assert not is_non_business_domain("linkedinstrategies.com.au")


def test_a_site_describing_itself_as_a_directory_is_contradicted() -> None:
    suitability, reason = _assess(
        facts=_facts(
            industry_category="B2B ratings and reviews platform",
            products_services=["company directory"],
        )
    )
    assert suitability is DiscoverySuitability.CONTRADICTED
    assert "directory" in reason


def test_a_company_selling_what_the_seller_sells_is_contradicted() -> None:
    """Fix 4: Appinventiv/MarkupDesigns — real companies, real people, but
    software vendors rather than buyers of custom software."""
    suitability, reason = _assess(
        facts=_facts(
            industry_category="IT services / digital agency",
            products_services=["Custom software development", "AI automation", "Cloud services"],
        )
    )
    assert suitability is DiscoverySuitability.CONTRADICTED
    assert "supplier or competitor" in reason


def test_a_packaged_software_vendor_is_contradicted_even_in_a_different_niche() -> None:
    """The rerun's surviving false positive: an Australian company selling
    ERP and warehouse software matched `it_consulting` while the seller
    matched `software_development`/`ai_ml`, so category-for-category
    comparison let it through as a buyer."""
    suitability, reason = _assess(
        facts=_facts(
            industry_category="Business software and IT services",
            products_services=[
                "ERP software",
                "Warehouse management systems",
                "Wholesale distribution software",
            ],
        )
    )
    assert suitability is DiscoverySuitability.CONTRADICTED
    assert "supplier or competitor" in reason


def test_geography_mismatch_against_the_requested_market_is_contradicted() -> None:
    suitability, reason = _assess(facts=_facts(geography_clue="Noida, India"))
    assert suitability is DiscoverySuitability.CONTRADICTED
    assert "India" in reason


def test_no_geography_clue_is_not_a_mismatch() -> None:
    suitability, _ = _assess(facts=_facts(geography_clue=None))
    assert suitability is DiscoverySuitability.SUPPORTED


def test_insufficient_page_content_is_uncertain() -> None:
    suitability, _ = _assess(facts=_facts(business_relevance="insufficient_content"))
    assert suitability is DiscoverySuitability.UNCERTAIN


def test_a_buyer_who_merely_mentions_automation_in_prose_is_not_a_competitor() -> None:
    """Competitor detection reads what a company *offers*, not its prose —
    a manufacturer describing its own automated line is still a buyer."""
    suitability, _ = _assess(
        facts=_facts(
            business_description="Our automated packing line runs custom software we bought in.",
        )
    )
    assert suitability is DiscoverySuitability.SUPPORTED


# --------------------------------------------------------- company identity --


@pytest.mark.parametrize(
    "value",
    [
        "AI in Healthcare in Australia: Transforming Patient Care ...",
        "Logistics Software Development Australia | ISH 2026 Brisbane",
        "Top Artificial Intelligence Companies in Australia - 2026 Reviews",
        "How we cut costs by 40%",
        "Home",
        "",
        None,
    ],
)
def test_headlines_and_placeholders_are_not_company_names(value: str | None) -> None:
    assert not looks_like_a_company_name(value)


@pytest.mark.parametrize("value", ["ExIQ", "Appinventiv", "ISH Technologies", "Design Pluz"])
def test_real_company_names_are_accepted(value: str) -> None:
    assert looks_like_a_company_name(value)


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("exiq.com.au", "Exiq"),
        ("ishtechnologies.com.au", "Ishtechnologies"),
        ("design-pluz.com.au", "Design Pluz"),
        ("blog.appinventiv.com", "Appinventiv"),
    ],
)
def test_domain_derived_names(domain: str, expected: str) -> None:
    assert domain_derived_name(domain) == expected


def test_resolution_order_metadata_then_site_then_domain() -> None:
    assert (
        resolve_company_name(
            provider_site_name="ExIQ", verified_site_name="Something Else", domain="exiq.com.au"
        )
        == "ExIQ"
    )
    assert (
        resolve_company_name(
            provider_site_name=None, verified_site_name="ISH Technologies", domain="ish.com.au"
        )
        == "ISH Technologies"
    )
    assert (
        resolve_company_name(
            provider_site_name="Best Logistics Software | 2026 Guide",
            verified_site_name=None,
            domain="ish.com.au",
        )
        == "Ish"
    )


# -------------------------------------------------------------- query intent --


def test_seller_vocabulary_queries_are_rejected() -> None:
    """Fix 5: these are the queries that produced a page of vendors last run."""
    terms = offering_category_terms(_SELLER)
    assert targets_the_seller("AI automation companies Australia", terms)
    assert targets_the_seller("custom software development Sydney", terms)
    assert targets_the_seller("top digital transformation consultants Australia", terms)


def test_buyer_shaped_queries_survive() -> None:
    terms = offering_category_terms(_SELLER)
    assert not targets_the_seller("third party logistics operators Brisbane", terms)
    assert not targets_the_seller("family owned food manufacturers Victoria", terms)
    assert not targets_the_seller("multi site aged care operators New South Wales", terms)
