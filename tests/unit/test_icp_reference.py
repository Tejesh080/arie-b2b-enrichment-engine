"""The Reference ICP for Live V1 (Phase 3).

Two things are checked here, and the second matters more than the first:

1. The profile says what the brief asked it to say.
2. It stays *descriptive* — it does not become a second scorer, and it never
   invents evidence for a criterion nothing can observe.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arie.icp import (
    REFERENCE_ICP_V1,
    CriterionVerdict,
    ICPAssessment,
    ReferenceICP,
    assess,
    free_email_disqualifier,
)
from arie.identity.normalize import FREE_EMAIL_DOMAINS
from arie.normalization.taxonomy import (
    CANONICAL_FUNCTIONS,
    CANONICAL_INDUSTRIES,
    CANONICAL_SENIORITIES,
    UNKNOWN,
    normalize_function,
    normalize_industry,
    normalize_seniority,
)


def _verdict(assessment: ICPAssessment, name: str) -> CriterionVerdict:
    return next(item.verdict for item in assessment.criteria if item.name == name)


# ------------------------------------------------------------ the profile --


def test_the_profile_is_named_and_versioned() -> None:
    """It is *a* reference profile, not a claim about every business — the name
    is how a future second profile stays distinguishable from this one."""
    assert REFERENCE_ICP_V1.name == "live-v1-reference"
    assert REFERENCE_ICP_V1.version == "1.0.0"


def test_the_company_profile_matches_the_brief() -> None:
    assert REFERENCE_ICP_V1.employee_count_min == 50
    assert REFERENCE_ICP_V1.employee_count_max == 1000
    assert REFERENCE_ICP_V1.geographies == {"US", "GB", "AU", "CA"}
    assert "software" in REFERENCE_ICP_V1.industries


def test_the_brief_s_contact_vocabulary_is_represented_after_the_documented_fold() -> None:
    """`revenue_operations` and `growth` have no weight in the scorer, so they
    fold onto `operations`/`marketing` rather than becoming canonical values
    that would score 0.0 — which would make ARIE's own highest-intent functions
    read as known-negative. The fold is recorded, not silent."""
    assert normalize_function("revenue_operations") in REFERENCE_ICP_V1.functions
    assert normalize_function("growth") in REFERENCE_ICP_V1.functions
    assert normalize_function("sales") in REFERENCE_ICP_V1.functions
    assert normalize_function("marketing") in REFERENCE_ICP_V1.functions
    assert normalize_function("operations") in REFERENCE_ICP_V1.functions

    assert "revenue_operations" in REFERENCE_ICP_V1.intent_notes
    assert "growth" in REFERENCE_ICP_V1.intent_notes


def test_the_brief_s_seniorities_are_represented_after_the_documented_fold() -> None:
    for raw in ("director", "vp", "head", "executive"):
        assert normalize_seniority(raw) in REFERENCE_ICP_V1.seniorities, raw
    assert "head" in REFERENCE_ICP_V1.intent_notes


def test_the_profile_uses_only_canonical_vocabulary() -> None:
    assert REFERENCE_ICP_V1.industries <= CANONICAL_INDUSTRIES
    assert REFERENCE_ICP_V1.functions <= CANONICAL_FUNCTIONS
    assert REFERENCE_ICP_V1.seniorities <= CANONICAL_SENIORITIES


def test_a_profile_built_from_non_canonical_vocabulary_is_refused() -> None:
    """A typo would otherwise produce a criterion that can never be met, and
    would do it silently. Import-time is the cheapest place to catch it."""
    with pytest.raises(ValueError, match="non-canonical"):
        ReferenceICP(
            name="broken",
            version="0",
            industries=frozenset({"softwaer"}),
            employee_count_min=1,
            employee_count_max=2,
            geographies=frozenset({"US"}),
            functions=frozenset({"sales"}),
            seniorities=frozenset({"vp"}),
            observable_disqualifiers=frozenset(),
            declared_disqualifiers=frozenset(),
            intent_notes={},
        )


def test_the_config_lives_in_one_place_not_in_provider_adapters() -> None:
    """The brief's "keep the reference ICP configurable in code/config rather
    than burying constants throughout provider adapters"."""
    import arie.providers.apollo_contract as apollo
    import arie.providers.live_abstract as abstract

    for module in (abstract, apollo):
        source = module.__file__
        assert source is not None
        text = Path(source).read_text(encoding="utf-8")
        assert "employee_count_min" not in text
        assert "REFERENCE_ICP" not in text


# ---------------------------------------------------------- the assessment --


def _fit_facts() -> dict[str, object]:
    return {
        "industry": normalize_industry("Computer Software"),
        "employee_count": 250,
        "title_function": normalize_function("Revenue Operations"),
        "title_seniority": normalize_seniority("vp"),
    }


def test_an_on_profile_lead_meets_the_observable_criteria() -> None:
    assessment = assess(_fit_facts(), canonical_email="dana@acme.test")
    for name in ("industry", "employee_count", "title_function", "title_seniority"):
        assert _verdict(assessment, name) is CriterionVerdict.MET


def test_a_known_off_profile_value_is_missed_not_unknown() -> None:
    facts = {**_fit_facts(), "industry": normalize_industry("Construction")}
    assessment = assess(facts, canonical_email="dana@acme.test")
    assert _verdict(assessment, "industry") is CriterionVerdict.MISSED


