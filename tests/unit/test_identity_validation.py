"""The identity validator — pure, deterministic, no I/O.

The Stripe case (a real result from the 2026-08-29 abstract-hunter-live-1
experiment) is the acceptance test: same company, different person, must
never read as VERIFIED.
"""

from __future__ import annotations

from arie.identity.validation import (
    MISMATCH,
    PROBABLE,
    UNVERIFIABLE,
    VERIFIED,
    RequestedIdentity,
    ReturnedIdentity,
    validate_identity,
)

# ------------------------------------------------------------- the real case --


def test_stripe_same_company_different_person_is_a_mismatch() -> None:
    """patrick@stripe.com: Hunter returned Patrick Bosmans, IT Administrator —
    a real match, an honest MATCHED_PROVIDER_RECORD, and not the intended
    MATCHED_INTENDED_PERSON (Patrick Collison)."""
    requested = RequestedIdentity(
        email="patrick@stripe.com", company_domain="stripe.com", full_name="Patrick Collison"
    )
    returned = ReturnedIdentity(
        full_name="Patrick Bosmans",
        email="patrick@stripe.com",
        employer_domain="stripe.com",
        employer_name="Stripe",
    )
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH
    assert any("name" in reason for reason in result.reasons)


def test_a_same_company_match_with_matching_name_is_verified() -> None:
    requested = RequestedIdentity(
        email="ahmed@orderii.co", company_domain="orderii.co", full_name="Ahmed Qais"
    )
    returned = ReturnedIdentity(
        full_name="Ahmed Qais", email="ahmed@orderii.co", employer_domain="orderii.co"
    )
    result = validate_identity(requested, returned)
    assert result.verdict == VERIFIED


# --------------------------------------------------------------------- tiers --


def test_domain_match_alone_with_no_expected_name_is_probable_not_verified() -> None:
    """The common real-world shape: no expected name on file. A single
    corroborating signal cannot rule out a same-domain wrong-person match, so
    this must stay PROBABLE, never VERIFIED."""
    requested = RequestedIdentity(email="jason@37signals.com", company_domain="37signals.com")
    returned = ReturnedIdentity(full_name="Jason Fried", employer_domain="37signals.com")
    result = validate_identity(requested, returned)
    assert result.verdict == PROBABLE


def test_an_echoed_email_does_not_upgrade_domain_only_to_verified() -> None:
    """The exact bug caught by the first real offline replay: Hunter (and
    Apollo) are queried *by* email, so a success almost always echoes the
    requested email straight back. Counting that echo as a second
    corroborating signal made every ordinary domain-only match read as
    VERIFIED — silently defeating the whole PROBABLE tier. This must stay
    PROBABLE even with the email present and matching."""
    requested = RequestedIdentity(email="jason@37signals.com", company_domain="37signals.com")
    returned = ReturnedIdentity(
        full_name="Jason Fried", email="jason@37signals.com", employer_domain="37signals.com"
    )
    result = validate_identity(requested, returned)
    assert result.verdict == PROBABLE


def test_domain_mismatch_is_a_mismatch_even_with_no_name_to_check() -> None:
    requested = RequestedIdentity(email="a@acme.com", company_domain="acme.com")
    returned = ReturnedIdentity(full_name="Someone Else", employer_domain="othercorp.com")
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH


def test_name_mismatch_overrides_an_agreeing_domain() -> None:
    """The core design invariant: one disagreeing signal is disqualifying no
    matter how many others agree."""
    requested = RequestedIdentity(
        email="a@acme.com", company_domain="acme.com", full_name="Jane Doe"
    )
    returned = ReturnedIdentity(
        full_name="John Smith", email="a@acme.com", employer_domain="acme.com"
    )
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH


def test_no_comparable_signal_on_either_side_is_unverifiable() -> None:
    requested = RequestedIdentity(email="a@acme.com")
    returned = ReturnedIdentity(full_name=None, employer_domain=None, email=None)
    result = validate_identity(requested, returned)
    assert result.verdict == UNVERIFIABLE


def test_provider_returned_a_name_but_domain_was_never_supplied_by_the_lead() -> None:
    """No company_domain on the request and a free-mail-shaped email would
    make the derived expected domain meaningless in production — but here the
    email domain itself is what's being validated, so it still corroborates."""
    requested = RequestedIdentity(email="ahmed@orderii.co", full_name="Ahmed Qais")
    returned = ReturnedIdentity(full_name="Ahmed Qais", employer_domain="orderii.co")
    result = validate_identity(requested, returned)
    assert result.verdict == VERIFIED  # domain (from email) + name both agree


# ---------------------------------------------------------------- name rules --


def test_a_nickname_is_treated_as_a_mismatch_by_design() -> None:
    """'Tobi' vs 'Tobias' is a real, if unwelcome, consequence of "not
    aggressively fuzzy": a nickname table would resolve it, but a nickname
    table can also resolve away a genuine wrong-person match. Landing on
    MISMATCH (not VERIFIED) is the conservative, explainable failure mode —
    it costs one avoidable human review, never a silently wrong score."""
    requested = RequestedIdentity(
        email="tobi@shopify.com", company_domain="shopify.com", full_name="Tobi Lütke"
    )
    returned = ReturnedIdentity(full_name="Tobias Lutke", employer_domain="shopify.com")
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH


def test_matching_last_name_with_diacritic_transliteration_is_verified() -> None:
    requested = RequestedIdentity(
        email="tobi@shopify.com", company_domain="shopify.com", full_name="Tobias Lütke"
    )
    returned = ReturnedIdentity(full_name="Tobias Lutke", employer_domain="shopify.com")
    result = validate_identity(requested, returned)
    assert result.verdict == VERIFIED


def test_a_middle_name_or_initial_does_not_break_a_match() -> None:
    requested = RequestedIdentity(
        email="a@acme.com", company_domain="acme.com", full_name="Patrick Collison"
    )
    returned = ReturnedIdentity(
        full_name="Patrick J. Collison", email="a@acme.com", employer_domain="acme.com"
    )
    result = validate_identity(requested, returned)
    assert result.verdict == VERIFIED


def test_a_single_token_name_cannot_be_keyed_and_reads_as_unknown() -> None:
    requested = RequestedIdentity(email="a@acme.com", company_domain="acme.com", full_name="Cher")
    returned = ReturnedIdentity(full_name="Cher", employer_domain="acme.com")
    result = validate_identity(requested, returned)
    # Domain still corroborates; the single-token name can't form a (first,
    # last) key on either side, so it contributes nothing — not a match, not
    # a mismatch.
    assert result.verdict == PROBABLE


# ------------------------------------------------------------------- explain --


def test_explain_joins_the_reasons_for_the_receipt() -> None:
    requested = RequestedIdentity(
        email="patrick@stripe.com", company_domain="stripe.com", full_name="Patrick Collison"
    )
    returned = ReturnedIdentity(full_name="Patrick Bosmans", employer_domain="stripe.com")
    result = validate_identity(requested, returned)
    assert "does not match" in result.explain()
