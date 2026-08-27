"""The second real enrichment adapter — Apollo People Enrichment (Live V1).

**The transport half of a contract that already existed.**
``arie.providers.apollo_contract`` was written and reviewed first: it holds the
raw-payload→canonical-evidence mapping, tested entirely against fixtures, with
no HTTP client and no credential. This module adds the part that talks to
Apollo and nothing else — a bounded ``POST``, a status-code table, and a
``ProviderResult``. Every field it emits still goes through
``normalize_apollo_person``, so the vocabulary boundary is unchanged by wiring
the network in behind it.

**The contract, verified against Apollo's own documentation rather than
inherited from the fixtures.**

* ``POST https://api.apollo.io/api/v1/people/match``
* ``x-api-key: <key>`` request header. Not a query parameter — which is the
  material difference from ``arie.providers.live_abstract``, where Abstract
  publishes no header mechanism and the key necessarily rides in the URL. Here
  the credential never enters a URL at all, so it cannot reach a proxy log, an
  ``httpx`` exception's ``str()``, or a span attribute by accident.
* Match identifiers as query parameters. ARIE sends exactly one — ``email`` —
  plus two explicit opt-*outs*; see :func:`_match_params`.
* ``200`` with ``{"person": {...}}`` for a match, ``200`` with
  ``{"person": null}`` for a miss. A miss is a 200, not a 404.
* Metered in **credits**: one for a demographics match, zero when nothing
  credit-consuming is found. See ``ApolloPersonConfig.cost_usd_per_success``
  for the USD conversion and for what that number does and does not claim.
* Documented rate limit on this endpoint: 600 calls/hour, plan-dependent.
  ARIE's own spend caps (``arie.live.budget``) bind far below that at any
  plausible volume, so there is no client-side rate limiter here — a ``429``
  is handled as an ordinary operational outcome instead.

**Cost is charged on a match, not on a lookup — the opposite of Abstract.**
``live_abstract`` bills every MISS, because Abstract's pricing page says a
submitted domain consumes a credit regardless of response success. Apollo
consumes nothing when it has no person. So this adapter distinguishes two
MISSes that ``live_abstract`` cannot: *no person matched* (zero cost — Apollo
found nothing to charge for) and *a person matched whose vocabulary ARIE could
not map* (full cost — the credit was consumed; the failure is ARIE's taxonomy,
not Apollo's data). Collapsing those into one billing rule would either invent
spend that did not happen or hide spend that did.

**Never raises for an ordinary operational outcome.** A miss, a timeout, an
auth failure, a rate limit, a server error, or a malformed body all return a
``ProviderResult`` with the matching ``ProviderStatus``. Raising is reserved
for a genuine caller bug (wrong ``entity_type``) — exactly the boundary
``arie.providers.simulated.SimulatedProvider.fetch`` and ``live_abstract``
already draw, and what lets ``arie.jobs.handlers``' live loop treat a second
provider as one more source rather than a second error-handling path.

**Personal contact data is opted out of, at the request.**
``reveal_personal_emails`` and ``reveal_phone_number`` are sent as ``false``
rather than left to Apollo's defaults. Two independent reasons, and either
alone would be sufficient: a returned mobile number costs eight extra credits
(a 9x cost multiplier arriving silently), and ARIE scores neither — a personal
email or phone number would be PII fetched for no decision-relevant purpose,
which the evidence store deliberately does not hold.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from arie.config import APOLLO_PERSON, ApolloPersonConfig
from arie.core.types import Entity, EntityType, ProviderResult, ProviderStatus
from arie.normalization.contract import NormalizationReport
from arie.providers.apollo_contract import (
    APOLLO_PROVIDER_NAME,
    APOLLO_PROVIDES_FIELDS,
    normalize_apollo_person,
    normalized_identity,
)

__all__ = [
    "APOLLO_PROVIDER_NAME",
    "APOLLO_PROVIDES_FIELDS",
    "ApolloPersonConfigurationError",
    "ApolloPersonEnrichmentProvider",
    "ApolloPersonProviderError",
]

# Declared source-reliability confidence for a SUCCESS result — the same kind
# of stated assumption as ``live_abstract``'s 0.80 and the catalogue's
# ``ProviderSpec`` error rates, not a measurement. Set marginally below
# Abstract's because this adapter has a documented second-choice path: when
# Apollo ships no ``seniority``/``departments`` enum, the value comes from
# parsing a free-text title (``arie.providers.apollo_contract``), which is
# inherently less reliable than a vendor's structured answer. One declared
# number covers both paths, so it is set at the weaker one's level rather than
# the stronger one's. Revisit when real-world agreement is measurable — the
# same "turn an assumption into a measurement" step ADR 0003 names.
_DECLARED_CONFIDENCE = 0.75

# Assumed latencies for the EnrichmentProvider Protocol's declared metadata
# only — never used for billing, and never for the ledger, which always records
# this adapter's own measured latency. Apollo publishes no SLA; these are a
# person-lookup's typical single-hop REST profile, set above
# ``live_abstract``'s because a people-match query does more work than a domain
# lookup.
_ASSUMED_P50_LATENCY_MS = 600
_ASSUMED_P95_LATENCY_MS = 2500

_ENTITY_TYPE: EntityType = "person"


class ApolloPersonProviderError(RuntimeError):
    """Base for every error this module raises."""


class ApolloPersonConfigurationError(ApolloPersonProviderError):
    """Raised at construction, not at call time, if no API key is configured.

    Mirrors ``arie.providers.live_abstract.AbstractCompanyConfigurationError``
    exactly, and for the same reason: failing before any request is attempted
    is what lets ``arie.jobs.handlers.build_handlers`` tell "live mode is
    misconfigured" apart from "live mode tried and failed", and a missing key
    must never silently fall back to a mode that reports coverage for vendors
    it did not call.
    """


@dataclass(frozen=True)
class ApolloPersonEnrichmentProvider:
    """Satisfies ``arie.providers.base.EnrichmentProvider`` structurally.

    Inject ``client`` in tests with an ``httpx.MockTransport``-backed client —
    the same pattern ``live_abstract`` and ``arie.llm.deepseek`` use — so
    nothing here needs a live API key, network access, or a spent credit to be
    exercised deterministically.
    """

    config: ApolloPersonConfig
    client: httpx.Client

    @staticmethod
    def build(
        *, config: ApolloPersonConfig | None = None, client: httpx.Client | None = None
    ) -> ApolloPersonEnrichmentProvider:
        resolved_config = config or APOLLO_PERSON
        if client is None and not resolved_config.configured:
            raise ApolloPersonConfigurationError(
                "APOLLO_API_KEY is not set — see .env.example. PROVIDER_MODE=live now "
                "acquires person evidence as well as company evidence. Pass an explicit "
                "`client` (e.g. in tests) to bypass this check."
            )
        resolved_client = client or httpx.Client(timeout=resolved_config.timeout_seconds)
        return ApolloPersonEnrichmentProvider(config=resolved_config, client=resolved_client)

    @property
    def name(self) -> str:
        return APOLLO_PROVIDER_NAME

    @property
    def entity_type(self) -> EntityType:
        return _ENTITY_TYPE

    @property
    def provides_fields(self) -> tuple[str, ...]:
        return APOLLO_PROVIDES_FIELDS

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
        """Look up one person by work email. Never raises for an operational failure.

        ``entity.canonical_key`` must be the person's canonical email — the
        single identifier ARIE always has (``arie.api.ingest`` requires it, and
        ``arie.identity.normalize`` canonicalizes it), which is why this
        adapter asks for nothing else. Apollo accepts name/domain/LinkedIn
        combinations too and matches better with more inputs, but requiring
        them would put a new mandatory field on every submission to raise a
        match rate this project has not measured. If real match rates turn out
        to justify it, adding ``first_name``/``last_name``/``domain`` from the
        existing ``_LeadIdentity`` is a change to :func:`_match_params` alone.
        """
        if entity.entity_type != _ENTITY_TYPE:
            raise ValueError(f"{self.name} serves person entities, got {entity.entity_type}")

        started = time.monotonic()
        try:
            response = self.client.post(
                self.config.base_url,
                headers={"x-api-key": self.config.api_key, "accept": "application/json"},
                params=_match_params(entity.canonical_key),
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
            # Never interpolate str(exc). The key is a header rather than a
            # query parameter here, so a URL leak is not the risk it is in
            # `live_abstract` — but an httpx error's repr can still carry
            # request detail, and the type name is all a reader needs.
            return self._result(
                status=ProviderStatus.ERROR,
                fields={},
                cost_usd=0.0,
                latency_ms=(time.monotonic() - started) * 1000,
                entity=entity,
                error_kind=f"transport_error:{type(exc).__name__}",
            )

        latency_ms = (time.monotonic() - started) * 1000
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

        if not _person_matched(body):
            # Apollo found nobody. Its documented metering consumes no credit
            # in this case, so this MISS is genuinely free — the one place this
            # adapter's cost model diverges from `live_abstract`'s
            # bill-on-every-lookup rule, and the divergence is the vendors',
            # not a modelling choice.
            return self._result(
                status=ProviderStatus.MISS,
                fields={},
                cost_usd=0.0,
                latency_ms=latency_ms,
                entity=entity,
                error_kind=None,
                identity_present=False,
            )

        report = normalize_apollo_person(body)

        if not report.has_usable_fields:
            # A person WAS matched — the credit is spent — but nothing they
            # said maps onto the canonical vocabulary. Billed in full, and
            # recorded as a MISS rather than a SUCCESS carrying nothing:
            # `unmapped` on the result is what tells an operator this is a
            # missing row in `arie.normalization.taxonomy`'s alias table rather
            # than a person Apollo has no data on.
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
        identity_present: bool | None = None,
    ) -> ProviderResult:
        raw: dict[str, Any] = {"provider": self.name, "entity": entity.canonical_key}
        if error_kind is not None:
            raw["error_kind"] = error_kind
        if cost_usd > 0.0:
            # The vendor's own unit, next to the modelled dollars, so a reader
            # of a ledger row or a span can see that "$0.0196" is one credit
            # converted at a stated rate rather than a price Apollo quoted.
            raw["credits_consumed"] = self.config.credits_per_match
            raw["cost_basis"] = "modelled_credit_equivalent"
        if normalization is not None:
            # Always, not only on failure — the raw→canonical pair IS the audit
            # trail for a mapping decision. `audit()` is field names, truncated
            # raw values, and canonical values; never the whole vendor payload.
            raw["normalization"] = normalization.audit()
        if body is not None:
            # Who Apollo actually matched, for the receipt and a human
            # reviewer. Deliberately the narrow `ApolloPersonIdentity` view
            # (name/title/email/linkedin/organization) rather than the payload:
            # Apollo returns far more personal data than ARIE has any use for,
            # and none of these is a SCORED_FIELD, so none can reach the scorer.
            raw["matched_identity"] = normalized_identity(body).audit()
        elif identity_present is False:
            raw["matched_identity"] = {}
        return ProviderResult(
            fields=fields,
            confidence=_DECLARED_CONFIDENCE if status is ProviderStatus.SUCCESS else 0.0,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
            status=status,
            raw=raw,
        )


def _match_params(email: str) -> dict[str, str]:
    """The one identifier ARIE sends, plus the two opt-outs it always sends.

    The reveal flags default to ``false`` at Apollo's end too, so sending them
    is redundant *today*. They are sent explicitly because they are the two
    parameters that change what this call costs (a revealed mobile number is
    eight extra credits) and what personal data it returns, and a vendor
    changing its own default should not be able to change either without this
    line changing first.
    """
    return {
        "email": email,
        "reveal_personal_emails": "false",
        "reveal_phone_number": "false",
    }


def _status_error_kind(status_code: int) -> str | None:
    """Map an HTTP status onto a stable ``error_kind``, or ``None`` for 200.

    A table rather than ``live_abstract``'s inline ``if`` ladder because there
    is now a second adapter to keep consistent with it: the same vocabulary
    (``authentication_failed``, ``rate_limited``, ``server_error``) reaches the
    ledger and the receipt from both, so an operator reads one set of strings
    rather than one per vendor.
    """
    if status_code == 200:
        return None
    if status_code in (401, 403):
        # Per Apollo's documented table: 401 is a bad key, 403 is a plan
        # without API access (API_INACCESSIBLE) — both credential/plan
        # problems a retry cannot fix.
        return "authentication_failed"
    if status_code == 402:
        # Not in Apollo's documented status table (which says itself it is
        # incomplete) — 402 is the conventional insufficient-credits status,
        # mapped explicitly so an exhausted allowance lands on the error kind
        # the quota cooldown watches instead of on `unexpected_status:402`,
        # which would re-dial a dead quota on every lead.
        return "quota_exhausted"
    if status_code == 429:
        return "rate_limited"
    if status_code == 422:
        # Apollo returns 422 for an unprocessable match request. Named
        # separately from a generic 4xx because it means "this request will
        # never work as written", which is a code/identity problem rather than
        # a transient one.
        return "unprocessable_request"
    if status_code >= 500:
        return "server_error"
    return f"unexpected_status:{status_code}"


def _person_matched(body: dict[str, Any]) -> bool:
    """Whether Apollo returned a person at all.

    ``{"person": null}`` is Apollo's documented no-match response and arrives
    as a 200. An entirely empty body is treated the same way. Kept separate
    from ``arie.providers.apollo_contract._person_payload``'s unwrapping, which
    answers "where are the fields?" rather than "was there a match?" — the
    second question is this module's, because only this module knows that the
    answer decides whether a credit was consumed.
    """
    if "person" in body:
        return isinstance(body["person"], dict) and bool(body["person"])
    return bool(body.get("title") or body.get("seniority") or body.get("departments"))
