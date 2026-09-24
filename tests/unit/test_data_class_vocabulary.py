"""Provenance and cost-basis vocabularies, and the rules that make them safe.

Both exist because one production organization's 194 leads turned out to be
193 pieces of development exhaust and one real customer lead, and because the
fictional prices attached to that exhaust had consumed the organization's
entire real-money monthly allowance.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from arie.data_class import PRODUCTION_ONLY_SQL, LeadDataClass, assignable_by_caller
from arie.ledger.cost_basis import (
    ACTUAL_SPEND_SQL,
    ESTIMATED_LIVE_SQL,
    MODELLED_SPEND_SQL,
    CostBasis,
    is_real_provider_call,
)

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


# --- nothing may claim to be a customer's data -------------------------------


def test_production_cannot_be_requested() -> None:
    """The one rule the whole mechanism rests on. `production` is reached only
    by the server's default, so a harness cannot talk its way into the numbers
    a customer reads."""
    with pytest.raises(ValueError, match="cannot be requested"):
        assignable_by_caller("production")


@pytest.mark.parametrize(
    "klass",
    [c for c in LeadDataClass if c is not LeadDataClass.PRODUCTION],
)
def test_every_other_class_is_assignable(klass: LeadDataClass) -> None:
    assert assignable_by_caller(klass.value) is klass


@pytest.mark.parametrize("value", ["", "prod", "PRODUCTION", "real", "customer", "none"])
def test_an_unknown_class_is_refused(value: str) -> None:
    with pytest.raises(ValueError, match="unknown data class"):
        assignable_by_caller(value)


def test_the_refusal_message_never_advertises_production() -> None:
    with pytest.raises(ValueError) as caught:
        assignable_by_caller("nonsense")
    assert "production" not in str(caught.value)


# --- unknown stays unknown ---------------------------------------------------


def test_unknown_is_not_production() -> None:
    """Nine leads in the audited organization had no establishable provenance.
    Two are named `probe6.com` and `probe7.com` and are obviously fixtures —
    but obviously is not evidence, and guessing from a domain name is the
    blacklist this column exists to replace."""
    assert LeadDataClass.UNKNOWN != LeadDataClass.PRODUCTION  # type: ignore[comparison-overlap]
    assert LeadDataClass.UNKNOWN.value not in PRODUCTION_ONLY_SQL


def test_only_production_passes_the_product_filter() -> None:
    for klass in LeadDataClass:
        included = f"l.data_class = '{klass.value}'" == PRODUCTION_ONLY_SQL
        assert included is (klass is LeadDataClass.PRODUCTION), klass


# --- the vocabularies match the schema ---------------------------------------


def _check_constraint_values(filename: str, constraint: str) -> set[str]:
    """The literals inside `ADD CONSTRAINT <name> CHECK (...)`.

    Anchored on ADD specifically: the same name also appears in the
    `DROP CONSTRAINT IF EXISTS` line above it, and matching that one would
    read an empty statement and pass vacuously.
    """
    sql = (MIGRATIONS / filename).read_text(encoding="utf-8")
    match = re.search(rf"ADD CONSTRAINT {constraint} CHECK \((.*?)\);", sql, re.DOTALL)
    assert match is not None, f"{constraint} not found in {filename}"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


def test_lead_data_class_matches_its_check_constraint() -> None:
    """A value the enum allows and the constraint rejects is an insert that
    fails in production and nowhere else."""
    allowed = _check_constraint_values("0042_lead_data_class.sql", "leads_data_class_check")

    assert {c.value for c in LeadDataClass} <= allowed


def test_cost_basis_matches_its_check_constraint() -> None:
    allowed = _check_constraint_values(
        "0043_cost_basis_vocabulary.sql", "provider_calls_cost_basis_check"
    )

    assert {c.value for c in CostBasis} <= allowed


# --- three kinds of number, kept apart ---------------------------------------


def test_a_simulated_call_is_not_a_real_provider_call() -> None:
    assert is_real_provider_call(CostBasis.SIMULATED_CATALOGUE) is False


@pytest.mark.parametrize(
    "basis",
    [
        CostBasis.MODELLED_CREDIT_EQUIVALENT,
        CostBasis.MODELLED_LIST_PRICE,
        CostBasis.VENDOR_BILLED,
    ],
)
def test_every_priced_basis_is_a_real_provider_call(basis: CostBasis) -> None:
    """Including the two modelled ones: how a call was *priced* says nothing
    about whether it happened. A free-credit call is still a real call."""
    assert is_real_provider_call(basis) is True


def test_a_cache_hit_is_not_a_real_provider_call() -> None:
    """No call was made, so there is nothing to price and nothing to bill."""
    assert is_real_provider_call(None) is False


def test_an_unrecognised_basis_is_not_assumed_real() -> None:
    """A basis this module does not know is not evidence that money moved, and
    guessing "real" is the direction that overstates spend."""
    assert is_real_provider_call("something_new") is False


def test_the_three_sql_predicates_are_distinct() -> None:
    assert MODELLED_SPEND_SQL != ESTIMATED_LIVE_SQL != ACTUAL_SPEND_SQL


def test_modelled_spend_captures_a_null_basis() -> None:
    """A cache hit records no basis and costs nothing. It belongs on the side
    that cannot consume an allowance, so the predicate uses IS NOT DISTINCT
    FROM rather than =."""
    assert "IS NOT DISTINCT FROM" in MODELLED_SPEND_SQL


def test_actual_spend_reads_money_not_basis() -> None:
    """`vendor_billed` with no figure has nothing to add; a free-credit call
    reports 0 and means it. Both are answered by the column, not the label."""
    assert "actual_cost_usd" in ACTUAL_SPEND_SQL
    assert "cost_basis" not in ACTUAL_SPEND_SQL


def test_simulated_catalogue_is_excluded_from_every_real_predicate() -> None:
    assert CostBasis.SIMULATED_CATALOGUE.value not in ESTIMATED_LIVE_SQL
    assert CostBasis.SIMULATED_CATALOGUE.value in MODELLED_SPEND_SQL
