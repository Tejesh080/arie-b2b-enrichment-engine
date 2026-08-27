"""The third real enrichment adapter — Hunter Combined Enrichment (Live V1).

The transport half of ``arie.providers.hunter_contract``, in exactly the
relationship ``live_apollo`` has to ``apollo_contract``: a bounded ``GET``, a
status-code table, and a ``ProviderResult``, with every emitted field passing
through the contract module's normalization. See that module for the payload
shape and the two (deliberately different) enum-vs-title precedence rules.

**The contract, verified against Hunter's own documentation** (``hunter.io/
api-documentation/v2`` and Hunter's credit help centre), not inferred:

* ``GET https://api.hunter.io/v2/combined/find?email=...`` — one call returns
  the person and their employer. The ``X-API-KEY`` header authenticates
  (Hunter also accepts a query parameter; the header is used so the credential
  never enters a URL, same rule as the Apollo adapter).
* **A no-match is a 404**, with Hunter's standard ``{"errors": [...]}`` body.
  Both other adapters get a 200 for a miss (Abstract an empty object, Apollo a
  ``null`` person) — here the miss is an HTTP error status, and mapping it to
  ``ProviderStatus.ERROR`` would report a perfectly healthy vendor as broken
  on every lead Hunter simply doesn't know.
* **Hunter's error table is inverted from the common convention, and the
  mapping below follows Hunter's documentation rather than the convention:**
  ``403`` is documented as *rate limit reached* (not forbidden) and ``429`` as
  *usage quota exceeded* (not rate limiting). So 429 — the plan's credits
  being gone — maps to ``quota_exhausted``, the error kind the cooldown guard
  watches, and 403 maps to ``rate_limited``, a transient condition that needs
  no cooldown. Swapping these would either cool Hunter down for an hour every
  time a burst grazed 15 req/s, or hammer a dead quota all day.
* Rate limits: 15 requests/second, 500/minute — far above anything ARIE's
  spend caps permit, so no client-side limiter.
* Metered in **credits**: 0.2 per successful API enrichment, none when Hunter
  finds nothing. Bill-on-match, like Apollo and unlike Abstract. A matched
  person whose vocabulary ARIE cannot map is a *billed* MISS — the credit was
  consumed and the failure is the taxonomy's, not Hunter's — exactly the
  distinction ``live_apollo`` draws.

**The company half is carried, not persisted.** A combined response includes
``category.industry`` and ``metrics.employees`` — Abstract's two fields. They
ride on ``ProviderResult.raw`` as ``company_preview`` (the canonical audit
form) for the provider bake-off to compare against Abstract, and they do not
become evidence: ``provides_fields`` declares person fields only, and the
handler persists only declared fields. Promoting Hunter's company data to
evidence is a decision the bake-off's measurements exist to inform, not one
this adapter takes by default.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from arie.config import HUNTER, HunterConfig
from arie.core.types import Entity, EntityType, ProviderResult, ProviderStatus
from arie.normalization.contract import NormalizationReport
from arie.providers.hunter_contract import (
    HUNTER_PROVIDER_NAME,
    HUNTER_PROVIDES_FIELDS,
    company_payload,
    normalize_hunter_company,
    normalize_hunter_person,
    normalized_identity,
    person_payload,
)

__all__ = [
    "HUNTER_PROVIDER_NAME",
    "HUNTER_PROVIDES_FIELDS",
    "HunterConfigurationError",
    "HunterEnrichmentProvider",
    "HunterProviderError",
]

# Declared source-reliability confidence for a SUCCESS result — a stated
# assumption, like its two siblings' (Abstract 0.80, Apollo 0.75). Matched to
# Apollo's, not Abstract's: this adapter has the same weaker second path
# (free-text title parsing when the enums are absent or too coarse), and
# declaring the pair equal means the evidence merge layer breaks a
# Hunter-vs-Apollo disagreement on recency and lets the conflict signals do
# their work, rather than one vendor silently outranking the other on a
# number nobody measured. Revisit when the bake-off produces real
# agreement data.
_DECLARED_CONFIDENCE = 0.75

_ASSUMED_P50_LATENCY_MS = 500
_ASSUMED_P95_LATENCY_MS = 2200

_ENTITY_TYPE: EntityType = "person"


class HunterProviderError(RuntimeError):
    """Base for every error this module raises."""


class HunterConfigurationError(HunterProviderError):
    """Raised at construction, not at call time, if no API key is configured.

    Same rule as both siblings: a live worker with a missing credential fails
    loudly at startup and never silently runs a thinner pipeline than it
    reports.
    """


@dataclass(frozen=True)
class HunterEnrichmentProvider:
    """Satisfies ``arie.providers.base.EnrichmentProvider`` structurally.

    Inject ``client`` in tests with an ``httpx.MockTransport``-backed client —
    the same pattern as every other adapter — so nothing here needs a key,
    network access, or a spent credit.
    """

    config: HunterConfig
    client: httpx.Client

    @staticmethod
    def build(
        *, config: HunterConfig | None = None, client: httpx.Client | None = None
    ) -> HunterEnrichmentProvider:
        resolved_config = config or HUNTER
        if client is None and not resolved_config.configured:
            raise HunterConfigurationError(
                "HUNTER_API_KEY is not set — see .env.example. PROVIDER_MODE=live builds "
                "every registered adapter at startup. Pass an explicit `client` (e.g. in "
                "tests) to bypass this check."
            )
        resolved_client = client or httpx.Client(timeout=resolved_config.timeout_seconds)
        return HunterEnrichmentProvider(config=resolved_config, client=resolved_client)

    @property
    def name(self) -> str:
        return HUNTER_PROVIDER_NAME

    @property
    def entity_type(self) -> EntityType:
        return _ENTITY_TYPE

    @property
    def provides_fields(self) -> tuple[str, ...]:
        return HUNTER_PROVIDES_FIELDS

    @property
    def base_cost_usd(self) -> float:
        return self.config.cost_usd_per_success

    @property
    def p50_latency_ms(self) -> int:
        return _ASSUMED_P50_LATENCY_MS

    @property
    def p95_latency_ms(self) -> int:
        return _ASSUMED_P95_LATENCY_MS

    def close(self) -> None:
        self.client.close()

    def fetch(self, entity: Entity) -> ProviderResult:
        """Look up one person (and their employer) by work email.

        Never raises for an operational failure; ``entity.canonical_key`` must
        be the person's canonical email, the identifier every ingested lead
        has. Same input discipline as the Apollo adapter, for the same reason.
        """
        if entity.entity_type != _ENTITY_TYPE:
            raise ValueError(f"{self.name} serves person entities, got {entity.entity_type}")

        started = time.monotonic()
        try:
            response = self.client.get(
                self.config.base_url,
                headers={"X-API-KEY": self.config.api_key, "Accept": "application/json"},
                params={"email": entity.canonical_key},
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
            # Never interpolate str(exc) — the type name is all a reader needs,
            # and an httpx repr can carry request detail.
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                entity=entity,
                error_kind=f"transport_error:{type(exc).__name__}",
            )

        latency_ms = (time.monotonic() - started) * 1000

        if response.status_code == 404:
            # Hunter's documented no-match. Free: "no credits are used if
            # Hunter can't find" — and NOT an error, however the status code
            # reads. The one adapter where a 4xx is an ordinary miss.
            return self._result(
                status=ProviderStatus.MISS,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=None,
            )

        failure = _status_error_kind(response.status_code)
        if failure is not None:
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=failure,
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

        if not person_payload(body) and not company_payload(body):
            # A 200 whose data holds neither half. Hunter's contract says a
            # no-match is a 404, so an empty 200 is off-contract — but the
            # honest reading is still "nothing was delivered", and Hunter's
            # billing rule says undelivered data is unbilled. A free MISS.
            return self._result(
                status=ProviderStatus.MISS,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=None,
            )

        report = normalize_hunter_person(body)

        if not report.has_usable_fields:
            # Hunter delivered a record — the 0.2 credits are consumed — but
            # nothing in it maps onto the canonical vocabulary. A billed MISS,
            # with `unmapped` carrying what Hunter actually said: the alias
            # table's feedback loop, and the same rule as `live_apollo`.
            return self._result(
                status=ProviderStatus.MISS,
                fields={},
                cost_usd=self.config.cost_usd_per_success,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=None,
                normalization=report,
                body=body,
            )

        return self._result(
            status=ProviderStatus.SUCCESS,
            fields=report.fields,
            cost_usd=self.config.cost_usd_per_success,
            latency_ms=latency_ms,
            entity=entity,
            error_kind=None,
            normalization=report,
            body=body,
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
        body: dict[str, Any] | None = None,
    ) -> ProviderResult:
        raw: dict[str, Any] = {"provider": self.name, "entity": entity.canonical_key}
        if error_kind is not None:
            raw["error_kind"] = error_kind
        if cost_usd > 0.0:
            raw["credits_consumed"] = self.config.credits_per_success
            raw["cost_basis"] = "modelled_credit_equivalent"
        if normalization is not None:
            raw["normalization"] = normalization.audit()
        if body is not None:
            raw["matched_identity"] = normalized_identity(body).audit()
            company = normalize_hunter_company(body)
            if company.mapped or company.unmapped:
                # The Abstract-overlap comparison data, in audit form only —
                # raw→canonical pairs and unmapped strings, never the payload.
                # Not evidence; see the module docstring.
                raw["company_preview"] = company.audit()
        return ProviderResult(
            fields=fields,
            confidence=_DECLARED_CONFIDENCE if status is ProviderStatus.SUCCESS else 0.0,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            raw=raw,
        )


def _status_error_kind(status_code: int) -> str | None:
    """Hunter's documented status table → the shared ``error_kind`` vocabulary.

    The 403/429 pair follows Hunter's documentation, not convention — see the
    module docstring. 404 never reaches here (handled as a miss first).
    """
    if status_code == 200:
        return None
    if status_code == 401:
        return "authentication_failed"
    if status_code == 403:
        return "rate_limited"
    if status_code == 429:
        return "quota_exhausted"
    if status_code in (400, 422):
        return "unprocessable_request"
    if status_code >= 500:
        return "server_error"
    return f"unexpected_status:{status_code}"
