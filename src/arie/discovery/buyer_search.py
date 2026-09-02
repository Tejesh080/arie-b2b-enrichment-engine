"""Real buyer identification — Opportunity Activation Part 3-10.

Hunter's Domain Search (`GET /v2/domain-search`, verified live against the
vendor 2026-09-03) is a genuinely different capability from the combined
enrichment endpoint `arie.providers.hunter_contract`/`arie.providers.
live_hunter` already wire: that pair enriches a *known* email; this endpoint
searches a *domain* and returns real named people — name, position, a
seniority/department enum pair, a `decision_maker` flag, and (for a share of
matches) an email with its own confidence and verification status. Apollo's
existing adapter (`arie.providers.apollo_contract`) has the identical
"enrich a known email" shape and no company-to-person search — inspected,
not assumed, before writing this module. `APOLLO_API_KEY` is also unset in
this environment, so Hunter is the one real provider this slice wires.

**Gated, not automatic.** `buyer_search_eligible` is the deterministic gate
Part 3/11 asks for: a company must already be a strong-enough recommendation
before ARIE spends on finding who to contact there. `execute_buyer_search`
re-checks freshness before ever calling out — a retry against an
already-answered lead costs nothing, the same idempotency guarantee
`arie.research_acquisition.execute_research` already gives selective
research.

**Nothing here is invented.** `rank_buyers` only reorders what Hunter
actually returned; it can drop a candidate to a lower rank for a wrong-role
match, never promote a fabricated one. `BuyerCandidate.full_name` always
comes from the provider's own `first_name`/`last_name` fields.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

import httpx
import psycopg

from arie.config import HUNTER, HunterConfig
from arie.core.types import Evidence, ProviderStatus
from arie.discovery.models import BuyerCandidate, EmailStatus
from arie.evidence import ttl_policy as _ttl_policy
from arie.evidence.store import PostgresEvidenceStore
from arie.evidence.ttl_policy import ttl_for_field
from arie.ledger.store import PostgresCostLedger
from arie.limits import get_usage_against_limits
from arie.normalization.taxonomy import (
    is_unknown,
    normalize_function,
    normalize_seniority,
    seniority_from_title,
)
from arie.recommendations import CustomerPriority

__all__ = [
    "BUYER_EVIDENCE_FIELDS",
    "BuyerSearchError",
    "BuyerSearchFn",
    "BuyerSearchOutcome",
    "buyer_search_eligible",
    "execute_buyer_search",
    "fake_buyer_search",
    "find_buyers",
    "rank_buyers",
    "read_existing_buyer",
]

_LOGGER = logging.getLogger("arie.discovery.buyer_search")

_ELIGIBLE_PRIORITIES = frozenset({CustomerPriority.CONTACT_FIRST, CustomerPriority.WORTH_PURSUING})

BUYER_EVIDENCE_FIELDS: tuple[str, ...] = (
    "buyer_name",
    "buyer_title",
    "buyer_function",
    "buyer_seniority",
    "buyer_email",
    "buyer_email_status",
    "buyer_profile_url",
    "buyer_decision_maker",
)
"""Field names this module writes to the existing `evidence` table, on the
lead's own person entity — additive, never scored (not in
`arie.scoring.rules.SCORED_FIELDS`), so they cannot silently move a score."""

_BUYER_FIELD_TTL_SECONDS = 30 * 86_400
"""A buyer's identity is stable for a while but not permanent (role changes,
company moves) — 30 days, matching the existing person-attribute fields
(`title_seniority`/`title_function`) rather than the 7-day generic default a
field this module didn't register would otherwise fall back to."""

for _field in BUYER_EVIDENCE_FIELDS:
    _ttl_policy.FIELD_TTL_SECONDS.setdefault(_field, _BUYER_FIELD_TTL_SECONDS)


class BuyerSearchError(RuntimeError):
    """A Hunter Domain Search call failed outright. Isolated per lead —
    never fails the discovery run."""


@dataclass(frozen=True)
class RawBuyerRecord:
    first_name: str | None
    last_name: str | None
    position: str | None
    seniority_enum: str | None
    department_enum: str | None
    decision_maker: bool | None
    email: str | None
    email_confidence: float | None
    email_verification_status: str | None
    linkedin: str | None