def test_an_unmappable_value_is_unknown_not_missed() -> None:
    """The same distinction the taxonomy draws, carried into the fit report.
    Collapsing "we could not read this" into "this does not fit" is how a
    reviewer ends up rejecting a lead nobody ever assessed."""
    facts = {**_fit_facts(), "industry": normalize_industry("Pet Grooming Franchises")}
    assessment = assess(facts, canonical_email="dana@acme.test")
    assert _verdict(assessment, "industry") is CriterionVerdict.UNKNOWN


def test_a_never_observed_field_is_unknown_too() -> None:
    facts = {k: v for k, v in _fit_facts().items() if k != "title_seniority"}
    assert _verdict(assess(facts), "title_seniority") is CriterionVerdict.UNKNOWN
    assert _verdict(assess({**facts, "title_seniority": UNKNOWN}), "title_seniority") is (
        CriterionVerdict.UNKNOWN
    )


def test_headcount_outside_the_band_is_missed() -> None:
    for count in (12, 25_000):
        assessment = assess({**_fit_facts(), "employee_count": count})
        assert _verdict(assessment, "employee_count") is CriterionVerdict.MISSED


def test_headcount_at_the_band_edges_is_met() -> None:
    for count in (50, 1000):
        assessment = assess({**_fit_facts(), "employee_count": count})
        assert _verdict(assessment, "employee_count") is CriterionVerdict.MET


# ------------------------------------------------ unobservable, not invented --


def test_geography_is_declared_and_reported_as_unobservable() -> None:
    """No configured live provider returns a country. Dropping the criterion
    would hide the gap; guessing one would be worse. It is reported."""
    assert _verdict(assess(_fit_facts()), "geography") is CriterionVerdict.UNOBSERVABLE


@pytest.mark.parametrize("name", ["student", "individual_or_freelancer"])
def test_the_unobservable_disqualifiers_are_never_inferred(name: str) -> None:
    """The brief's "Do NOT invent a disqualifying flag when evidence does not
    exist" — enforced as a verdict a reviewer can see rather than as a
    convention."""
    assert name in REFERENCE_ICP_V1.declared_disqualifiers
    assert _verdict(assess(_fit_facts()), name) is CriterionVerdict.UNOBSERVABLE


def test_the_assessment_never_writes_a_disqualifying_flag() -> None:
    """`disqualifying_flag` means "a checked blocker exists" — a claim no
    current live provider can support. The fit report must not manufacture one,
    because the scorer treats it as absolute and it would zero the lead."""
    assessment = assess(_fit_facts(), canonical_email="someone@gmail.com")
    assert all(item.name != "disqualifying_flag" for item in assessment.criteria)


# ---------------------------------------- the one observable disqualifier --


@pytest.mark.parametrize("domain", sorted(FREE_EMAIL_DOMAINS)[:5])
def test_a_free_mailbox_is_the_one_genuinely_observable_blocker(domain: str) -> None:
    assert free_email_disqualifier(f"someone@{domain}") is True
    assessment = assess(_fit_facts(), canonical_email=f"someone@{domain}")
    assert _verdict(assessment, "free_email_domain") is CriterionVerdict.MISSED


def test_a_corporate_mailbox_is_not_a_blocker() -> None:
    assert free_email_disqualifier("dana@acme.test") is False
    assessment = assess(_fit_facts(), canonical_email="dana@acme.test")
    assert _verdict(assessment, "free_email_domain") is CriterionVerdict.MET


@pytest.mark.parametrize("email", [None, "", "not-an-email", "@", "dana@"])
def test_a_missing_or_malformed_address_is_not_evidence_of_a_personal_one(
    email: str | None,
) -> None:
    assert free_email_disqualifier(email) is False


def test_the_free_email_list_is_shared_with_identity_resolution_not_duplicated() -> None:
    """One list. A second copy here would drift from the one the ingestion path
    already uses to decide whether an email implies a company domain — and the
    drift would be invisible, since both would keep working.

    Checked two ways: the module imports the shared list rather than defining
    its own, and every domain in that list is actually treated as a blocker.
    """
    import arie.icp as icp

    source_path = icp.__file__
    assert source_path is not None
    source = Path(source_path).read_text(encoding="utf-8")
    assert "from arie.identity.normalize import FREE_EMAIL_DOMAINS" in source

    for domain in FREE_EMAIL_DOMAINS:
        assert free_email_disqualifier(f"someone@{domain}") is True, domain


# ------------------------------------------------------------- the counts --


def test_the_assessment_summarises_its_four_verdict_kinds() -> None:
    assessment = assess(
        {**_fit_facts(), "industry": normalize_industry("Construction")},
        canonical_email="someone@gmail.com",
    )
    total = assessment.met + assessment.missed + assessment.unknown + assessment.unobservable
    assert total == len(assessment.criteria)
    assert assessment.missed >= 2  # industry + free mailbox
    assert assessment.unobservable >= 3  # geography + the two declared blockers


def test_the_assessment_produces_no_score_and_no_decision() -> None:
    """It is descriptive. A second surface that turns facts into a number would
    eventually disagree with `arie.scoring.rules`, and then there would be two
    answers to "does this lead qualify"."""
    assessment = assess(_fit_facts())
    assert not hasattr(assessment, "score")
    assert not hasattr(assessment, "decision")
