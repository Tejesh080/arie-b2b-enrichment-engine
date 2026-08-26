"""Canonical taxonomy — the mapping layer between real vendors and ARIE's scorer.

Every test here is pure: no network, no database, no fixtures on disk. The
question this file answers is the one the Live V1 audit raised — *does a real
provider's vocabulary reach the scorer as something the scorer understands, or
as a silent zero?*
"""

from __future__ import annotations

import pytest

from arie.normalization.taxonomy import (
    CANONICAL_FUNCTIONS,
    CANONICAL_INDUSTRIES,
    CANONICAL_SENIORITIES,
    UNKNOWN,
    canonical_key,
    function_from_title,
    normalize_employee_count,
    normalize_function,
    normalize_industry,
    normalize_seniority,
    seniority_from_title,
)
from arie.scoring.rules import field_points, is_unknown

# --------------------------------------------------------------- industry --

# The Phase 4 acceptance list, verbatim: every surface form of "this is a
# software company" a real provider might send. Each must earn full software
# points, not zero.
SOFTWARE_VARIANTS = (
    "software",
    "computer software",
    "Computer Software",
    "COMPUTER SOFTWARE",
    "  Computer Software  ",
    "computer-software",
    "software development",
    "Software Development",
    "SaaS",
    "saas",
    "B2B SaaS",
    "Software as a Service",
    "Enterprise Software",
    "Information Technology & Services",
    "Internet Software and Services",
    "Technology",
    "Cloud Computing",
)


@pytest.mark.parametrize("raw", SOFTWARE_VARIANTS)
def test_every_software_surface_form_maps_to_the_scoring_canonical_value(raw: str) -> None:
    assert normalize_industry(raw) == "software"


@pytest.mark.parametrize("raw", SOFTWARE_VARIANTS)
def test_every_software_surface_form_actually_earns_points(raw: str) -> None:
    """The regression that matters. Mapping to a canonical value is only useful
    if that value is one the scorer pays for — a mapping onto a canonical-but-
    unscored family would pass the test above and still score zero."""
    assert field_points("industry", normalize_industry(raw)) == pytest.approx(15.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # The four "X-tech" families must beat their non-tech parents.
        ("Financial Technology", "fintech"),
        ("FinTech", "fintech"),
        ("Payments", "fintech"),
        ("Insurtech", "fintech"),
        ("Digital Health", "healthtech"),
        ("Healthcare Software", "healthtech"),
        ("EdTech", "education"),
        ("E-Learning", "education"),
        # ...and the parents must not be captured by them.
        ("Financial Services", "financial_services"),
        ("Banking", "financial_services"),
        ("Insurance", "financial_services"),
        ("Investment Management", "financial_services"),
        ("Hospital & Health Care", "healthcare"),
        ("Medical Practice", "healthcare"),
        ("Pharmaceuticals", "healthcare"),
        # The generic-technology rule must not swallow bio/medical.
        ("Biotechnology", "healthcare"),
        ("biotech", "healthcare"),
        ("Medical Technology", "healthcare"),
        # Ordinary families.
        ("E-Commerce", "ecommerce"),
        ("Online Retail", "ecommerce"),
        ("Retail", "retail"),
        ("Logistics and Supply Chain", "logistics"),
        ("Transportation/Trucking/Railroad", "logistics"),
        ("Industrial Machinery Manufacturing", "manufacturing"),
        ("Automotive", "manufacturing"),
        ("Higher Education", "education"),
        ("Non-profit Organization Management", "nonprofit"),
        ("Management Consulting", "professional_services"),
        ("Marketing and Advertising", "professional_services"),
        ("Construction", "construction"),
        ("Commercial Real Estate", "real_estate"),
        ("Computer Games", "media"),
        ("Telecommunications", "telecom"),
        ("Oil & Energy", "energy"),
        ("Restaurants", "hospitality"),
        ("Government Administration", "government"),
        ("Farming", "agriculture"),
    ],
)
def test_industry_families_map_deliberately(raw: str, expected: str) -> None:
    assert normalize_industry(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "Pet Grooming Franchises",
        "Artisanal Widget Refurbishment",
        "???",
        "12345",
    ],
)
def test_unrecognised_or_absent_industry_is_unknown_never_a_canonical_value(raw: object) -> None:
    result = normalize_industry(raw)
    assert result == UNKNOWN
    assert is_unknown(result)


def test_every_mapped_industry_is_a_member_of_the_closed_canonical_set() -> None:
    """The boundary invariant. If this ever fails, a mapping table has grown a
    value the scorer, the ICP config, and the receipt do not share."""
    probes = [
        *SOFTWARE_VARIANTS,
        "Financial Services",
        "Hospital & Health Care",
        "Construction",
        "nonsense that will not match anything at all",
    ]
    for raw in probes:
        assert normalize_industry(raw) in CANONICAL_INDUSTRIES, raw