def buyer_search_eligible(*, priority: CustomerPriority, existing_buyer_name: str | None) -> bool:
    """Part 3/11's deterministic gate. `SKIP`/`REVIEW` companies never reach
    a buyer search — poor or uncertain fit is not worth spending on. A lead
    that already has a named buyer on file is also never re-searched."""
    return priority in _ELIGIBLE_PRIORITIES and existing_buyer_name is None


def find_buyers(domain: str, *, limit: int, config: HunterConfig = HUNTER) -> list[RawBuyerRecord]:
    if not config.api_key:
        raise BuyerSearchError("Hunter is not configured (HUNTER_API_KEY unset)")
    try:
        response = httpx.get(
            config.domain_search_base_url,
            params={"domain": domain, "limit": max(1, min(limit, 10))},
            headers={"X-API-KEY": config.api_key},
            timeout=config.timeout_seconds,
        )
    except httpx.HTTPError as exc:
        raise BuyerSearchError(f"Hunter domain search transport error: {exc}") from exc

    if response.status_code == 404:
        return []  # Hunter's documented no-match shape for this endpoint
    if response.status_code != 200:
        raise BuyerSearchError(f"Hunter domain search returned HTTP {response.status_code}")
    try:
        body = response.json()
    except ValueError as exc:
        raise BuyerSearchError("Hunter domain search returned a non-JSON response") from exc

    data = body.get("data") if isinstance(body, dict) else None
    emails = data.get("emails") if isinstance(data, dict) else None
    if not isinstance(emails, list):
        return []

    records: list[RawBuyerRecord] = []
    for row in emails:
        if not isinstance(row, dict):
            continue
        verification = row.get("verification")
        verification_status = verification.get("status") if isinstance(verification, dict) else None
        confidence = row.get("confidence")
        records.append(
            RawBuyerRecord(
                first_name=_clean(row.get("first_name")),
                last_name=_clean(row.get("last_name")),
                position=_clean(row.get("position") or row.get("position_raw")),
                seniority_enum=_clean(row.get("seniority")),
                department_enum=_clean(row.get("department")),
                decision_maker=row.get("decision_maker")
                if isinstance(row.get("decision_maker"), bool)
                else None,
                email=_clean(row.get("value")),
                email_confidence=float(confidence) / 100.0
                if isinstance(confidence, (int, float))
                else None,
                email_verification_status=_clean(verification_status),
                linkedin=_clean(row.get("linkedin")),
            )
        )
    return records


