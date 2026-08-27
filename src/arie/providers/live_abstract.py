"""The one real enrichment adapter (post-M1 P5) — Abstract API Company Enrichment.

**Scope, stated the way every other real-boundary module in this codebase states
it:** this calls exactly one documented REST endpoint
(``https://companyenrichment.abstractapi.com/v2``) for exactly one capability —
company/domain firmographic lookup — and normalizes at most two fields,
``employee_count`` and ``industry``, the same two ``arie.providers.catalog``'s
simulated ``firmographics_basic``/``firmographics_premium`` already model. It
never registers a second provider, never scrapes, never automates a browser, and
never retries beyond the one bounded HTTP attempt below — see
``docs/architecture.md``'s P5 section for the full "will not do" list.

**Why this provider.** Simple API-key-in-query-string auth, a free tier (100
requests/month, no card) for failure-path testing, a deterministic JSON
response, and — the deciding factor — its response *field names* are exactly
ARIE's own ``arie.scoring.rules.SCORED_FIELDS`` names for the two fields it
returns.

**Matching field names are not matching vocabularies, and P5 conflated the
two.** ``industry`` arrives as Abstract's own category string — ``"Computer
Software"``, ``"Financial Services"``, ``"Hospital & Health Care"``. Until the
Live V1 Foundation this adapter lower-cased it and handed it straight to the
scorer, where ``_INDUSTRY_POINTS.get("computer software", 0.0)`` returned 0.0:
a prime-ICP software company scored as though it had been assessed and found
worthless, indistinguishable from a genuine poor fit. Every value this adapter
emits now goes through ``arie.normalization.contract``, which maps it onto the
closed canonical vocabulary or reports it as unmapped — never both, and never
silently neither.

**Never raises for an ordinary operational outcome.** Mirrors
``arie.providers.simulated.SimulatedProvider`` exactly: a data miss, a timeout,
an auth failure, a rate limit, a server error, or a malformed body all come back
as a ``ProviderResult`` with the matching ``ProviderStatus`` and zero cost —
never an exception. Raising is reserved for a genuine caller bug (wrong
``entity_type``), the same boundary ``SimulatedProvider.fetch`` draws. This
keeps the adapter's failure modes composable with the ordinary evidence/ledger
path (``arie.jobs.handlers``) without that caller needing a provider-specific
except clause.

**Never leaks the API key.** ``api_key`` travels as a query parameter because
that is Abstract's documented (and only) authentication mechanism — there is no
header alternative to switch to. Every error path below builds its own message
from structured facts (status code, exception type name) and never interpolates
an httpx exception's ``str()`` or a request/response URL, either of which would
otherwise carry the key straight into a log line, a span attribute, or a raised
message.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from arie.config import LIVE_PROVIDER, LiveProviderConfig
from arie.core.types import Entity, EntityType, ProviderResult, ProviderStatus
from arie.normalization.contract import NormalizationReport, normalize_provider_fields

PROVIDER_NAME = "abstract_company_enrichment"
"""The one live provider's catalogue-independent name. Deliberately not added to
``arie.providers.catalog.CATALOG`` — that tuple drives frozen dataset generation
(``arie.evalgen.generator``) and the M0 benchmark; adding to it would perturb
both. This provider is registered only by ``arie.jobs.handlers``' live-mode
handler builder, never by ``arie.providers.simulated.build_registry``."""

PROVIDES_FIELDS: tuple[str, ...] = ("employee_count", "industry")

LIVE_POLICY_NAME = "live_single_provider"
"""LEGACY ``decision_receipts.policy_name`` — what the live handler wrote while
exactly one real provider existed. The handler now writes
``arie.live.strategy.OPTIMIZED_POLICY_NAME`` (or the evaluation name), and the
receipt matches against ``arie.live.strategy.LIVE_POLICY_NAMES``, which keeps
this value so stored rows never stop resolving as live receipts. Nothing
writes it anymore; it exists for reading history."""

# Declared source-reliability confidence for a SUCCESS result. An assumption,
# exactly in the spirit of arie.providers.catalog's ProviderSpec fields
# (categorical_error, numeric_noise) — Abstract does not publish an accuracy
# figure, and this project's own rule (docs/benchmark.md) is that no
# parameter enters production unstated. Revisit once real-world agreement is
# measurable — the same "turn an assumption into a measurement" step ADR 0003's
# "When to revisit" section already names for the first real adapter wired.
_DECLARED_CONFIDENCE = 0.80

# Typical REST latency for a single-hop JSON API call — not vendor-published
# (Abstract's docs state no SLA), so this is an assumption used only for the
# EnrichmentProvider Protocol's declared metadata, never for billing or for the
# ledger, which always records *measured* latency from this adapter's own call.
_ASSUMED_P50_LATENCY_MS = 400
_ASSUMED_P95_LATENCY_MS = 1800


class AbstractCompanyProviderError(RuntimeError):
    """Base for every error this module raises."""


class AbstractCompanyConfigurationError(AbstractCompanyProviderError):
    """Raised at construction, not at call time, if no API key is configured.

    Mirrors ``arie.llm.deepseek.DeepSeekConfigurationError``'s reasoning
    exactly: failing before any request is attempted is what lets a caller
    (``arie.jobs.handlers.build_handlers``) tell "live mode is misconfigured"
    apart from "live mode tried and failed" — and per the P5 brief, a missing
    key must fail clearly and never silently fall back to the simulator.
    """


@dataclass(frozen=True)
class AbstractCompanyEnrichmentProvider:
    """Satisfies ``arie.providers.base.EnrichmentProvider`` structurally.

    Inject ``client`` in tests with an ``httpx.MockTransport``-backed client —
    same pattern as ``arie.llm.deepseek.DeepSeekSignalExtractor`` — so nothing
    here ever needs a live API key or network access to be exercised
    deterministically.
    """

    config: LiveProviderConfig
    client: httpx.Client

    @staticmethod
    def build(
        *, config: LiveProviderConfig | None = None, client: httpx.Client | None = None
    ) -> AbstractCompanyEnrichmentProvider:
        resolved_config = config or LIVE_PROVIDER
        if client is None and not resolved_config.configured:
            raise AbstractCompanyConfigurationError(
                "ABSTRACT_COMPANY_API_KEY is not set — see .env.example. Pass an explicit "
                "`client` (e.g. in tests) to bypass this check."
            )
        # follow_redirects=True as defense-in-depth: a live request during P5's
        # own verification found Abstract 301-redirects a trailing-slash
        # mismatch (query string preserved) rather than erroring, and
        # LiveProviderConfig.base_url's own default now avoids that
        # round-trip anyway — this only matters for a misconfigured override.
        resolved_client = client or httpx.Client(
            timeout=resolved_config.timeout_seconds, follow_redirects=True
        )
        return AbstractCompanyEnrichmentProvider(config=resolved_config, client=resolved_client)

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def entity_type(self) -> EntityType:
        return "company"

    @property
    def provides_fields(self) -> tuple[str, ...]:
        return PROVIDES_FIELDS

    @property
    def base_cost_usd(self) -> float:
        return self.config.cost_usd_per_call

    @property
    def p50_latency_ms(self) -> int:
        return _ASSUMED_P50_LATENCY_MS

    @property
    def p95_latency_ms(self) -> int:
        return _ASSUMED_P95_LATENCY_MS

    def close(self) -> None:
        self.client.close()

    def fetch(self, entity: Entity) -> ProviderResult:
        """Look up one company by domain. Never raises for an operational failure.

        ``entity.canonical_key`` must be the company's domain — the only
        identifier this endpoint accepts. A caller with no domain at all (a
        real lead the identity resolver could only key by company name) must
        not call this method; ``arie.jobs.handlers``' live handler checks that
        before ever constructing an ``Entity`` for it.
        """
        if entity.entity_type != "company":
            raise ValueError(f"{self.name} serves company entities, got {entity.entity_type}")

        started = time.monotonic()
        try:
            response = self.client.get(
                self.config.base_url,
                params={"api_key": self.config.api_key, "domain": entity.canonical_key},
            )
        except httpx.TimeoutException:
            return self._result(
                status=ProviderStatus.TIMEOUT,
                fields={},
                cost_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                entity=entity,
                error_kind="timeout",
            )
        except httpx.HTTPError as exc:
            # Never interpolate str(exc) or exc.request.url — both may carry
            # the api_key query parameter straight into this message.
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                entity=entity,
                error_kind=f"transport_error:{type(exc).__name__}",
            )

        latency_ms = (time.monotonic() - started) * 1000

        if response.status_code == 401 or response.status_code == 403:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind="authentication_failed",
            )
        if response.status_code == 429:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind="rate_limited",
            )
        if response.status_code == 422:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind="insufficient_credits",
            )
        if response.status_code >= 500:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind="server_error",
            )
        if response.status_code != 200:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=f"unexpected_status:{response.status_code}",
            )

        try:
            body: Any = response.json()
        except ValueError:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind="malformed_response",
            )
        if not isinstance(body, dict):
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind="malformed_response",
            )

        report = _normalize_fields(body)

        if not report.has_usable_fields:
            # A 200 with no usable field is a genuine miss, not a failure —
            # and, post-Live-V1, "the provider answered in vocabulary we could
            # not map" lands here too rather than being scored as zero. The
            # `unmapped` detail rides along on `raw` so an operator can see the
            # difference between "Abstract knows nothing about this domain" and
            # "Abstract said something ARIE needs a taxonomy entry for".
            #
            # Abstract's own pricing page states a submitted domain consumes a
            # credit "regardless of response success" — billed the same as a
            # SUCCESS call, matching arie.providers.catalog's `bill_on_miss`
            # convention for vendors that charge for the lookup itself.
            return self._result(
                status=ProviderStatus.MISS,
                fields={},
                cost_usd=self.config.cost_usd_per_call,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=None,
                normalization=report,
            )

        return self._result(
            status=ProviderStatus.SUCCESS,
            fields=report.fields,
            cost_usd=self.config.cost_usd_per_call,
            latency_ms=latency_ms,
            entity=entity,
            error_kind=None,
            normalization=report,
        )

    def _result(
        self,
        *,
        status: ProviderStatus,
        fields: dict[str, Any],
        cost_usd: float,
        latency_ms: float,
        entity: Entity,
        error_kind: str | None,
        normalization: NormalizationReport | None = None,
    ) -> ProviderResult:
        raw: dict[str, Any] = {"provider": self.name, "entity": entity.canonical_key}
        if error_kind is not None:
            raw["error_kind"] = error_kind
        if normalization is not None:
            # Always, not only on failure. The raw->canonical pair is the whole
            # audit trail for a mapping decision: "Abstract said 'Computer
            # Software', ARIE scored it as 'software'" is checkable, while
            # "industry: software" alone is not. `audit()` is field names,
            # truncated raw values, and canonical values — never the whole
            # vendor payload, and never anything derived from the API key.
            raw["normalization"] = normalization.audit()
        return ProviderResult(
            fields=fields,
            confidence=_DECLARED_CONFIDENCE if status is ProviderStatus.SUCCESS else 0.0,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            raw=raw,
        )


def _normalize_fields(body: dict[str, Any]) -> NormalizationReport:
    """Only ever surface the two fields this adapter declares, deterministically.

    Two steps, and the split is the point (see
    ``arie.normalization.contract``): *this* function knows which Abstract
    response keys correspond to which ARIE field names — the only
    Abstract-specific knowledge in the pipeline — and the canonical layer knows
    what the values mean. A new provider re-implements the first step and
    reuses the second, which is what stops two adapters disagreeing about
    whether "SaaS" is software.

    A malformed or unmappable individual field is dropped from ``fields`` and
    recorded in ``unmapped`` rather than failing the whole response: the honest
    response to "the provider sent something we can't use for this one field"
    is "treat that field as unknown", not "fail the entire call" and not
    "score it as zero".
    """
    return normalize_provider_fields(
        provider=PROVIDER_NAME,
        entity_type="company",
        raw_fields={key: body.get(key) for key in PROVIDES_FIELDS},
    )