def test_known_negative_and_unknown_both_score_zero_but_are_different_states() -> None:
    """The central distinction, stated as one assertion pair.

    `construction` is deliberately assessed and worth nothing under this ICP.
    `"Pet Grooming Franchises"` is not assessed at all. Both contribute 0.0
    points — and `is_unknown` is what separates them, which is what
    `arie.scoring.engine` reads for bounds and completeness.
    """
    known_negative = normalize_industry("Construction")
    unknown = normalize_industry("Pet Grooming Franchises")

    assert field_points("industry", known_negative) == 0.0
    assert field_points("industry", unknown) == 0.0

    assert not is_unknown(known_negative)
    assert is_unknown(unknown)


# -------------------------------------------------------------- seniority --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("c_level", "c_level"),
        ("C-Level", "c_level"),
        ("chief", "c_level"),
        ("executive", "c_level"),
        ("founder", "c_level"),
        ("owner", "c_level"),
        ("vp", "vp"),
        ("VP", "vp"),
        ("svp", "vp"),
        ("vice president", "vp"),
        ("director", "director"),
        ("head", "director"),
        ("manager", "manager"),
        ("senior manager", "manager"),
        ("entry", "ic"),
        ("intern", "ic"),
        ("senior", "ic"),
    ],
)
def test_seniority_enum_values_map_onto_the_scorer_ladder(raw: str, expected: str) -> None:
    assert normalize_seniority(raw) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Chief Revenue Officer", "c_level"),
        ("CEO", "c_level"),
        ("Co-Founder & CTO", "c_level"),
        ("President", "c_level"),
        ("VP of Sales", "vp"),
        ("SVP, Revenue Operations", "vp"),
        ("Vice President of Marketing", "vp"),
        ("Head of Revenue Operations", "director"),
        ("Senior Director, Marketing", "director"),
        ("Director of Demand Generation", "director"),
        ("Growth Manager", "manager"),
        ("Team Lead, Sales Ops", "manager"),
        ("Data Analyst", "ic"),
        ("Senior Software Engineer", "ic"),
        ("Marketing Coordinator", "ic"),
    ],
)
def test_seniority_from_free_text_titles(title: str, expected: str) -> None:
    assert seniority_from_title(title) == expected


def test_the_most_senior_token_in_a_compound_title_wins() -> None:
    """A title says several things at once. "VP of Engineering, Data" contains
    both a VP and an engineer; reading it as an IC would cost 16 points."""
    assert seniority_from_title("VP of Engineering, Data") == "vp"
    assert seniority_from_title("Chief Data Officer & Head of Analytics") == "c_level"


@pytest.mark.parametrize("title", [None, "", "Zookeeper", "Vibes Curator", "Wrangler"])
def test_an_unparseable_title_is_unknown_not_ic(title: object) -> None:
    """Defaulting an unreadable title to the bottom rung would quietly reject
    every unusual title — an active wrong answer dressed as a safe one."""
    result = seniority_from_title(title)
    assert result == UNKNOWN
    assert result != "ic"


def test_a_recognised_rung_inside_a_silly_title_is_still_recognised() -> None:
    """The complement of the test above, and the reason it must not be greedier:
    "Chief Happiness Wrangler" is a real C-level-shaped title. `UNKNOWN` is for
    titles carrying *no* rung signal, not for titles that are unusual."""
    assert seniority_from_title("Chief Happiness Wrangler of Vibes") == "c_level"


def test_vice_president_is_not_read_as_president() -> None:
    """Rule ordering. "Vice President" contains "President"; without the
    explicit precedence every VP in the pipeline would collect C-level's
    20 points instead of VP's 18."""
    assert seniority_from_title("Vice President of Marketing") == "vp"
    assert seniority_from_title("President") == "c_level"


def test_every_mapped_seniority_is_canonical() -> None:
    for raw in ("vp", "Head of Sales", "nonsense", "", "CEO"):
        assert normalize_seniority(raw) in CANONICAL_SENIORITIES
        assert seniority_from_title(raw) in CANONICAL_SENIORITIES


