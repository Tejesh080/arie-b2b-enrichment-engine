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


def test_a_conservative_nickname_variant_on_a_matching_surname_is_probable() -> None:
    """The real result from the validation-20 run: Hunter honestly resolved
    tobi@shopify.com to Tobias Lutke — Tobi Lütke's own legal first name.
    Reading that as a full MISMATCH, indistinguishable from the genuinely
    different Patrick Bosmans two cases below, was a false negative. "Tobi" is
    an exact prefix of "Tobias" and the surname matches exactly, so this caps
    at PROBABLE (domain agreeing, name treated as no signal at all) — not
    VERIFIED (no free pass for a non-exact name), not MISMATCH."""
    requested = RequestedIdentity(
        email="tobi@shopify.com", company_domain="shopify.com", full_name="Tobi Lütke"
    )
    returned = ReturnedIdentity(full_name="Tobias Lutke", employer_domain="shopify.com")
    result = validate_identity(requested, returned)
    assert result.verdict == PROBABLE
    assert any("variant" in reason for reason in result.reasons)


def test_a_first_name_variant_with_no_agreeing_domain_is_unverifiable_not_probable() -> None:
    """The variant tier only ever substitutes for "no name at all" — it needs
    a genuinely agreeing domain to reach PROBABLE, same as any other
    single-signal case. No domain to check means nothing corroborates."""
    requested = RequestedIdentity(email="tobi@shopify.com", full_name="Tobi Lütke")
    returned = ReturnedIdentity(full_name="Tobias Lutke")
    result = validate_identity(requested, returned)
    assert result.verdict == UNVERIFIABLE


def test_patrick_collison_vs_patrick_bosmans_stays_a_mismatch() -> None:
    """The variant tier must never soften a genuine surname disagreement —
    "Collison" is not a prefix or variant of "Bosmans" by any reading, so this
    is unaffected by the Tobi/Tobias fix and remains exactly what it always
    was: a different person at the same company."""
    requested = RequestedIdentity(
        email="patrick@stripe.com", company_domain="stripe.com", full_name="Patrick Collison"
    )
    returned = ReturnedIdentity(full_name="Patrick Bosmans", employer_domain="stripe.com")
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH


def test_zte_zhang_zheng_reversed_name_order_stays_a_mismatch() -> None:
    """The real ZTE result: requested "Zheng Zhang", Hunter returned "Zhang
    Zheng" for zhang.zheng@zte.com.cn — same two tokens, reversed order, and a
    wildly implausible title ("Model") for the RFC author this address
    belongs to. (first, last) key comparison treats "Zheng Zhang" and "Zhang
    Zheng" as different surnames ("zhang" vs "zheng"), so the variant check
    never even applies — this must stay MISMATCH."""
    requested = RequestedIdentity(
        email="zhang.zheng@zte.com.cn", company_domain="zte.com.cn", full_name="Zheng Zhang"
    )
    returned = ReturnedIdentity(full_name="Zhang Zheng", employer_domain="zte.com.cn")
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH


def test_comcast_lee_yiu_reversed_name_order_stays_a_mismatch() -> None:
    """The real Comcast result: requested "Yiu L. Lee", Hunter returned "Lee
    Yiu" for yiu_lee@comcast.com — reversed given/family order and a "Producer"
    title, nothing like the VP of System Architecture this address belongs
    to. Surnames compare unequal ("lee" vs "yiu"), so this must stay MISMATCH,
    same as ZTE."""
    requested = RequestedIdentity(
        email="yiu_lee@comcast.com", company_domain="comcast.com", full_name="Yiu L. Lee"
    )
    returned = ReturnedIdentity(full_name="Lee Yiu", employer_domain="comcast.com")
    result = validate_identity(requested, returned)
    assert result.verdict == MISMATCH


def test_a_two_letter_first_name_prefix_does_not_qualify_as_a_variant() -> None:
    """The three-character floor: "Al" is a prefix of both "Alex" and
    "Albert", two names that are not the same person. Below the floor, this
    must fall through to an ordinary MISMATCH, not a lucky PROBABLE."""
    requested = RequestedIdentity(
        email="al@acme.com", company_domain="acme.com", full_name="Al Harrison"
    )
    returned = ReturnedIdentity(full_name="Albert Harrison", employer_domain="acme.com")
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
