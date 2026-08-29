"""Provider bake-off — measure the three live vendors against each other.

**Separate from the synthetic benchmark, on purpose.** ``bench/`` proves the
*acquisition policy* against a frozen simulated world and must not move. This
harness measures the *vendors*: on a controlled list of identities, what does
each provider actually match, return, cost, and take — and where do they
overlap, disagree, or add nothing? Its output is the evidence for choosing the
default waterfall order; until it exists, "cheapest first" is a reasoned prior
and is labelled as one.

Two modes, one honest boundary between them:

* ``--mock`` — every vendor HTTP layer is a deterministic ``MockTransport``
  driven by the identity file's ``persona`` column. Zero credentials, zero
  spend, runnable in CI. This is how the harness itself is proven, and how the
  report's shape is reviewed before a single credit is risked.
* real mode — requires ``--confirm-live-spend`` *and* every provider key, and
  refuses up front (listing the exact missing variables) rather than running a
  partial comparison that would under-count whichever vendor lacked a key.

**Spend discipline (Phase 12).** Successful and miss results are cached in the
run directory (``results.jsonl``) keyed by provider+identity: re-running a
partially completed run resumes without re-spending, and no provider is ever
called twice for the same unchanged identity just to re-test code. Errors are
deliberately not cached — they are transient, and caching one would freeze a
vendor's bad five minutes into the dataset. ``--limit`` bounds identities per
run (start small), ``--max-spend-usd`` bounds the run's total modelled spend
predictively, and every cost figure in the report is a *modelled* credit
equivalent, never billed spend.

**PII.** Run artifacts land in ``data/evaluation/runs/`` (gitignored — a real
run's CSV is the operator's own controlled identity list and stays local). The
shipped ``identities.example.csv`` is entirely synthetic ``.test`` identities.
Records keep the email (the join key of the whole comparison), the job title,
and the canonical values — no payloads, no personal contact data beyond what
was submitted.

Usage::

    python scripts/provider_bakeoff.py --identities data/evaluation/identities.example.csv \\
        --mock --out data/evaluation/runs/mock-demo

    python scripts/provider_bakeoff.py --identities my-controlled-list.csv \\
        --confirm-live-spend --limit 5 --max-spend-usd 0.25 \\
        --out data/evaluation/runs/pilot-1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from arie.config import APOLLO_PERSON, HUNTER, LIVE_PROVIDER
from arie.core.types import Entity
from arie.identity.normalize import domain_from_email, normalize_email
from arie.identity.validation import (
    MISMATCH,
    PROBABLE,
    VERIFIED,
    RequestedIdentity,
    ReturnedIdentity,
    validate_identity,
)
from arie.live.evaluation import (
    CONFLICT,
    classify_agreement,
    overall_agreement,
)
from arie.providers.base import EnrichmentProvider
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider
from arie.providers.live_apollo import APOLLO_PROVIDER_NAME, ApolloPersonEnrichmentProvider
from arie.providers.live_hunter import HunterEnrichmentProvider

PERSON_FIELDS = ("title_seniority", "title_function")
PERSON_PROVIDERS = (HUNTER_PROVIDER_NAME, APOLLO_PROVIDER_NAME)


# ---------------------------------------------------------------- identities --


@dataclass(frozen=True)
class BakeoffIdentity:
    """One controlled identity — the unit of comparison."""

    email: str
    domain: str | None
    full_name: str | None = None
    company_name: str | None = None
    persona: str | None = None
    """Mock-mode script: which canned vendor behaviour this identity gets.
    Ignored entirely in real mode."""
    expected_full_name: str | None = None
    """The operator's own ground truth for *who this email should belong to*
    — optional, and usually absent (most real leads carry no expected name
    at all; see ``arie.identity.validation``'s module docstring). When
    present, ``_record_from_result`` runs it through ``validate_identity``
    against each person provider's match, which is what turns a same-company
    wrong-person answer (the Stripe/Patrick Bosmans case) into a measured
    ``identity_verdict`` instead of an uncounted false success."""


def load_identities(path: Path) -> list[BakeoffIdentity]:
    identities: list[BakeoffIdentity] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            email = normalize_email(row["email"].strip())
            domain = (row.get("domain") or "").strip() or domain_from_email(email)
            identities.append(
                BakeoffIdentity(
                    email=email,
                    domain=domain,
                    full_name=(row.get("full_name") or "").strip() or None,
                    company_name=(row.get("company_name") or "").strip() or None,
                    persona=(row.get("persona") or "").strip() or None,
                    expected_full_name=(row.get("expected_full_name") or "").strip() or None,
                )
            )
    return identities


# ------------------------------------------------------------------- records --


@dataclass(frozen=True)
class BakeoffRecord:
    """One provider's outcome for one identity — the Phase 7 comparison row."""

    provider: str
    email: str
    served_from: str
    """``live_call`` | ``cache_file`` (a previous run's result reused) |
    ``skipped_budget`` | ``skipped_no_domain``."""
    status: str
    raw_title: str | None
    title_seniority: str | None
    title_function: str | None
    usable_fields: tuple[str, ...]
    company_industry: str | None
    """Canonical industry — from ``fields`` for Abstract, from the
    ``company_preview`` audit for Hunter (carried, never evidence)."""
    company_employee_count: int | None
    fields_returned: tuple[str, ...]
    latency_ms: float
    cache_hit: bool
    credits_consumed: float | None
    cost_usd: float
    cost_basis: str | None
    error_kind: str | None
    called_at: str
    identity_verdict: str | None = None
    """``arie.identity.validation`` verdict for a person-provider success,
    when the identity CSV supplied an ``expected_full_name`` to check against
    — ``None`` for a company-provider record, a miss/error, or an identity
    with no expected name on file (nothing to compare)."""
    person_evidence_usable: bool | None = None
    """Whether this record's person fields would be allowed to score — false
    only for a confirmed ``MISMATCH``; ``None`` alongside ``identity_verdict
    is None`` (no check was possible, not the same claim as "usable")."""

    def cache_key(self) -> str:
        return _cache_key(self.provider, self.email)


def _cache_key(provider: str, email: str) -> str:
    digest = hashlib.sha256(f"{provider}:{email}".encode()).hexdigest()
    return digest[:24]


def _record_from_result(
    provider: EnrichmentProvider, identity: BakeoffIdentity, result: Any, *, served_from: str
) -> BakeoffRecord:
    fields: Mapping[str, Any] = result.fields
    raw: Mapping[str, Any] = result.raw
    matched = raw.get("matched_identity") or {}
    company_industry: str | None = None
    company_employees: int | None = None
    if provider.name == ABSTRACT_PROVIDER_NAME:
        company_industry = fields.get("industry")
        company_employees = fields.get("employee_count")
    else:
        preview = raw.get("company_preview") or {}
        for item in preview.get("mapped", []):
            if item["field"] == "industry":
                company_industry = item["canonical"]
            if item["field"] == "employee_count":
                company_employees = item["canonical"]

    identity_verdict: str | None = None
    person_evidence_usable: bool | None = None
    if (
        provider.entity_type == "person"
        and str(result.status) == "success"
        and identity.expected_full_name
        and isinstance(matched, Mapping)
    ):
        validation = validate_identity(
            RequestedIdentity(
                email=identity.email,
                company_domain=identity.domain,
                full_name=identity.expected_full_name,
            ),
            ReturnedIdentity(
                full_name=matched.get("full_name"),
                email=matched.get("email"),
                employer_domain=matched.get("employer_domain"),
                employer_name=matched.get("employer_name"),
            ),
        )
        identity_verdict = validation.verdict
        person_evidence_usable = validation.verdict != MISMATCH

    return BakeoffRecord(
        provider=provider.name,
        email=identity.email,
        served_from=served_from,
        status=str(result.status),
        raw_title=matched.get("title") if isinstance(matched, Mapping) else None,
        title_seniority=fields.get("title_seniority"),
        title_function=fields.get("title_function"),
        usable_fields=tuple(sorted(fields)),
        company_industry=company_industry,
        company_employee_count=company_employees,
        fields_returned=tuple(sorted(fields)),
        latency_ms=round(result.latency_ms, 1),
        cache_hit=False,
        credits_consumed=raw.get("credits_consumed"),
        cost_usd=result.cost_usd,
        cost_basis=raw.get("cost_basis"),
        error_kind=raw.get("error_kind"),
        called_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        identity_verdict=identity_verdict,
        person_evidence_usable=person_evidence_usable,
    )


def _skip_record(provider_name: str, identity: BakeoffIdentity, served_from: str) -> BakeoffRecord:
    return BakeoffRecord(
        provider=provider_name,
        email=identity.email,
        served_from=served_from,
        status="skipped",
        raw_title=None,
        title_seniority=None,
        title_function=None,
        usable_fields=(),
        company_industry=None,
        company_employee_count=None,
        fields_returned=(),
        latency_ms=0.0,
        cache_hit=False,
        credits_consumed=None,
        cost_usd=0.0,
        cost_basis=None,
        error_kind=None,
        called_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )


# ----------------------------------------------------------------- mock mode --

_MOCK_PERSONAS = (
    "agree",
    "conflict",
    "hunter_only",
    "apollo_only",
    "both_miss",
    "unmappable",
    "abstract_sufficient",
)


def _persona_for(identity: BakeoffIdentity) -> str:
    if identity.persona in _MOCK_PERSONAS:
        return identity.persona
    digest = int(hashlib.sha256(identity.email.encode()).hexdigest(), 16)
    return _MOCK_PERSONAS[digest % len(_MOCK_PERSONAS)]


def _mock_abstract(personas: Mapping[str, str]) -> AbstractCompanyEnrichmentProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        domain = request.url.params.get("domain", "")
        persona = personas.get(domain, "agree")
        if persona == "abstract_sufficient":
            return httpx.Response(200, json={"employee_count": 5, "industry": "Construction"})
        if persona == "unmappable":
            # The overlap-mismatch case: Abstract's industry disagrees with the
            # one Hunter's preview will claim below.
            return httpx.Response(
                200, json={"employee_count": 30, "industry": "Financial Services"}
            )
        return httpx.Response(200, json={"employee_count": 240, "industry": "Computer Software"})

    from arie.config import LiveProviderConfig

    return AbstractCompanyEnrichmentProvider(
        config=LiveProviderConfig(api_key="mock", cost_usd_per_call=0.00165),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _mock_hunter(personas: Mapping[str, str]) -> HunterEnrichmentProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        email = request.url.params.get("email", "")
        persona = personas.get(email, "agree")
        if persona in ("apollo_only", "both_miss"):
            return httpx.Response(404, json={"errors": [{"id": "not_found", "code": 404}]})
        if persona == "unmappable":
            return httpx.Response(
                200,
                json={
                    "data": {
                        "person": {
                            "name": {"fullName": "Alex Vance"},
                            "employment": {"title": "Vibes Curator", "role": "chief_vibes"},
                        },
                        "company": {
                            "category": {"industry": "Computer Software"},
                            "metrics": {"employees": 260},
                        },
                    }
                },
            )
        title, role = ("VP of Sales", "sales")
        if persona == "conflict":
            title, role = ("Director of Marketing", "marketing")
        return httpx.Response(
            200,
            json={
                "data": {
                    "person": {
                        "name": {"fullName": "Mock Person"},
                        "employment": {"name": "MockCo", "title": title, "role": role},
                    },
                    "company": {
                        "category": {"industry": "Computer Software"},
                        "metrics": {"employees": 240},
                    },
                }
            },
        )

    return HunterEnrichmentProvider(
        config=HUNTER.__class__(api_key="mock", cost_usd_per_success=0.0049),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def _mock_apollo(personas: Mapping[str, str]) -> ApolloPersonEnrichmentProvider:
    def handler(request: httpx.Request) -> httpx.Response:
        email = request.url.params.get("email", "")
        persona = personas.get(email, "agree")
        if persona in ("hunter_only", "both_miss"):
            return httpx.Response(200, json={"person": None})
        return httpx.Response(
            200,
            json={
                "person": {
                    "name": "Mock Person",
                    "title": "VP of Sales",
                    "seniority": "vp",
                    "departments": ["sales"],
                }
            },
        )

    return ApolloPersonEnrichmentProvider(
        config=APOLLO_PERSON.__class__(api_key="mock", cost_usd_per_success=0.0196),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def build_mock_providers(identities: Sequence[BakeoffIdentity]) -> list[EnrichmentProvider]:
    by_email = {identity.email: _persona_for(identity) for identity in identities}
    by_domain = {
        identity.domain: _persona_for(identity) for identity in identities if identity.domain
    }
    return [_mock_abstract(by_domain), _mock_hunter(by_email), _mock_apollo(by_email)]


def build_real_providers() -> list[EnrichmentProvider]:
    """Abstract and Hunter are required; Apollo is optional.

    Abstract (company) and Hunter (the cheaper person provider) are the
    minimum needed for *any* comparison — missing either would under-count a
    vendor the run claims to have measured, so both still refuse loudly.
    Apollo is the third, most expensive person provider: a bake-off run that
    deliberately excludes it (e.g. a two-provider experiment) is a legitimate,
    smaller comparison, not a broken one, so an absent ``APOLLO_API_KEY``
    degrades the provider list rather than refusing the whole run. It is never
    silent — the caller sees Apollo missing from the returned list and every
    downstream summary/report already renders an unmeasured provider's rates
    as ``None`` rather than zero (see ``summarize_provider``), so "not
    configured" cannot be misread as "measured and found lacking."
    """
    missing = []
    if not LIVE_PROVIDER.configured:
        missing.append("ABSTRACT_COMPANY_API_KEY")
    if not HUNTER.configured:
        missing.append("HUNTER_API_KEY")
    if missing:
        raise SystemExit(
            "Refusing to run a live bake-off with provider keys missing: "
            + ", ".join(missing)
            + ". A partial comparison would under-count whichever vendor lacked a key. "
            "Set the variables in .env (see .env.example) or run with --mock."
        )
    providers: list[EnrichmentProvider] = [
        AbstractCompanyEnrichmentProvider.build(),
        HunterEnrichmentProvider.build(),
    ]
    if APOLLO_PERSON.configured:
        providers.append(ApolloPersonEnrichmentProvider.build())
    else:
        print(
            "APOLLO_API_KEY not set — running without Apollo (not_configured). "
            "Abstract and Hunter still run.",
            file=sys.stderr,
        )
    return providers


# --------------------------------------------------------------------- runner --


def run_bakeoff(
    identities: Sequence[BakeoffIdentity],
    providers: Sequence[EnrichmentProvider],
    *,
    cached: Mapping[str, BakeoffRecord],
    max_spend_usd: float,
) -> tuple[list[BakeoffRecord], float]:
    """Every provider against every identity, spend-bounded and resumable.

    Returns the records (cache-served ones included, marked) and the modelled
    USD actually added by this run. The spend bound is predictive: a call
    whose worst-case price would cross ``max_spend_usd`` is skipped and
    recorded as skipped, never half-made.
    """
    records: list[BakeoffRecord] = []
    spent = 0.0
    for identity in identities:
        for provider in providers:
            key = _cache_key(provider.name, identity.email)
            if key in cached:
                previous = cached[key]
                records.append(BakeoffRecord(**{**asdict(previous), "served_from": "cache_file"}))
                continue
            if provider.entity_type == "company":
                if identity.domain is None:
                    records.append(_skip_record(provider.name, identity, "skipped_no_domain"))
                    continue
                entity = Entity(
                    entity_type="company", entity_id=uuid.uuid4(), canonical_key=identity.domain
                )
            else:
                entity = Entity(
                    entity_type="person", entity_id=uuid.uuid4(), canonical_key=identity.email
                )
            if spent + provider.base_cost_usd > max_spend_usd:
                records.append(_skip_record(provider.name, identity, "skipped_budget"))
                continue
            result = provider.fetch(entity)
            spent += result.cost_usd
            records.append(_record_from_result(provider, identity, result, served_from="live_call"))
    return records, spent


# ------------------------------------------------------------------- metrics --


def _rate(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _percentile(values: Sequence[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 1)


def summarize_provider(records: Sequence[BakeoffRecord], provider: str) -> dict[str, Any]:
    """The Phase 8 per-provider metric block. Pure; unit-tested."""
    own = [r for r in records if r.provider == provider]
    attempted = [r for r in own if r.served_from in ("live_call", "cache_file")]
    live = [r for r in own if r.served_from == "live_call"]
    matches = [r for r in attempted if r.status == "success"]
    misses = [r for r in attempted if r.status == "miss"]
    errors = [r for r in attempted if r.status == "error"]
    with_title = [r for r in attempted if r.raw_title]
    with_seniority = [r for r in attempted if r.title_seniority]
    with_function = [r for r in attempted if r.title_function]
    usable = [r for r in attempted if r.usable_fields]
    latencies = [r.latency_ms for r in live if r.status in ("success", "miss")]
    credits = sum(r.credits_consumed or 0.0 for r in live)
    cost = sum(r.cost_usd for r in live)
    n = len(attempted)

    # Identity-fidelity metrics (Live V1 stabilization, 2026-08-30): a
    # PROVIDER match ("someone answered") is not a VERIFIED match ("the
    # intended person answered") — see arie.identity.validation. Only
    # meaningful for person providers whose identity CSV supplied an
    # `expected_full_name`; `identity_checked` is the denominator note that
    # makes the rest of these rates honest when it did not.
    identity_checked = [r for r in attempted if r.identity_verdict is not None]
    verified = [r for r in identity_checked if r.identity_verdict == VERIFIED]
    probable = [r for r in identity_checked if r.identity_verdict == PROBABLE]
    mismatched = [r for r in identity_checked if r.identity_verdict == MISMATCH]
    # "Usable" = not a *confirmed* mismatch — a success with no expected name
    # to check (person_evidence_usable is None) is unproven, not unusable;
    # excluding it from the numerator would misreport it as bad evidence,
    # which is precisely the UNKNOWN-is-not-negative-evidence rule this
    # package exists to keep.
    usable_person = [r for r in matches if r.person_evidence_usable is not False]

    return {
        "provider": provider,
        "identities_attempted": n,
        "live_calls": len(live),
        "served_from_cache_file": len([r for r in own if r.served_from == "cache_file"]),
        "skipped": len([r for r in own if r.served_from.startswith("skipped")]),
        "match_rate": _rate(len(matches), n),
        "title_return_rate": _rate(len(with_title), n),
        "canonical_seniority_rate": _rate(len(with_seniority), n),
        "canonical_function_rate": _rate(len(with_function), n),
        "usable_evidence_rate": _rate(len(usable), n),
        "miss_rate": _rate(len(misses), n),
        "error_rate": _rate(len(errors), n),
        "median_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "credits_consumed": round(credits, 2),
        "modelled_cost_usd": round(cost, 5),
        "cost_per_match_usd": _cost_per(cost, len(matches)),
        "cost_per_usable_field_usd": _cost_per(cost, sum(len(r.usable_fields) for r in matches)),
        "identity_checked_count": len(identity_checked),
        "verified_person_rate": _rate(len(verified), n),
        "probable_person_rate": _rate(len(probable), n),
        "identity_mismatch_rate": _rate(len(mismatched), n),
        "usable_person_evidence_rate": _rate(len(usable_person), n),
        "cost_per_verified_person_result_usd": _cost_per(cost, len(verified)),
        "cost_per_usable_person_result_usd": _cost_per(cost, len(usable_person)),
    }


def _cost_per(cost: float, count: int) -> float | None:
    return round(cost / count, 5) if count else None


def _person_values(records: Sequence[BakeoffRecord], email: str) -> dict[str, dict[str, Any]]:
    values: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.email != email or record.provider not in PERSON_PROVIDERS:
            continue
        if record.served_from not in ("live_call", "cache_file"):
            continue
        values[record.provider] = {
            "title_seniority": record.title_seniority,
            "title_function": record.title_function,
        }
    return values


def agreement_summary(records: Sequence[BakeoffRecord]) -> dict[str, Any]:
    """Phase 9: per-identity Hunter-vs-Apollo verdicts, aggregated.

    Uses the same classifier as the evaluation-parallel pipeline
    (``arie.live.evaluation``) so "conflict" means one thing everywhere.
    """
    emails = sorted({r.email for r in records})
    verdicts: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    for email in emails:
        values = _person_values(records, email)
        if len(values) < 2:
            continue
        per_field = classify_agreement(values, PERSON_FIELDS)
        verdict = overall_agreement(per_field)
        verdicts[email] = verdict
        if verdict == CONFLICT:
            conflicts.append({"email": email, "fields": per_field, "values": values})
    counts: dict[str, int] = {}
    for verdict in verdicts.values():
        counts[verdict] = counts.get(verdict, 0) + 1
    compared = len(verdicts)
    return {
        "identities_compared": compared,
        "rates": {label: _rate(count, compared) for label, count in sorted(counts.items())},
        "conflicts": conflicts,
    }


def overlap_summary(records: Sequence[BakeoffRecord]) -> dict[str, Any]:
    """Phase 8's overlap block + Phase 10's Abstract-vs-Hunter company view.

    Person overlap: for each scored person field, how often each provider was
    the only source of a usable value (provider-only evidence) versus both
    supplying one. Company overlap: where Hunter's combined response carried a
    company preview, does it agree with what Abstract said about the same
    employer?
    """
    emails = sorted({r.email for r in records})
    both = 0
    only: dict[str, int] = {name: 0 for name in PERSON_PROVIDERS}
    for email in emails:
        values = _person_values(records, email)
        usable = {
            provider
            for provider, fields in values.items()
            if any(value for value in fields.values())
        }
        if len(usable) == 2:
            both += 1
        elif len(usable) == 1:
            only[next(iter(usable))] += 1

    industry_pairs = 0
    industry_agree = 0
    count_pairs = 0
    count_within_quarter = 0
    for email in emails:
        abstract = next(
            (
                r
                for r in records
                if r.email == email
                and r.provider == ABSTRACT_PROVIDER_NAME
                and r.status == "success"
            ),
            None,
        )
        hunter = next(
            (
                r
                for r in records
                if r.email == email and r.provider == HUNTER_PROVIDER_NAME and r.status == "success"
            ),
            None,
        )
        if abstract is None or hunter is None:
            continue
        if abstract.company_industry and hunter.company_industry:
            industry_pairs += 1
            if abstract.company_industry == hunter.company_industry:
                industry_agree += 1
        if abstract.company_employee_count and hunter.company_employee_count:
            count_pairs += 1
            larger = max(abstract.company_employee_count, hunter.company_employee_count)
            gap = abs(abstract.company_employee_count - hunter.company_employee_count)
            if gap / larger <= 0.25:
                count_within_quarter += 1

    return {
        "person_evidence": {
            "both_providers_usable": both,
            "provider_only": only,
        },
        "company_hunter_vs_abstract": {
            "industry_pairs": industry_pairs,
            "industry_agreement_rate": _rate(industry_agree, industry_pairs),
            "employee_count_pairs": count_pairs,
            "employee_count_within_25pct_rate": _rate(count_within_quarter, count_pairs),
        },
    }


def sufficiency_summary(records: Sequence[BakeoffRecord]) -> dict[str, Any]:
    """How often Abstract alone would have ended optimized acquisition.

    Answered with the pipeline's own stopping rule — the corpus-calibrated
    confidence model over company-only evidence — because "sufficient" means
    one thing in ARIE: the model is confident enough at tau that buying person
    evidence stops being worthwhile. The model fit takes a few seconds.
    """
    from datetime import UTC, datetime

    from arie.core.types import Evidence
    from arie.evidence.ttl_policy import ttl_for_field
    from arie.jobs.handlers import build_runtime
    from arie.scoring.engine import score_evidence

    model = build_runtime().policy.model
    now = datetime.now(UTC)
    sufficient = 0
    assessed = 0
    per_identity: dict[str, bool] = {}
    for email in sorted({r.email for r in records}):
        abstract = next(
            (
                r
                for r in records
                if r.email == email
                and r.provider == ABSTRACT_PROVIDER_NAME
                and r.status == "success"
            ),
            None,
        )
        if abstract is None:
            continue
        entity_id = uuid.uuid4()
        rows = [
            Evidence(
                entity_type="company",
                entity_id=entity_id,
                field_name=name,
                value=value,
                source=ABSTRACT_PROVIDER_NAME,
                confidence=0.8,
                ttl_seconds=ttl_for_field(name),
                fetched_at=now,
            )
            for name, value in (
                ("industry", abstract.company_industry),
                ("employee_count", abstract.company_employee_count),
            )
            if value is not None
        ]
        if not rows:
            continue
        assessed += 1
        scoring = score_evidence(rows, now)
        is_sufficient = bool(scoring.bounds.is_settled or model.predict(scoring) >= model.tau)
        per_identity[email] = is_sufficient
        if is_sufficient:
            sufficient += 1
    return {
        "identities_assessed": assessed,
        "abstract_alone_sufficient": sufficient,
        "abstract_alone_sufficient_rate": _rate(sufficient, assessed),
        "per_identity": per_identity,
    }


def build_summary(records: Sequence[BakeoffRecord], *, with_sufficiency: bool) -> dict[str, Any]:
    providers = (ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME, APOLLO_PROVIDER_NAME)
    summary: dict[str, Any] = {
        "providers": [summarize_provider(records, name) for name in providers],
        "agreement": agreement_summary(records),
        "overlap": overlap_summary(records),
        "total_modelled_cost_usd": round(
            sum(r.cost_usd for r in records if r.served_from == "live_call"), 5
        ),
    }
    if with_sufficiency:
        summary["abstract_sufficiency"] = sufficiency_summary(records)
    return summary


# -------------------------------------------------------------------- report --


def render_report(summary: Mapping[str, Any]) -> str:
    lines = ["PROVIDER BAKE-OFF", "=" * 72]
    header = f"{'metric':<32}" + f"{'abstract':>13}" + f"{'hunter':>13}" + f"{'apollo':>13}"
    lines += [header, "-" * 72]
    metric_keys = [
        "identities_attempted",
        "live_calls",
        "served_from_cache_file",
        "match_rate",
        "title_return_rate",
        "canonical_seniority_rate",
        "canonical_function_rate",
        "usable_evidence_rate",
        "miss_rate",
        "error_rate",
        "median_latency_ms",
        "p95_latency_ms",
        "credits_consumed",
        "modelled_cost_usd",
        "cost_per_match_usd",
        "cost_per_usable_field_usd",
        "identity_checked_count",
        "verified_person_rate",
        "probable_person_rate",
        "identity_mismatch_rate",
        "usable_person_evidence_rate",
        "cost_per_verified_person_result_usd",
        "cost_per_usable_person_result_usd",
    ]
    blocks = {block["provider"]: block for block in summary["providers"]}
    for key in metric_keys:
        row = f"{key:<32}"
        for name in (ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME, APOLLO_PROVIDER_NAME):
            value = blocks[name].get(key)
            row += f"{'-' if value is None else value:>13}"
        lines.append(row)
    agreement = summary["agreement"]
    lines += ["", "person-provider agreement (hunter vs apollo, canonical values):"]
    lines.append(f"  identities compared: {agreement['identities_compared']}")
    for label, rate in agreement["rates"].items():
        lines.append(f"  {label:<10} {rate}")
    for conflict in agreement["conflicts"]:
        lines.append(f"  CONFLICT {conflict['email']}: {conflict['values']}")
    overlap = summary["overlap"]
    lines += ["", "evidence overlap:"]
    lines.append(
        f"  both person providers usable: {overlap['person_evidence']['both_providers_usable']}"
    )
    for name, count in overlap["person_evidence"]["provider_only"].items():
        lines.append(f"  only {name}: {count}")
    company = overlap["company_hunter_vs_abstract"]
    lines += ["", "hunter company preview vs abstract (phase 10):"]
    lines.append(
        f"  industry agreement: {company['industry_agreement_rate']} over {company['industry_pairs']} pairs"
    )
    lines.append(
        f"  employee count within 25%: {company['employee_count_within_25pct_rate']} over "
        f"{company['employee_count_pairs']} pairs"
    )
    if "abstract_sufficiency" in summary:
        sufficiency = summary["abstract_sufficiency"]
        lines += ["", "abstract-alone sufficiency (pipeline's own stopping rule):"]
        lines.append(
            f"  {sufficiency['abstract_alone_sufficient']} of {sufficiency['identities_assessed']} "
            f"identities ({sufficiency['abstract_alone_sufficient_rate']}) would stop before any "
            "person lookup"
        )
    lines += [
        "",
        f"total modelled spend this run: ${summary['total_modelled_cost_usd']}",
        "(every cost figure is a modelled credit equivalent, never billed spend)",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------- main --


def _load_cache(path: Path) -> dict[str, BakeoffRecord]:
    if not path.exists():
        return {}
    cached: dict[str, BakeoffRecord] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        payload = json.loads(line)
        payload.pop("served_from_original", None)
        record = BakeoffRecord(
            **{
                **payload,
                "usable_fields": tuple(payload["usable_fields"]),
                "fields_returned": tuple(payload["fields_returned"]),
            }
        )
        # Errors are transient; never resume them from cache.
        if record.status in ("success", "miss"):
            cached[record.cache_key()] = record
    return cached


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--identities", type=Path, required=True, help="CSV of identities")
    parser.add_argument("--out", type=Path, required=True, help="Run directory for artifacts")
    parser.add_argument("--mock", action="store_true", help="Fixture-backed run; no keys, no spend")
    parser.add_argument(
        "--confirm-live-spend",
        action="store_true",
        help="Required for a real run: acknowledges real provider credits will be consumed.",
    )
    parser.add_argument("--limit", type=int, default=5, help="Identities this run (default 5)")
    parser.add_argument(
        "--max-spend-usd",
        type=float,
        default=0.25,
        help="Hard modelled-spend ceiling for this run (default $0.25)",
    )
    parser.add_argument(
        "--no-sufficiency",
        action="store_true",
        help="Skip the abstract-alone sufficiency block (avoids the model fit)",
    )
    args = parser.parse_args(argv)

    if not args.mock and not args.confirm_live_spend:
        print(
            "Refusing to run without --mock or --confirm-live-spend: a real bake-off "
            "consumes provider credits deliberately, not by accident.",
            file=sys.stderr,
        )
        return 1

    identities = load_identities(args.identities)[: args.limit]
    providers = build_mock_providers(identities) if args.mock else build_real_providers()

    args.out.mkdir(parents=True, exist_ok=True)
    results_path = args.out / "results.jsonl"
    cached = _load_cache(results_path)

    records, spent = run_bakeoff(
        identities, providers, cached=cached, max_spend_usd=args.max_spend_usd
    )

    with results_path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(asdict(record)) + "\n")

    summary = build_summary(records, with_sufficiency=not args.no_sufficiency)
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    report = render_report(summary)
    (args.out / "report.txt").write_text(report + "\n", encoding="utf-8")
    print(report)
    print(f"\nartifacts: {results_path}, {args.out / 'summary.json'}, {args.out / 'report.txt'}")
    print(f"modelled spend added by this run: ${round(spent, 5)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
