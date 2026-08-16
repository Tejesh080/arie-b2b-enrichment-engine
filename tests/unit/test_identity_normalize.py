"""Deterministic normalization for identity resolution.

No database here — these pin the pure string transforms that
arie.identity.resolver relies on for exact matching. The live,
data-driven measurement of how well this actually unifies the eval dataset's
ambiguous-identity subset lives in
tests/integration/test_identity_resolution_integration.py instead.
"""

from __future__ import annotations

import pytest

from arie.identity.normalize import (
    domain_from_email,
    normalize_company_name,
    normalize_domain,
    normalize_email,
)

# --- normalize_domain ---------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "acme.com",
        "ACME.COM",
        "  acme.com  ",
        "https://acme.com",
        "http://acme.com",
        "https://www.acme.com",
        "www.acme.com",
        "https://acme.com/pricing?ref=abc",
        "acme.com:443",
        "https://ACME.com/",
    ],
)
def test_domain_variants_normalize_identically(raw: str) -> None:
    assert normalize_domain(raw) == "acme.com"


def test_domain_keeps_subdomains_other_than_www() -> None:
    assert normalize_domain("https://app.acme.com") == "app.acme.com"


@pytest.mark.parametrize("raw", ["", "   ", "https://", "www."])
def test_blank_domain_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_domain(raw)


# --- normalize_email -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "jane@acme.com",
        "JANE@ACME.COM",
        "  jane@acme.com  ",
        "jane@www.acme.com",
        "jane+newsletter@acme.com",
        "jane+xyz123@ACME.COM",
    ],
)
def test_email_variants_normalize_identically(raw: str) -> None:
    assert normalize_email(raw) == "jane@acme.com"


def test_email_does_not_fold_dots_in_local_part() -> None:
    """Dot-folding is Gmail-specific, not a universal email rule — must not apply it."""
    assert normalize_email("jane.doe@acme.com") == "jane.doe@acme.com"
    assert normalize_email("janedoe@acme.com") == "janedoe@acme.com"
    assert normalize_email("jane.doe@acme.com") != normalize_email("janedoe@acme.com")


@pytest.mark.parametrize("raw", ["", "   ", "not-an-email", "@acme.com", "jane@", "+tag@acme.com"])
def test_invalid_or_blank_email_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_email(raw)


def test_plus_only_tag_leaves_the_base_local_part() -> None:
    """ "jane+" has a non-empty local part ("jane") once the tag is dropped."""
    assert normalize_email("jane+@acme.com") == "jane@acme.com"


# --- domain_from_email ---------------------------------------------------------


def test_domain_from_work_email() -> None:
    assert domain_from_email("jane@acme.com") == "acme.com"


@pytest.mark.parametrize(
    "email", ["jane@gmail.com", "jane@yahoo.com", "jane@outlook.com", "jane@icloud.com"]
)
def test_domain_from_free_mail_is_none(email: str) -> None:
    assert domain_from_email(email) is None


# --- normalize_company_name -----------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "Acme",
        "Acme Inc",
        "Acme Inc.",
        "ACME",
        "acme",
        "  Acme  ",
        "Acme Corp",
        "Acme Corp.",
        "Acme Corporation",
        "Acme LLC",
        "Acme Ltd",
        "Acme Limited",
        "Acme Co",
        "Acme Company",
        "Acme, Inc.",
    ],
)
def test_company_name_variants_normalize_identically(raw: str) -> None:
    assert normalize_company_name(raw) == "acme"


def test_strips_multiple_trailing_suffixes() -> None:
    assert normalize_company_name("Acme Corp Inc") == "acme"


def test_ampersand_becomes_and_not_dropped() -> None:
    assert normalize_company_name("Smith & Co") == "smith and"


def test_collapses_internal_whitespace() -> None:
    assert normalize_company_name("Acme    Rockets") == "acme rockets"


def test_distinct_companies_stay_distinct() -> None:
    assert normalize_company_name("Acme Inc") != normalize_company_name("Acme Rockets Inc")


@pytest.mark.parametrize("raw", ["", "   ", "Inc", "Inc.", "LLC"])
def test_blank_or_suffix_only_name_raises(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_company_name(raw)
