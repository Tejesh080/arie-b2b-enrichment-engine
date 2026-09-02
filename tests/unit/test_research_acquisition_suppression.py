"""M7 Slice 6, Part 0 — wiring `arie.live.outcome_cache.ProviderOutcomeGuard`
into `arie.research_acquisition`'s live-mode authorization inputs.

`arie.research.authorize_research`'s own suppression branch
(`SUPPRESSED_RECENT_FAILURE`) is already exhaustively covered against a bare
`suppressed_providers` set in `tests/unit/test_research.py`; the guard's own
TTL/tenant-scoping SQL is covered live in
`tests/integration/test_provider_outcome_and_identity_integration.py`. What
was missing — the actual gap this slice closes — is the read that turns a
guard's answer into that set at all. This file exercises exactly that seam,
against a small duck-typed fake guard so it needs no database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from arie.research_acquisition import _suppressed_providers

ORG = UUID("11111111-1111-1111-1111-111111111111")
OTHER_ORG = UUID("22222222-2222-2222-2222-222222222222")
COMPANY = UUID("33333333-3333-3333-3333-333333333333")
NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class _Sentinel:
    since: datetime


class _FakeOutcomeGuard:
    """Duck-typed stand-in for `ProviderOutcomeGuard` — records every call it
    receives (so a test can assert tenant scoping was actually requested) and
    answers from two small, explicitly-configured maps."""

    def __init__(
        self,
        *,
        misses: dict[tuple[str, UUID, UUID], _Sentinel] | None = None,
        uncertain: dict[tuple[str, UUID, UUID], _Sentinel] | None = None,
    ) -> None:
        self._misses = misses or {}
        self._uncertain = uncertain or {}
        self.calls: list[tuple[str, str, UUID, UUID]] = []

    def recent_miss(self, provider_name, entity_type, entity_id, *, organization_id):  # type: ignore[no-untyped-def]
        self.calls.append(("miss", provider_name, entity_id, organization_id))
        return self._misses.get((provider_name, entity_id, organization_id))

    def recent_uncertain_outcome(self, provider_name, entity_type, entity_id, *, organization_id):  # type: ignore[no-untyped-def]
        self.calls.append(("uncertain", provider_name, entity_id, organization_id))
        return self._uncertain.get((provider_name, entity_id, organization_id))


def test_recent_miss_suppresses_that_provider() -> None:
    guard = _FakeOutcomeGuard(
        misses={("abstract_company_enrichment", COMPANY, ORG): _Sentinel(NOW)}
    )
    result = _suppressed_providers(
        guard,
        ("abstract_company_enrichment",),
        entity_type="company",
        entity_id=COMPANY,
        organization_id=ORG,
    )
    assert result == frozenset({"abstract_company_enrichment"})


def test_recent_uncertain_outcome_suppresses_that_provider() -> None:
    guard = _FakeOutcomeGuard(
        uncertain={("hunter_combined_enrichment", COMPANY, ORG): _Sentinel(NOW)}
    )
    result = _suppressed_providers(
        guard,
        ("hunter_combined_enrichment",),
        entity_type="company",
        entity_id=COMPANY,
        organization_id=ORG,
    )
    assert result == frozenset({"hunter_combined_enrichment"})


def test_unsuppressed_provider_is_unaffected_by_a_sibling_suppression() -> None:
    guard = _FakeOutcomeGuard(
        misses={("abstract_company_enrichment", COMPANY, ORG): _Sentinel(NOW)}
    )
    result = _suppressed_providers(
        guard,
        ("abstract_company_enrichment", "hunter_combined_enrichment"),
        entity_type="company",
        entity_id=COMPANY,
        organization_id=ORG,
    )
    assert result == frozenset({"abstract_company_enrichment"})
    assert "hunter_combined_enrichment" not in result


def test_expired_suppression_does_not_block() -> None:
    """`ProviderOutcomeGuard.recent_miss`/`recent_uncertain_outcome` already
    return `None` once a suppression window has passed — this only proves
    that a `None` answer (a guard's own "safe to ask again") never gets
    turned into a suppression here."""
    guard = _FakeOutcomeGuard(misses={})
    result = _suppressed_providers(
        guard,
        ("abstract_company_enrichment",),
        entity_type="company",
        entity_id=COMPANY,
        organization_id=ORG,
    )
    assert result == frozenset()


def test_organization_id_is_forwarded_to_the_guard_for_every_candidate() -> None:
    """No cross-org suppression leakage: the guard is always asked with this
    call's own `organization_id`, never a bare/omitted one — the tenant
    isolation the guard itself enforces depends on this caller actually
    supplying it."""
    guard = _FakeOutcomeGuard()
    _suppressed_providers(
        guard,
        ("abstract_company_enrichment",),
        entity_type="company",
        entity_id=COMPANY,
        organization_id=ORG,
    )
    assert all(call[3] == ORG for call in guard.calls)
    assert all(call[3] != OTHER_ORG for call in guard.calls)


def test_no_guard_means_no_suppression() -> None:
    """`outcome_guard=None` (every pre-fix caller) keeps the old behaviour —
    an always-empty suppressed set — rather than raising."""
    result = _suppressed_providers(
        None,
        ("abstract_company_enrichment",),
        entity_type="company",
        entity_id=COMPANY,
        organization_id=ORG,
    )
    assert result == frozenset()


def test_no_candidates_means_no_suppression() -> None:
    guard = _FakeOutcomeGuard(
        misses={("abstract_company_enrichment", COMPANY, ORG): _Sentinel(NOW)}
    )
    result = _suppressed_providers(
        guard, (), entity_type="company", entity_id=COMPANY, organization_id=ORG
    )
    assert result == frozenset()
    assert guard.calls == []


def test_unresolved_entity_means_no_suppression() -> None:
    """A field whose entity hasn't resolved yet (shouldn't happen for a
    decided lead, but stays a safe no-op rather than a crash)."""
    guard = _FakeOutcomeGuard(
        misses={("abstract_company_enrichment", COMPANY, ORG): _Sentinel(NOW)}
    )
    result = _suppressed_providers(
        guard,
        ("abstract_company_enrichment",),
        entity_type="company",
        entity_id=None,
        organization_id=ORG,
    )
    assert result == frozenset()
    assert guard.calls == []
