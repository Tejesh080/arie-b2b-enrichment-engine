"""Identity resolution — turning raw lead data into stable company/person rows.

This is what M1 needed that M0 never modelled: the benchmark takes identity as
given (``canonical_domain``/``email`` are already clean, stable keys baked into
the eval dataset). A real inbound lead is not that tidy — company names arrive
misspelled, capitalised differently, or as a bare name with no domain at all.
This module is the boundary that turns that raw input into the same kind of
clean, stable key the rest of the system already assumes.

Company-level evidence sharing (``arie.evidence.store.PostgresEvidenceStore``)
depends entirely on this resolver returning the *same* ``company_id`` for the
*same* real company every time it is asked, however the company's name happens
to be spelled on a given lead. That invariant — not fuzzy matching — is the
actual point of this module, and is what
``tests/integration/test_identity_resolution_integration.py`` exists to pin.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.identity.normalize import (
    domain_from_email,
    normalize_company_name,
    normalize_domain,
    normalize_email,
)

_INSERT_COMPANY_BY_DOMAIN = """
    INSERT INTO companies (canonical_domain, name)
    VALUES (%(domain)s, %(name)s)
    ON CONFLICT (canonical_domain) DO UPDATE SET updated_at = now()
    RETURNING company_id, canonical_domain, (xmax = 0) AS created
"""

# Serializes concurrent name-only resolutions of the *same* normalized name so
# the check-then-insert below can't race into two rows for one company. Not
# needed on the domain path -- `ON CONFLICT` already makes that atomic.
_LOCK_NORMALIZED_NAME = "SELECT pg_advisory_xact_lock(hashtext(%(normalized_name)s))"

# Scoped to `canonical_domain IS NULL`: a name-only mention must never silently
# attach itself to a company that's already known by domain. Less evidence
# should mean *more* caution, not a shortcut into an existing, better-identified
# row.
_FIND_COMPANY_BY_NAME = """
    SELECT company_id, canonical_domain
    FROM companies
    WHERE canonical_domain IS NULL AND normalized_name = %(normalized_name)s
    LIMIT 1
"""

_INSERT_COMPANY_BY_NAME = """
    INSERT INTO companies (name, normalized_name)
    VALUES (%(name)s, %(normalized_name)s)
    RETURNING company_id, canonical_domain
"""

_UPSERT_PERSON = """
    INSERT INTO persons (canonical_email, full_name, title, company_id, organization_id)
    VALUES (%(email)s, %(full_name)s, %(title)s, %(company_id)s, %(organization_id)s)
    ON CONFLICT (organization_id, canonical_email) DO UPDATE SET updated_at = now()
    RETURNING person_id, canonical_email, company_id, (xmax = 0) AS created
