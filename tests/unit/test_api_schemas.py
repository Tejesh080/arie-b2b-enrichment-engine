"""Request validation for the ingestion API — no database, no network.

The point of these is not that Pydantic works. It is that the API's idea of a
valid lead and the identity resolver's idea of one are *the same idea*: the
validators call ``arie.identity.normalize`` rather than restating its rules, so
nothing can be accepted at the edge that resolution would later reject.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from arie.api.schemas import IngestLeadRequest


def _minimal(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {"source": "webhook", "email": "ada@acme.com"}
    payload.update(overrides)
    return payload


def test_minimal_payload_is_valid() -> None:
    request = IngestLeadRequest(**_minimal())  # type: ignore[arg-type]
    assert request.source == "webhook"
    assert request.external_ref is None
    assert request.budget_usd_cap is None


@pytest.mark.parametrize(
    "email",
    [
        "not-an-email",  # no @
        "@acme.com",  # empty local part
        "+tag@acme.com",  # local part is only a tag
        "   ",  # blank
    ],
)
def test_unresolvable_emails_are_rejected(email: str) -> None:
    """Rejected by the *normalizer*, not by a regex restated here.

    Each of these is an address `normalize_email` raises on, so accepting any
    of them would push the failure into the ingestion transaction instead of
    answering 422.
    """
    with pytest.raises(ValidationError):
        IngestLeadRequest(**_minimal(email=email))  # type: ignore[arg-type]


def test_email_is_stored_unnormalized_for_the_resolver_to_handle() -> None:
    """Validation proves it *can* be normalized; it doesn't do the normalizing.

    Identity resolution owns that transform. Normalizing twice, in two places,
    is how the two drift apart.
    """
    request = IngestLeadRequest(**_minimal(email="Ada+Newsletter@ACME.com"))  # type: ignore[arg-type]
    assert request.email == "Ada+Newsletter@ACME.com"


def test_unresolvable_domain_is_rejected() -> None:
    with pytest.raises(ValidationError):
        IngestLeadRequest(**_minimal(company_domain="https://"))  # type: ignore[arg-type]


def test_messy_but_resolvable_domain_is_accepted() -> None:
    request = IngestLeadRequest(
        **_minimal(company_domain="https://www.Acme.com/pricing?ref=x")  # type: ignore[arg-type]
    )
    assert request.company_domain == "https://www.Acme.com/pricing?ref=x"


def test_blank_source_is_rejected() -> None:
    """`source` is half the deduplication key; an empty one silently widens it."""
    with pytest.raises(ValidationError):
        IngestLeadRequest(**_minimal(source=""))  # type: ignore[arg-type]


@pytest.mark.parametrize("cap", [Decimal("0"), Decimal("-1.00")])
def test_non_positive_budget_cap_is_rejected(cap: Decimal) -> None:
    """A cap of zero makes every provider unbuyable, which is a
    silently-does-nothing pipeline rather than an error anyone would notice."""
    with pytest.raises(ValidationError):
        IngestLeadRequest(**_minimal(budget_usd_cap=cap))  # type: ignore[arg-type]


def test_to_command_carries_every_field_through() -> None:
    """A field added to the request model but not to the command is a field
    silently dropped on the way into the database."""
    request = IngestLeadRequest(
        source="n8n",
        email="ada@acme.com",
        external_ref="crm-7",
        company_domain="acme.com",
        company_name="Acme Inc",
        full_name="Ada Lovelace",
        title="VP Engineering",
        budget_usd_cap=Decimal("2.50"),
    )
    command = request.to_command()

    assert command.source == "n8n"
    assert command.email == "ada@acme.com"
    assert command.external_ref == "crm-7"
    assert command.company_domain == "acme.com"
    assert command.company_name == "Acme Inc"
    assert command.full_name == "Ada Lovelace"
    assert command.title == "VP Engineering"
    assert command.budget_usd_cap == Decimal("2.50")