# --------------------------------------------------------------- function --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("sales", "sales"),
        ("Business Development", "sales"),
        ("Customer Success", "sales"),
        # The reference ICP's revenue_operations / growth folds.
        ("revenue_operations", "operations"),
        ("Revenue Operations", "operations"),
        ("RevOps", "operations"),
        ("Sales Operations", "operations"),
        ("operations", "operations"),
        ("growth", "marketing"),
        ("Growth Marketing", "marketing"),
        ("Demand Generation", "marketing"),
        ("marketing", "marketing"),
        ("Data Science", "data"),
        ("Analytics", "data"),
        ("Business Intelligence", "data"),
        ("Engineering", "engineering"),
        ("DevOps", "engineering"),
        ("Information Technology", "engineering"),
        ("Finance", "finance"),
        ("Accounting", "finance"),
        ("Human Resources", "other"),
        ("Product Management", "other"),
        ("Legal", "other"),
    ],
)
def test_function_enum_values_map_onto_the_scorer_vocabulary(raw: str, expected: str) -> None:
    assert normalize_function(raw) == expected


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("VP of Revenue Operations", "operations"),
        ("Head of RevOps", "operations"),
        ("Director of Sales Operations", "operations"),
        ("VP of Sales", "sales"),
        ("Chief Revenue Officer", "sales"),
        ("Growth Manager", "marketing"),
        ("Senior Director, Marketing", "marketing"),
        ("Data Analyst", "data"),
        ("Head of Business Intelligence", "data"),
        ("IT Director", "engineering"),
        ("Staff Software Engineer", "engineering"),
        ("Financial Controller", "finance"),
        ("Head of People", "other"),
    ],
)
def test_function_from_free_text_titles(title: str, expected: str) -> None:
    assert function_from_title(title) == expected


def test_revenue_operations_outranks_bare_sales_in_a_compound_title() -> None:
    """Rule ordering, asserted rather than trusted. "VP Revenue Operations"
    contains "revenue"; reading it as `sales` (5.0) instead of `operations`
    (9.0) would systematically under-credit the reference ICP's own
    highest-intent function."""
    assert function_from_title("VP Revenue Operations") == "operations"
    assert field_points("title_function", "operations") > field_points("title_function", "sales")


@pytest.mark.parametrize("title", [None, "", "Zookeeper", "Vibes Curator"])
def test_an_unparseable_function_is_unknown_not_other(title: object) -> None:
    """`other` is a *known* low-value function worth 2.0 points. Using it for
    "we could not parse this" would credit an unreadable title with real
    evidence and shrink the score bounds on nothing."""
    result = function_from_title(title)
    assert result == UNKNOWN
    assert result != "other"
    assert field_points("title_function", "other") > 0.0


def test_every_mapped_function_is_canonical() -> None:
    for raw in ("RevOps", "growth", "nonsense", "", "Data Science"):
        assert normalize_function(raw) in CANONICAL_FUNCTIONS
        assert function_from_title(raw) in CANONICAL_FUNCTIONS


# --------------------------------------------------------- employee_count --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (4200, 4200),
        (42.0, 42),
        ("120", 120),
        ("1,250", 1250),
        ("51-200", 51),
        ("201-1000", 201),
        ("1001+", 1001),
        ("200 employees", 200),
        (1, 1),
    ],
)
def test_valid_headcounts_are_accepted(raw: object, expected: int) -> None:
    assert normalize_employee_count(raw) == expected


@pytest.mark.parametrize("raw", [None, True, False, 0, -3, "abc", "", "   ", 10_000_001, 5e9])
def test_invalid_headcounts_are_unknown_not_zero(raw: object) -> None:
    """A headcount of 0 is a *known* company size worth 0.0 points. A provider
    that returns 0, a negative, or a placeholder has told us nothing, and the
    two must not collapse into the same reading."""
    result = normalize_employee_count(raw)
    assert result == UNKNOWN
    assert is_unknown(result)


def test_a_banded_headcount_collapses_to_its_lower_bound() -> None:
    """Not the midpoint: taking 600 for "201-1000" would move a company between
    the scorer's size tiers on a number the provider never reported."""
    assert normalize_employee_count("51-200") == 51
    assert normalize_employee_count("201-1000") == 201


# ------------------------------------------------------------ surface form --


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Computer Software", "computer software"),
        ("computer-software", "computer software"),
        ("  Computer   Software  ", "computer software"),
        ("Oil & Energy", "oil and energy"),
        ("Transportation/Trucking/Railroad", "transportation trucking railroad"),
        ("Non-profit Organization Management", "non profit organization management"),
        (None, ""),
        ("", ""),
    ],
)
def test_canonical_key_folds_surface_noise(raw: object, expected: str) -> None:
    assert canonical_key(raw) == expected


def test_word_boundary_matching_keeps_tech_out_of_biotechnology() -> None:
    """The specific bug substring matching would have introduced: "tech" is a
    software rule, "biotechnology" is not a software company."""
    assert normalize_industry("Tech") == "software"
    assert normalize_industry("Biotechnology") == "healthcare"