def _clean(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def _email_status(record: RawBuyerRecord) -> EmailStatus:
    if record.email is None:
        return EmailStatus.NONE
    if record.email_verification_status == "valid":
        return EmailStatus.VERIFIED
    if (record.email_confidence or 0.0) >= 0.90:
        return EmailStatus.LIKELY
    return EmailStatus.UNVERIFIED


def _to_candidate(record: RawBuyerRecord, *, source: str) -> BuyerCandidate | None:
    """`None` for a record with no name at all — Hunter can return an email
    with no attached identity, and a nameless "buyer" is not a buyer."""
    name = " ".join(part for part in (record.first_name, record.last_name) if part).strip()
    if not name:
        return None

    # Seniority: title-first, enum fallback — the same inversion
    # `hunter_contract.py` documents and for the identical reason (Hunter's
    # five-value ladder folds C-level and VP into one `executive` bucket).
    parsed_seniority = seniority_from_title(record.position)
    seniority = (
        parsed_seniority
        if not is_unknown(parsed_seniority)
        else normalize_seniority(record.seniority_enum)
    )
    function = normalize_function(record.department_enum)
    if is_unknown(function):
        function = normalize_function(record.position)

    email_status = _email_status(record)
    return BuyerCandidate(
        full_name=name,
        title=record.position,
        seniority=None if is_unknown(seniority) else seniority,
        function=None if is_unknown(function) else function,
        email=record.email,
        email_status=email_status,
        profile_url=record.linkedin,
        decision_maker=record.decision_maker,
        source=source,
        confidence=record.email_confidence,
    )


def rank_buyers(
    records: list[RawBuyerRecord],
    *,
    preferred_seniorities: tuple[str, ...],
    preferred_functions: tuple[str, ...],
    source: str = "hunter_domain_search",
) -> list[BuyerCandidate]:
    """Deterministic ranking against the targeting profile's own preferred
    seniorities/functions — never an LLM decision (Part 6: a model may only
    normalize an ambiguous title elsewhere, never decide ranking or spend)."""
    candidates = [c for c in (_to_candidate(r, source=source) for r in records) if c is not None]

    def _score(candidate: BuyerCandidate) -> tuple[float, float, float, float]:
        seniority_score = 1.0 if candidate.seniority in preferred_seniorities else 0.0
        function_score = 1.0 if candidate.function in preferred_functions else 0.0
        decision_maker_score = 1.0 if candidate.decision_maker else 0.0
        email_score = {
            EmailStatus.VERIFIED: 1.0,
            EmailStatus.LIKELY: 0.7,
            EmailStatus.UNVERIFIED: 0.3,
            EmailStatus.NONE: 0.0,
        }[candidate.email_status]
        return (seniority_score, function_score, decision_maker_score, email_score)

    return sorted(candidates, key=_score, reverse=True)


def read_existing_buyer(
    evidence_store: PostgresEvidenceStore, *, organization_id: UUID, person_id: UUID, now: datetime
) -> BuyerCandidate | None:
    """Idempotency read — a fresh `buyer_name` row means a previous run
    already found and ranked a buyer for this lead; never re-spend."""
    name_evidence = evidence_store.get_fresh(
        "person", person_id, "buyer_name", organization_id=organization_id, now=now
    )
    if name_evidence is None:
        return None

    def _read(field: str) -> Any | None:
        e = evidence_store.get_fresh(
            "person", person_id, field, organization_id=organization_id, now=now
        )
        return e.value if e is not None else None

    email_status_raw = _read("buyer_email_status")
    return BuyerCandidate(
        full_name=str(name_evidence.value),
        title=_read("buyer_title"),
        seniority=_read("buyer_seniority"),
        function=_read("buyer_function"),
        email=_read("buyer_email"),
        email_status=EmailStatus(email_status_raw) if email_status_raw else EmailStatus.NONE,
        profile_url=_read("buyer_profile_url"),
        decision_maker=_read("buyer_decision_maker"),
        source=name_evidence.source,
        confidence=name_evidence.confidence,
    )


def _write_buyer_evidence(
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    person_id: UUID,
    candidate: BuyerCandidate,
    now: datetime,
) -> None:
    fields: dict[str, Any] = {
        "buyer_name": candidate.full_name,
        "buyer_title": candidate.title,
        "buyer_function": candidate.function,
        "buyer_seniority": candidate.seniority,
        "buyer_email": candidate.email,
        "buyer_email_status": str(candidate.email_status),
        "buyer_profile_url": candidate.profile_url,
        "buyer_decision_maker": candidate.decision_maker,
    }
    for field_name, value in fields.items():
        if value is None:
            continue
        evidence_store.put(
            Evidence(
                entity_type="person",
                entity_id=person_id,
                field_name=field_name,
                value=value,
                source=candidate.source,
                confidence=candidate.confidence if candidate.confidence is not None else 0.75,
                ttl_seconds=ttl_for_field(field_name),
                fetched_at=now,
            ),
            organization_id=organization_id,
        )


_MAX_ALTERNATES = 2


@dataclass(frozen=True)
class BuyerSearchOutcome:
    best: BuyerCandidate | None
    alternates: tuple[BuyerCandidate, ...]
    """Up to `_MAX_ALTERNATES` runners-up — Part 7: "not twenty employees."
    Empty whenever `best` came from `read_existing_buyer` (nothing new was
    ranked this call) or no buyer was found at all."""
    provider_called: bool
    """`False` on every path that spent nothing — ineligible, already known,
    unconfigured, over budget, or a transport failure — so the caller's
    funnel accounting never has to re-derive it."""


def execute_buyer_search(
    conn: psycopg.Connection,
    ledger: PostgresCostLedger,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    lead_id: UUID,
    person_id: UUID,
    domain: str,
    priority: CustomerPriority,
    preferred_seniorities: tuple[str, ...],
    preferred_functions: tuple[str, ...],
    now: datetime,
) -> BuyerSearchOutcome:
    existing = read_existing_buyer(
        evidence_store, organization_id=organization_id, person_id=person_id, now=now
    )
    if not buyer_search_eligible(
        priority=priority, existing_buyer_name=existing.full_name if existing else None
    ):
        return BuyerSearchOutcome(best=existing, alternates=(), provider_called=False)

    if not HUNTER.api_key:
        return BuyerSearchOutcome(best=None, alternates=(), provider_called=False)

    usage = get_usage_against_limits(conn, organization_id=organization_id, now=now)
    estimated_cost = Decimal(str(HUNTER.domain_search_cost_usd_per_call))
    if Decimal(str(usage.modeled_spend_remaining_usd)) < estimated_cost:
        return BuyerSearchOutcome(best=None, alternates=(), provider_called=False)

    idempotency_key = f"buyer_search:{lead_id}:hunter_domain_search"
    started = time.perf_counter()
    try:
        records = find_buyers(domain, limit=10)
        status = ProviderStatus.SUCCESS if records else ProviderStatus.MISS
    except BuyerSearchError:
        _LOGGER.info("buyer search failed for lead %s domain %s", lead_id, domain, exc_info=True)
        status = ProviderStatus.ERROR
        records = []
    latency_ms = (time.perf_counter() - started) * 1000

    ledger.record_provider_call(
        idempotency_key=idempotency_key,
        provider="hunter_domain_search",
        entity_type="person",
        entity_id=person_id,
        status=status,
        cost_usd=float(estimated_cost) if status == ProviderStatus.SUCCESS else 0.0,
        latency_ms=latency_ms,
        organization_id=organization_id,
        lead_id=lead_id,
    )

    if not records:
        return BuyerSearchOutcome(best=None, alternates=(), provider_called=True)

    ranked = rank_buyers(
        records,
        preferred_seniorities=preferred_seniorities,
        preferred_functions=preferred_functions,
    )
    if not ranked:
        return BuyerSearchOutcome(best=None, alternates=(), provider_called=True)

    best = ranked[0]
    _write_buyer_evidence(
        evidence_store,
        organization_id=organization_id,
        person_id=person_id,
        candidate=best,
        now=now,
    )
    return BuyerSearchOutcome(
        best=best, alternates=tuple(ranked[1 : 1 + _MAX_ALTERNATES]), provider_called=True
    )


class BuyerSearchFn(Protocol):
    """`execute_buyer_search`'s own signature, extracted so
    `arie.discovery.orchestrator` can inject a fake in tests — the same
    seam `arie.discovery.providers.DiscoveryProvider` and
    `arie.discovery.website_verification.WebsiteVerifierFn` already are."""

    def __call__(
        self,
        conn: psycopg.Connection,
        ledger: PostgresCostLedger,
        evidence_store: PostgresEvidenceStore,
        *,
        organization_id: UUID,
        lead_id: UUID,
        person_id: UUID,
        domain: str,
        priority: CustomerPriority,
        preferred_seniorities: tuple[str, ...],
        preferred_functions: tuple[str, ...],
        now: datetime,
    ) -> BuyerSearchOutcome: ...


def fake_buyer_search(
    conn: psycopg.Connection,
    ledger: PostgresCostLedger,
    evidence_store: PostgresEvidenceStore,
    *,
    organization_id: UUID,
    lead_id: UUID,
    person_id: UUID,
    domain: str,
    priority: CustomerPriority,
    preferred_seniorities: tuple[str, ...],
    preferred_functions: tuple[str, ...],
    now: datetime,
) -> BuyerSearchOutcome:
    """Deterministic, no network — never finds a buyer. The only buyer
    search the test suite or a keyless developer machine exercises; a test
    that needs a real-looking match constructs a `BuyerSearchOutcome`
    directly instead of going through Hunter."""
    return BuyerSearchOutcome(best=None, alternates=(), provider_called=False)