"""


@dataclass(frozen=True)
class ResolvedCompany:
    company_id: UUID
    canonical_domain: str | None
    created: bool
    """Whether this call inserted a new row, as opposed to matching one that already existed."""


@dataclass(frozen=True)
class ResolvedPerson:
    person_id: UUID
    canonical_email: str
    company_id: UUID | None
    created: bool


class IdentityResolver:
    """Deterministic, exact-match identity resolution against companies/persons.

    Domain is the only key trusted enough to carry a UNIQUE constraint
    (``companies.canonical_domain``); the name-only fallback below is a
    best-effort match among domain-less companies, not a guarantee — see
    ``migrations/0003_identity_resolution.sql`` for why it isn't (and can't
    safely be) unique itself.
    """

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    @classmethod
    def connect(cls, conninfo: str, *, min_size: int = 1, max_size: int = 10) -> IdentityResolver:
        pool = ConnectionPool(conninfo, min_size=min_size, max_size=max_size, open=True)
        return cls(pool)

    def close(self) -> None:
        self._pool.close()

    def __enter__(self) -> IdentityResolver:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transaction-joining variants ---------------------------------------
    #
    # Each `*_in` method does the work on a caller-provided connection and does
    # not commit; the public method of the same name wraps it with its own
    # connection and commit, exactly as it always behaved. The split exists for
    # the ingestion API, which resolves identity and inserts the lead in one
    # transaction so a failure anywhere leaves no partial trace of the request.
    #
    # A note on the advisory lock in the name path: `pg_advisory_xact_lock` is
    # transaction-scoped, so when identity resolution shares the ingestion
    # transaction the lock is held until ingestion commits rather than being
    # released immediately. That is longer, but ingestion is a handful of
    # statements with no network call in it, and the lock is keyed on one
    # normalized company name — it serializes concurrent ingests of the *same*
    # domain-less company, which is precisely the race it exists to prevent.

    def resolve_company_in(
        self, conn: psycopg.Connection, *, domain: str | None = None, name: str | None = None
    ) -> ResolvedCompany:
        """Resolve a company on a caller-provided connection. Does not commit."""
        if domain is None and name is None:
            raise ValueError("resolve_company requires a domain, a name, or both")

        if domain is not None:
            normalized_domain = normalize_domain(domain)
            display_name = name if name is not None else normalized_domain
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    _INSERT_COMPANY_BY_DOMAIN,
                    {"domain": normalized_domain, "name": display_name},
                )
                row = cur.fetchone()
                assert row is not None
            return ResolvedCompany(
                company_id=row["company_id"],
                canonical_domain=row["canonical_domain"],
                created=row["created"],
            )

        assert name is not None  # narrows for mypy; the guard above already enforced it
        normalized_name = normalize_company_name(name)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(_LOCK_NORMALIZED_NAME, {"normalized_name": normalized_name})
            cur.execute(_FIND_COMPANY_BY_NAME, {"normalized_name": normalized_name})
            existing = cur.fetchone()
            if existing is not None:
                return ResolvedCompany(
                    company_id=existing["company_id"],
                    canonical_domain=existing["canonical_domain"],
                    created=False,
                )

            cur.execute(_INSERT_COMPANY_BY_NAME, {"name": name, "normalized_name": normalized_name})
            row = cur.fetchone()
            assert row is not None
            return ResolvedCompany(
                company_id=row["company_id"], canonical_domain=row["canonical_domain"], created=True
            )

    def resolve_person_in(
        self,
        conn: psycopg.Connection,
        *,
        email: str,
        organization_id: UUID,
        full_name: str | None = None,
        title: str | None = None,
        company_id: UUID | None = None,
    ) -> ResolvedPerson:
        """Resolve a person on a caller-provided connection. Does not commit.

        `organization_id` is required, unlike `company_id` — Productization M1
        made `persons` tenant-owned (`companies` stays the shared, global
        identity layer; see `migrations/0012_organizations_and_members.sql`),
        so two organizations independently ingesting the same email must
        resolve to two distinct rows rather than colliding on the old global
        `canonical_email` unique constraint and silently overwriting each
        other's name/title on `ON CONFLICT`.
        """
        normalized_email = normalize_email(email)
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _UPSERT_PERSON,
                {
                    "email": normalized_email,
                    "full_name": full_name,
                    "title": title,
                    "company_id": company_id,
                    "organization_id": organization_id,
                },
            )
            row = cur.fetchone()
            assert row is not None
        return ResolvedPerson(
            person_id=row["person_id"],
            canonical_email=row["canonical_email"],
            company_id=row["company_id"],
            created=row["created"],
        )

    def resolve_lead_in(
        self,
        conn: psycopg.Connection,
        *,
        person_email: str,
        organization_id: UUID,
        company_domain: str | None = None,
        company_name: str | None = None,
        person_full_name: str | None = None,
        person_title: str | None = None,
    ) -> tuple[ResolvedCompany, ResolvedPerson]:
        """Resolve company and contact on a caller-provided connection. Does not commit.

        `company_id` is shared, global identity (see `resolve_company_in`);
        `organization_id` scopes only the person row — see
        `resolve_person_in`'s docstring for why.
        """
        domain = company_domain or domain_from_email(person_email)
        company = self.resolve_company_in(conn, domain=domain, name=company_name)
        person = self.resolve_person_in(
            conn,
            email=person_email,
            organization_id=organization_id,
            full_name=person_full_name,
            title=person_title,
            company_id=company.company_id,
        )
        return company, person

    # -- self-committing public API -----------------------------------------

    def resolve_company(
        self, *, domain: str | None = None, name: str | None = None
    ) -> ResolvedCompany:
        """Resolve a company by domain if given, else by name among domain-less companies.

        The domain path is always safe to call repeatedly: it's a plain
        UPSERT on a unique key. The name-only path is the one Step 7 measures
        rather than trusts — see the ambiguous-identity tests.
        """
        with self._pool.connection() as conn:
            resolved = self.resolve_company_in(conn, domain=domain, name=name)
            conn.commit()
        return resolved

    def resolve_person(
        self,
        *,
        email: str,
        organization_id: UUID,
        full_name: str | None = None,
        title: str | None = None,
        company_id: UUID | None = None,
    ) -> ResolvedPerson:
        """Resolve a person by email — the one identifier this schema treats as reliable.

        Unlike companies, there's no name-only fallback: two different people
        sharing a name is far more common than two different companies sharing
        a normalized name, so a name-only match would be materially unsafe.
        """
        with self._pool.connection() as conn:
            resolved = self.resolve_person_in(
                conn,
                email=email,
                organization_id=organization_id,
                full_name=full_name,
                title=title,
                company_id=company_id,
            )
            conn.commit()
        return resolved

    def resolve_lead(
        self,
        *,
        person_email: str,
        organization_id: UUID,
        company_domain: str | None = None,
        company_name: str | None = None,
        person_full_name: str | None = None,
        person_title: str | None = None,
    ) -> tuple[ResolvedCompany, ResolvedPerson]:
        """Resolve a company and its contact together — the shape a real lead arrives in.

        Falls back to the contact's work-email domain when no explicit company
        domain is given, skipping free-mail providers (see
        ``arie.identity.normalize.domain_from_email``) so a Gmail sender never
        gets treated as a company.
        """
        with self._pool.connection() as conn:
            resolved = self.resolve_lead_in(
                conn,
                person_email=person_email,
                organization_id=organization_id,
                company_domain=company_domain,
                company_name=company_name,
                person_full_name=person_full_name,
                person_title=person_title,
            )
            conn.commit()
        return resolved
