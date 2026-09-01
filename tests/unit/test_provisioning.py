"""Self-service organization provisioning (Productization M6 Part 10/11) —
the pure parts of `arie.provisioning`: slug generation and the empty-name
guard, which raises before touching the connection. Atomicity/race/DB
behavior is covered by tests/integration/test_provisioning_integration.py.
"""

from __future__ import annotations

from typing import cast
from uuid import UUID

import psycopg
import pytest

from arie.provisioning import InvalidOrganizationNameError, _slugify, create_customer_organization

_UNUSED_CONN = cast(psycopg.Connection, None)
_UNUSED_USER_ID = cast(UUID, "not-used")


def test_slugify_lowercases_and_hyphenates() -> None:
    assert _slugify("Acme Corp") == "acme-corp"


def test_slugify_strips_non_alphanumeric_runs_to_single_hyphen() -> None:
    assert _slugify("  Acme & Co.,  Ltd!! ") == "acme-co-ltd"


def test_slugify_falls_back_to_a_default_for_an_all_symbol_name() -> None:
    assert _slugify("!!!") == "organization"


def test_create_customer_organization_rejects_an_empty_name_before_touching_the_connection() -> (
    None
):
    with pytest.raises(InvalidOrganizationNameError):
        create_customer_organization(
            _UNUSED_CONN, owner_user_id=_UNUSED_USER_ID, organization_name="   "
        )
