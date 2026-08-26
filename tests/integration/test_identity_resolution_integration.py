"""Identity resolution against a real Postgres database.

Requires TEST_DATABASE_URL; skipped otherwise (see
conftest.py). Four concerns, each load-bearing for a different reason:

- **Deduplication** — the same real company/person, spelled differently on
  different leads, must resolve to one row, not one row per spelling.
- **Isolation** — genuinely different companies/persons must never collide,
  including across the domain-known / name-only boundary.
- **Cache sharing** — the actual payoff of Step 7: a second contact at an
  already-known company reads the first contact's evidence for free, via
  arie.evidence.store.PostgresEvidenceStore keyed on the resolved company_id.
- **Ambiguous-identity measurement** — not a target to hit, a number to
  report. Per docs/architecture.md, this is what would turn "add Splink"
  from taste into a data-driven decision; it is deliberately not gated on a
  threshold here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from tests.integration.conftest import IdentityCleanup

from arie.core.types import Evidence
from arie.evalgen.schema import EvalLead
from arie.evidence.store import PostgresEvidenceStore
from arie.identity.normalize import normalize_company_name, normalize_domain
from arie.identity.resolver import IdentityResolver

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 16, 12, 0, 0, tzinfo=UTC)


def _unique_domain(label: str) -> str:
    return f"{label}-{uuid4().hex[:10]}.com"


# --- deduplication -------------------------------------------------------------


def test_company_resolves_to_same_id_across_domain_variants(
    identity_resolver: IdentityResolver,
    db_conn: psycopg.Connection,
    cleanup_identity: IdentityCleanup,
) -> None:
    domain = _unique_domain("acme")
    cleanup_identity.domains.append(domain)

    raw_variants = [f"https://{domain}", f"HTTPS://WWW.{domain.upper()}/", f"  {domain}  "]
    resolved_ids = {
        identity_resolver.resolve_company(domain=v, name="Acme").company_id for v in raw_variants
    }

    assert len(resolved_ids) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM companies WHERE canonical_domain = %s", (domain,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


def test_resolve_company_created_flag_is_true_only_once(
    identity_resolver: IdentityResolver, cleanup_identity: IdentityCleanup
) -> None:
    domain = _unique_domain("idempotent")
    cleanup_identity.domains.append(domain)

    first = identity_resolver.resolve_company(domain=domain, name="Idempotent Co")
    second = identity_resolver.resolve_company(domain=domain, name="Idempotent Co")

    assert first.created is True
    assert second.created is False
    assert first.company_id == second.company_id


def test_person_resolves_to_same_id_across_email_variants(
    identity_resolver: IdentityResolver,
    db_conn: psycopg.Connection,
    cleanup_identity: IdentityCleanup,
) -> None:
    domain = _unique_domain("personco")
    email = f"jane@{domain}"
    cleanup_identity.domains.append(domain)
    cleanup_identity.emails.append(email)

    company = identity_resolver.resolve_company(domain=domain, name="Person Co")

    raw_variants = [f"Jane@{domain.upper()}", f"  jane@{domain}  ", f"jane+newsletter@{domain}"]
    resolved_ids = {
        identity_resolver.resolve_person(
            email=v, full_name="Jane Doe", company_id=company.company_id
        ).person_id
        for v in raw_variants
    }

    assert len(resolved_ids) == 1
    with db_conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM persons WHERE canonical_email = %s", (email,))
        row = cur.fetchone()
        assert row is not None
        assert row[0] == 1


# --- isolation -------------------------------------------------------------


def test_different_domains_never_merge(
    identity_resolver: IdentityResolver, cleanup_identity: IdentityCleanup
) -> None:
    domain_a, domain_b = _unique_domain("alpha"), _unique_domain("beta")
    cleanup_identity.domains.extend([domain_a, domain_b])

    a = identity_resolver.resolve_company(domain=domain_a, name="Alpha")
    b = identity_resolver.resolve_company(domain=domain_b, name="Beta")

    assert a.company_id != b.company_id


def test_different_emails_never_merge(
    identity_resolver: IdentityResolver, cleanup_identity: IdentityCleanup
) -> None:
    domain = _unique_domain("isoco")
    cleanup_identity.domains.append(domain)
    company = identity_resolver.resolve_company(domain=domain, name="Iso Co")

    email_a, email_b = f"alice@{domain}", f"bob@{domain}"
    cleanup_identity.emails.extend([email_a, email_b])

    a = identity_resolver.resolve_person(email=email_a, company_id=company.company_id)
    b = identity_resolver.resolve_person(email=email_b, company_id=company.company_id)

    assert a.person_id != b.person_id


def test_name_only_company_never_matches_a_domain_known_company(
    identity_resolver: IdentityResolver, cleanup_identity: IdentityCleanup
) -> None:
    """Less evidence must mean more caution, not a shortcut into a better-identified row."""
    label = uuid4().hex[:10]
    domain = _unique_domain(label)
    name = f"Widgets {label} Inc"
    cleanup_identity.domains.append(domain)
    cleanup_identity.company_names.append(normalize_company_name(name))

    known = identity_resolver.resolve_company(domain=domain, name=name)
    name_only = identity_resolver.resolve_company(domain=None, name=name)

    assert name_only.company_id != known.company_id
    assert name_only.canonical_domain is None


# --- cache sharing -------------------------------------------------------------


def test_two_contacts_at_same_company_share_fresh_evidence(
    identity_resolver: IdentityResolver,
    evidence_store: PostgresEvidenceStore,
    cleanup_identity: IdentityCleanup,
    cleanup_evidence: list[UUID],
) -> None:
    domain = _unique_domain("shared")
    cleanup_identity.domains.append(domain)
    email_a, email_b = f"alice@{domain}", f"bob@{domain}"
    cleanup_identity.emails.extend([email_a, email_b])

    company_a, _person_a = identity_resolver.resolve_lead(
        person_email=email_a,
        company_domain=domain,
        company_name="Shared Co",
        person_full_name="Alice A",
    )
    # Second contact, submitted with a differently-spelled domain.
    company_b, _person_b = identity_resolver.resolve_lead(
        person_email=email_b,
        company_domain=f"HTTPS://WWW.{domain.upper()}/",
        company_name="Shared Co",
        person_full_name="Bob B",
    )

    assert company_a.company_id == company_b.company_id
    cleanup_evidence.append(company_a.company_id)

    # Contact A's enrichment call writes company-level evidence.
    evidence_store.put(
        Evidence(
            entity_type="company",
            entity_id=company_a.company_id,
            field_name="industry",
            value="fintech",
            source="dns_web",
            confidence=0.9,
            ttl_seconds=3600,
            fetched_at=NOW,
        )
    )

    # Contact B benefits without any provider call of their own.
    fresh = evidence_store.get_all_fresh(
        "company", company_b.company_id, now=NOW + timedelta(minutes=1)
    )
    assert {e.field_name: e.value for e in fresh} == {"industry": "fintech"}


# --- ambiguous-identity measurement ---------------------------------------------


def test_ambiguous_identity_name_only_matching_measured_live(
    identity_resolver: IdentityResolver,
    leads: list[EvalLead],
    cleanup_identity: IdentityCleanup,
) -> None:
    """Not a pass/fail bar — a live-measured number, printed for the record.

    Domain-based matching is the primary path and always succeeds in this
    dataset by construction (canonical_domain never varies). The interesting
    number is the name-only fallback: if a lead arrives with a company name
    and no domain, how often does exact matching still unify it correctly
    against the ~5% subset built specifically to stress that?
    """
    ambiguous_companies = {
        lead.company.company_id: lead.company for lead in leads if lead.company.name_variants
    }
    assert ambiguous_companies, "seeded dataset produced no ambiguous-identity companies to measure"

    domain_failures = 0
    name_only_failures = 0
    false_merges = 0
    owner_of: dict[UUID, str] = {}

    for synthetic_id, company in ambiguous_companies.items():
        raw_names = (company.legal_name, *company.name_variants)

        domain_ids = {
            identity_resolver.resolve_company(domain=company.canonical_domain, name=n).company_id
            for n in raw_names
        }
        cleanup_identity.domains.append(normalize_domain(company.canonical_domain))
        if len(domain_ids) != 1:
            domain_failures += 1

        name_only_ids: set[UUID] = set()
        for n in raw_names:
            resolved = identity_resolver.resolve_company(domain=None, name=n)
            name_only_ids.add(resolved.company_id)
            cleanup_identity.company_names.append(normalize_company_name(n))

            previous_owner = owner_of.get(resolved.company_id)
            if previous_owner is not None and previous_owner != synthetic_id:
                false_merges += 1
            owner_of[resolved.company_id] = synthetic_id

        if len(name_only_ids) != 1:
            name_only_failures += 1

    total = len(ambiguous_companies)
    domain_failure_rate = domain_failures / total
    name_only_failure_rate = name_only_failures / total

    print(
        f"\nAmbiguous-identity measurement (seed=42, {total} companies with "
        f"deliberate name-surface-form variation):\n"
        f"  domain-based matching failure rate:     {domain_failure_rate:.1%} "
        f"({domain_failures}/{total})\n"
        f"  name-only fallback failure rate:        {name_only_failure_rate:.1%} "
        f"({name_only_failures}/{total})\n"
        f"  cross-company false merges (name-only): {false_merges}\n"
    )

    # Domain is always present in this dataset by construction -- this isn't
    # really "measuring" so much as confirming the primary path is unaffected
    # by ambiguity that specifically targets the name-only fallback.
    assert domain_failure_rate == 0.0
    # A false merge -- two genuinely different companies treated as one -- is
    # the one failure mode worse than the fallback simply missing a match,
    # and the one this test fails loudly on regardless of the measured rate.
    assert false_merges == 0
