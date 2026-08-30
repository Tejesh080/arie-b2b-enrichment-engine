"""Organization settings (Productization M4 Part 1) — the parts of
`arie.organizations` and `arie.api.schemas.UpdateOrganizationRequest` that
don't need a live database: field validation happens before any query is
built, so a dummy `None` connection proves it never gets touched.

API-level behavior (GET/PATCH, role gating, RLS, tenant isolation) is covered
by tests/integration/test_organizations_integration.py instead.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

import psycopg
import pytest
from pydantic import ValidationError

from arie.api.schemas import UpdateOrganizationRequest
from arie.organizations import (
    InvalidOrganizationSettingsError,
    update_organization,
    validate_timezone,
)

_UNUSED_CONN = cast(psycopg.Connection, None)
_UNUSED_ORG_ID = cast(UUID, "not-used")
"""Field validation in `update_organization` raises before either argument is
ever touched — see the three tests below — so a placeholder of the wrong
runtime type is fine here and avoids a real connection/UUID just to prove
that."""


def test_validate_timezone_accepts_a_real_iana_zone() -> None:
    validate_timezone("Australia/Adelaide")  # does not raise


def test_validate_timezone_rejects_an_unknown_zone() -> None:
    with pytest.raises(InvalidOrganizationSettingsError, match="unknown timezone"):
        validate_timezone("Mars/Olympus_Mons")


def test_update_organization_rejects_an_unknown_field_before_touching_the_connection() -> None:
    with pytest.raises(InvalidOrganizationSettingsError, match="slug"):
        update_organization(_UNUSED_CONN, organization_id=_UNUSED_ORG_ID, updates={"slug": "x"})


def test_update_organization_rejects_an_empty_name_before_touching_the_connection() -> None:
    with pytest.raises(InvalidOrganizationSettingsError, match="non-empty"):
        update_organization(_UNUSED_CONN, organization_id=_UNUSED_ORG_ID, updates={"name": "   "})


def test_update_organization_rejects_an_unknown_timezone_before_touching_the_connection() -> None:
    with pytest.raises(InvalidOrganizationSettingsError, match="unknown timezone"):
        update_organization(
            _UNUSED_CONN,
            organization_id=_UNUSED_ORG_ID,
            updates={"timezone": "Mars/Olympus_Mons"},
        )


# ------------------------------------------------------------- request schema --


def test_update_organization_request_rejects_an_empty_body() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        UpdateOrganizationRequest()


def test_update_organization_request_accepts_a_single_field() -> None:
    request = UpdateOrganizationRequest(timezone="Australia/Adelaide")
    assert request.model_dump(exclude_unset=True) == {"timezone": "Australia/Adelaide"}


def test_update_organization_request_rejects_an_unknown_timezone() -> None:
    with pytest.raises(ValidationError, match="unknown timezone"):
        UpdateOrganizationRequest(timezone="Mars/Olympus_Mons")


def test_update_organization_request_rejects_an_unnormalizable_domain() -> None:
    """`normalize_domain` only requires a non-blank result after stripping
    scheme/path/port — a whitespace-only string is the one input it actually
    rejects; see `arie.identity.normalize.normalize_domain`."""
    with pytest.raises(ValidationError):
        UpdateOrganizationRequest(company_domain="   ")


def test_update_organization_request_distinguishes_omitted_from_explicit_null() -> None:
    """`exclude_unset` must tell "never mentioned" from "sent as null" apart —
    the whole reason `update_organization` can use a plain `dict` rather than
    a sentinel value to know whether to clear `company_domain`."""
    omitted = UpdateOrganizationRequest(name="Acme")
    assert "company_domain" not in omitted.model_dump(exclude_unset=True)

    cleared = UpdateOrganizationRequest(name="Acme", company_domain=None)
    dumped = cleared.model_dump(exclude_unset=True)
    assert "company_domain" in dumped
    assert dumped["company_domain"] is None
