"""Tenancy constants shared by migrations, application code, and tests.

Productization M1 — see ``migrations/0012_organizations_and_members.sql``
onward for the schema this supports.
"""

from __future__ import annotations

from uuid import UUID

LEGACY_ORGANIZATION_ID: UUID = UUID("00000000-0000-0000-0000-000000000001")
"""The well-known organization every pre-tenancy row was backfilled into by
``migrations/0014_legacy_organization_backfill.sql``. A fixed UUID, not a
lookup, so application code and tests can reference it as a constant."""
