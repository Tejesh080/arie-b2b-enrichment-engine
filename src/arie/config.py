"""Central configuration.

Policy parameters live here rather than being scattered across call sites so that
a benchmark run can state, in one object, exactly what configuration produced a
given result. Reproducibility depends on this.

All defaults use ``default_factory`` rather than direct calls. That matters: a
plain ``os.getenv(...)`` default is evaluated once at import time, so tests that
patch the environment would silently read stale values, and the config could not
be re-derived without reimporting the module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Literal, cast

from dotenv import load_dotenv

load_dotenv()

ProviderMode = Literal["simulated", "live"]


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r} is not a valid number") from exc


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{key}={raw!r} is not a valid integer") from exc


@dataclass(frozen=True)
class PolicyConfig:
    """Parameters of the enrichment decision algorithm.

    ``lead_budget_usd_cap`` is intentionally redundant with the EVoI stopping
    rule. If the EVoI estimate is miscalibrated and wants to keep spending, the
    hard cap stops it anyway — a policy bug should cost a bounded amount.
    """

    lead_budget_usd_cap: float = field(
        default_factory=lambda: _env_float("LEAD_BUDGET_USD_CAP", 1.50)
    )
    """Hard ceiling on spend per lead.

    Must sit **above** the cost of calling every provider once, or it stops
    being a backstop and becomes a binding constraint: the policy could never
    reach full information, so "matches full enrichment at lower cost" would be
    structurally unattainable rather than empirically false. An earlier default
    of $0.50 sat below the most expensive provider's price and silently made
    that provider unbuyable."""
    target_autonomous_error_rate: float = field(
        default_factory=lambda: _env_float("TARGET_AUTONOMOUS_ERROR_RATE", 0.10)
    )
    """Error budget for decisions taken without a human.

    Raised from 0.05 after enlarging the calibration split showed 5% to be
    unachievable: on several seeds the threshold search correctly refuses every
    operating point and automates nothing. The earlier 5% result that *did*
    find a threshold was small-sample luck — with four times the evaluation
    data, the top confidence block has a measured ~8% error rate against a
    stated ~4%.

    This is a business policy parameter rather than a result, and the trade is
    stated rather than hidden: 10% is met with roughly 45% coverage and is
    stable across seeds; 5% is met by refusing to automate at all. A system
    that automates nothing is not safer, it is just useless."""
    latency_penalty_usd_per_sec: float = field(
        default_factory=lambda: _env_float("LATENCY_PENALTY_USD_PER_SEC", 0.01)
    )

    # Decision-confidence thresholds. These are *defaults*: the conformal
    # procedure in arie.confidence replaces `auto_route_threshold` with a value
    # derived from the calibration split. A hardcoded threshold that never gets
    # replaced would be exactly the anti-pattern this project argues against.
    auto_route_threshold: float = 0.90
    escalate_floor: float = 0.60

    max_enrichment_steps: int = 8


@dataclass(frozen=True)
class DatabaseConfig:
    url: str = field(default_factory=lambda: os.getenv("DATABASE_URL", ""))
    direct_url: str = field(default_factory=lambda: os.getenv("DATABASE_DIRECT_URL", ""))


@dataclass(frozen=True)
class IntegrationDatabaseConfig:
    """Where the integration suite is allowed to write. Never ``DATABASE_URL``.

    Named for the *suite* rather than starting with ``Test``: pytest collects
    any module-level ``Test*`` class as a test class, so the obvious name emits
    a ``PytestCollectionWarning`` on every run of every file that imports it.

    **This class exists because a fallback almost destroyed production data.**
    The integration fixtures used to read ``DatabaseConfig``, which on any
    developer machine with a populated ``.env`` is the *deployed* database. Two
    consequences followed, and the second is worse than the first: the tests
    wrote and deleted rows in a live database, and the deployed worker — polling
    that same database — claimed the tests' jobs within about a second of
    ingestion and processed them with its own handlers, so assertions failed for
    reasons unrelated to the code under test.

    Reading a *different* variable is what makes the first problem structural
    rather than procedural. There is deliberately no fallback: if
    ``TEST_DATABASE_URL`` is unset the integration suite skips with an
    explanation, and no combination of environment can route it back to
    ``DATABASE_URL``. "Remember to override the URL before running tests" is
    not a safety mechanism; not having the URL is.

    Two further guards live in ``tests/integration/conftest.py`` — see
    ``ARIE_ALLOW_INTEGRATION_TEST_DB`` and the designation marker — because
    they are checks on a *live connection*, which config cannot perform.
    """

    url: str = field(default_factory=lambda: os.getenv("TEST_DATABASE_URL", ""))
    direct_url: str = field(
        default_factory=lambda: (
            os.getenv("TEST_DATABASE_DIRECT_URL", "") or os.getenv("TEST_DATABASE_URL", "")
        )
    )
    """Falls back to ``url``, and only to ``url``. The pooled/direct split
    matters solely against Supabase's transaction pooler (see
    ``scripts/migrate.py``); for an ordinary Postgres — which every supported
    test target is — they are the same server, and requiring both would be two
    variables to get out of sync for no benefit."""

    allow: bool = field(
        default_factory=lambda: os.getenv("ARIE_ALLOW_INTEGRATION_TEST_DB", "") == "1"
    )
    """Explicit operator intent, separate from merely having a URL configured.

    A URL can be inherited from a shell, a CI secret, or a stale export. This
    cannot be inherited by accident in the same way: it is meaningless outside
    the integration suite, so its only reason to be set is that someone meant
    to run destructive tests against the database it accompanies.
    """

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class SupabaseAuthConfig:
    """Verification config for Supabase-issued user session tokens.

    Productization M1's auth model (`arie.auth`): a Supabase JWT bearer token
    plus an `X-Organization-Id` header, checked against `organization_members`.

    Verified against Supabase's published JWKS (`jwks_url`), not a shared
    secret. This project's Supabase Auth signing keys (Dashboard -> Settings
    -> JWT Keys) are the newer asymmetric kind — current key ECC/P-256
    (ES256), with the legacy HS256 shared secret retained only as the
    *previous* key for rotation continuity — confirmed directly, not assumed.
    A `SUPABASE_JWT_SECRET`/HS256 verifier (this config's original shape)
    could never have verified a token those keys sign: wrong algorithm
    entirely, not merely a wrong secret. `SUPABASE_URL` is the same value
    already used for `SUPABASE_SERVICE_ROLE_KEY`'s project, not a new secret.
    """

    url: str = field(default_factory=lambda: os.getenv("SUPABASE_URL", "").rstrip("/"))
    service_role_key: str = field(
        default_factory=lambda: os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    )
    """Used only by `arie.supabase_admin` (Productization M6) to resolve a
    member's `user_id` to their account email for transactional notifications
    — the Supabase Auth Admin API is the only source for that, since no table
    in this database stores it. Never sent to a frontend, never logged; see
    that module's own docstring."""

    @property
    def jwks_url(self) -> str:
        return f"{self.url}/auth/v1/.well-known/jwks.json"

    @property
    def issuer(self) -> str:
        return f"{self.url}/auth/v1"

    @property
    def configured(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class ObservabilityConfig:
    """Tracing configuration. Absent an endpoint, tracing is off — see
    ``arie.observability.tracing``.

    There is deliberately no ``TRACING_ENABLED`` flag. Two switches for one
    behaviour (an endpoint *and* a boolean) is two things to get out of sync,
    and "enabled but pointed nowhere" is not a state worth being able to
    express. Configuring an endpoint is the act of enabling it.
    """

    service_name: str = field(default_factory=lambda: os.getenv("OTEL_SERVICE_NAME", "arie"))
    otlp_endpoint: str = field(
        default_factory=lambda: os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip()
    )

    @property
    def enabled(self) -> bool:
        return bool(self.otlp_endpoint)


@dataclass(frozen=True)
class LLMConfig:
    """DeepSeek client configuration for ``arie.llm`` signal extraction.

    See ``docs/architecture.md``'s Step 10 section for why this exists at all:
    one narrow task (buying-signal extraction from free text), never a general
    "call an LLM" facility.
    """

    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))
    deepseek_base_url: str = field(
        default_factory=lambda: os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip(
            "/"
        )
    )
    model: str = field(default_factory=lambda: os.getenv("DEEPSEEK_MODEL", "deepseek-chat"))
    """``deepseek-chat`` (cheap tier), not ``deepseek-reasoner``. Fixed, not a
    cascade: escalating between the two would be multi-model routing, which
    Step 10 is deliberately scoped to exclude — see the module docstring of
    ``arie.llm.deepseek``."""
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("DEEPSEEK_TIMEOUT_SECONDS", 30.0)
    )
    max_attempts: int = field(default_factory=lambda: _env_int("DEEPSEEK_MAX_ATTEMPTS", 3))
    """Total attempts (including the first), not retries on top of one. Bounded
    the same way ``RuntimeConfig.worker_max_attempts`` is — a miscalibrated
    prompt should cost a bounded amount, not loop."""

    @property
    def configured(self) -> bool:
        return bool(self.deepseek_api_key)


@dataclass(frozen=True)
class LiveProviderConfig:
    """Config for the one real enrichment adapter (P5) — ``arie.providers.live_abstract``.

    Abstract API's Company Enrichment endpoint (``docs.abstractapi.com/api/company-enrichment``):
    a plain ``GET`` with ``api_key``/``domain`` query params, returning firmographic
    fields including ``employee_count`` and ``industry`` — the same two fields the
    simulated ``firmographics_basic``/``firmographics_premium`` providers model.
    """

    api_key: str = field(default_factory=lambda: os.getenv("ABSTRACT_COMPANY_API_KEY", ""))
    base_url: str = field(
        default_factory=lambda: os.getenv(
            "ABSTRACT_COMPANY_BASE_URL", "https://companyenrichment.abstractapi.com/v2/"
        )
    )
    """The exact endpoint URL, used verbatim (not an httpx `base_url` a path
    gets joined onto) — unlike ``LLMConfig.deepseek_base_url``, there is
    nothing to strip a trailing slash *for*. Keep the trailing slash: Abstract
    canonicalizes `/v2` -> `/v2/` with a 301 that preserves the query string,
    confirmed by a live request during P5's verification — harmless if
    ``AbstractCompanyEnrichmentProvider``'s ``follow_redirects=True`` client
    ever needs to correct a misconfigured value, but avoiding the redirect
    round-trip entirely is better than relying on that."""
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("ABSTRACT_COMPANY_TIMEOUT_SECONDS", 10.0)
    )
    cost_usd_per_call: float = field(
        default_factory=lambda: _env_float("ABSTRACT_COMPANY_COST_USD_PER_CALL", 0.00165)
    )
    """An ESTIMATED unit cost, not a per-call price Abstract's response reports.

    Derived from the Standard plan's list price at time of writing ($99/month
    for 60,000 requests/month => ~$0.00165/request) — see ``docs/architecture.md``'s
    P5 section. Configurable because a list price changes; never presented as
    an exact, provider-reported cost the way ``arie.ledger.store`` treats a
    vendor's own billed figure."""

    min_request_interval_seconds: float = field(
        default_factory=lambda: _env_float("ABSTRACT_COMPANY_MIN_REQUEST_INTERVAL_SECONDS", 1.0)
    )
    """Client-side pacing between successive calls from one adapter instance
    — see ``arie.live.rate_limit.MinIntervalPacer``. **Not** a figure taken
    from Abstract's documentation: Abstract publishes a monthly volume cap
    (100 requests/month, free tier) and no requests-per-second number at all,
    so this default is a conservative estimate chosen after the 2026-08-29
    abstract-hunter-live-1 experiment hit a real ``rate_limited`` on the fifth
    of five unpaced sequential calls. ``0`` disables pacing entirely."""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class ApolloPersonConfig:
    """Config for the second real enrichment adapter — ``arie.providers.live_apollo``.

    Apollo's People Enrichment endpoint (``docs.apollo.io/reference/people-enrichment``):
    ``POST https://api.apollo.io/api/v1/people/match`` with an ``x-api-key`` header
    and the match identifiers as query parameters, returning one ``person`` object
    whose ``seniority``/``departments``/``title`` fields become ARIE's
    ``title_seniority`` and ``title_function`` — the 35 of the scorer's 100
    reachable points ``LiveProviderConfig``'s company-only provider can never reach.

    Separate from ``LiveProviderConfig`` rather than a second set of fields on it:
    the two vendors share no setting (different auth mechanism, different cost
    unit, different miss semantics), and one class holding both would need every
    field prefixed by vendor anyway.
    """

    api_key: str = field(default_factory=lambda: os.getenv("APOLLO_API_KEY", ""))
    """Sent as the ``x-api-key`` request header, never as a query parameter —
    unlike Abstract, Apollo offers a header mechanism, so the credential never
    reaches a URL that could be logged by a proxy or an exception's ``str()``."""

    base_url: str = field(
        default_factory=lambda: os.getenv(
            "APOLLO_BASE_URL", "https://api.apollo.io/api/v1/people/match"
        )
    )
    """The exact endpoint, used verbatim. Apollo versions its API in the path
    (``/api/v1/``), so an override is how you pin or move a version."""

    timeout_seconds: float = field(
        default_factory=lambda: _env_float("APOLLO_TIMEOUT_SECONDS", 10.0)
    )

    cost_usd_per_success: float = field(
        default_factory=lambda: _env_float("APOLLO_PERSON_COST_USD_PER_SUCCESS", 0.0196)
    )
    """A MODELLED USD equivalent of the credit Apollo actually consumes — not a
    dollar figure Apollo reports, and never to be read as billed spend.

    Apollo meters this endpoint in **credits**, not dollars: one credit for a
    demographics match, zero when no credit-consuming data is found, and an
    additional eight if a mobile phone number is returned (which
    ``arie.providers.live_apollo`` disables outright — see ``credits_per_match``).
    ARIE's ledger stores USD, so the credit is converted here, once, at a rate
    stated rather than assumed: Apollo's published Basic plan at time of writing
    is $49/user/month for 30,000 credits/year — 2,500/month — giving
    $49 / 2,500 = $0.0196/credit. The Professional and Organization plans work
    out within a twentieth of a cent of the same figure, so the number is not
    especially plan-sensitive.

    **This corrects an assumption, and the correction is the reason to state the
    derivation.** ``LiveBudgetConfig.per_lead_usd`` previously described Apollo's
    per-credit price as "well under a cent"; verifying the published plans while
    wiring this adapter put it at roughly two cents — an order of magnitude out.
    The $0.05 per-lead cap still comfortably covers one Abstract call plus one
    Apollo call ($0.02125 together), so nothing had to move; had the estimate
    been trusted instead of checked, the first live person enrichment would have
    silently sat much closer to the cap than anyone believed.

    What this figure is **not**: the amount an Apollo invoice will show. Credits
    are bought in monthly plan blocks and expire; a lead that consumes one credit
    has consumed a unit of an already-paid allowance, not $0.0196 of new spend.
    ARIE ledgers it as an acquisition cost because the acquisition policy needs a
    comparable number to reason about, and ``arie.api.receipt`` presents it the
    same way it presents Abstract's estimate. Reconciling either against a real
    vendor invoice is a separate exercise neither number claims to have done."""

    credits_per_match: int = 1
    """Credits Apollo consumes for one successful match under the request
    ``arie.providers.live_apollo`` actually sends.

    Not env-configurable: it is a property of Apollo's documented metering and of
    the request the adapter builds (``reveal_personal_emails=false``,
    ``reveal_phone_number=false``), not a knob. Recorded so the adapter can put
    the vendor's own unit on its result alongside the modelled dollars, and so
    that anyone enabling phone reveal has to change this line and read this
    docstring rather than discovering a 9x cost multiplier from an invoice."""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class HunterConfig:
    """Config for the third real enrichment adapter — ``arie.providers.live_hunter``.

    Hunter's Combined Enrichment endpoint (``hunter.io/api-documentation/v2``):
    ``GET https://api.hunter.io/v2/combined/find?email=...`` with an ``X-API-KEY``
    header, returning one Clearbit-style ``{person, company}`` pair. The person's
    ``employment.seniority``/``employment.role``/``employment.title`` become
    ARIE's ``title_seniority``/``title_function``; the company half rides along
    for the Abstract-overlap comparison and is deliberately not persisted as
    evidence yet — see ``arie.providers.live_hunter``'s module docstring.

    Combined rather than the narrower ``people/find`` because Hunter prices both
    in the same class (0.2 credits per enriched email) and combined returns the
    company data the provider bake-off needs to measure whether Hunter could
    ever stand in for Abstract — one call answers both questions.
    """

    api_key: str = field(default_factory=lambda: os.getenv("HUNTER_API_KEY", ""))
    """Sent as the ``X-API-KEY`` request header, never as a query parameter —
    Hunter documents both mechanisms, and the header keeps the credential out
    of URLs the way the Apollo adapter already does."""

    base_url: str = field(
        default_factory=lambda: os.getenv(
            "HUNTER_BASE_URL", "https://api.hunter.io/v2/combined/find"
        )
    )
    """The exact endpoint, used verbatim. Overriding it to
    ``.../v2/people/find`` yields the person-only variant with the same request
    shape (both take ``email``) and no company preview."""

    timeout_seconds: float = field(
        default_factory=lambda: _env_float("HUNTER_TIMEOUT_SECONDS", 10.0)
    )

    cost_usd_per_success: float = field(
        default_factory=lambda: _env_float("HUNTER_ENRICHMENT_COST_USD", 0.0049)
    )
    """A MODELLED USD equivalent of the 0.2 credits Hunter consumes for a
    successful enrichment — not a dollar figure Hunter reports, and never to be
    read as billed spend.

    Derivation, same discipline as ``ApolloPersonConfig.cost_usd_per_success``:
    Hunter's published Starter plan at time of writing is $49/month for 2,000
    credits/month => $0.0245/credit, and Hunter's credit table prices an API
    enrichment at 0.2 credits => $0.0049 per successful enrichment. Hunter's
    own help centre states no credits are consumed when nothing is found, so a
    miss is ledgered at zero — the same bill-on-match semantics as Apollo, and
    the opposite of Abstract's bill-every-lookup.

    That puts the three vendors in the price order the acquisition loop walks
    them: Abstract $0.00165, Hunter $0.0049, Apollo $0.0196 — cheapest first,
    person providers cheapest-first among themselves."""

    credits_per_success: float = 0.2
    """Hunter's own metering unit for one successful API enrichment. Not
    env-configurable: it is a property of Hunter's published credit table, not
    a knob. Recorded on the ledger row (``provider_calls.credits_used``) next
    to the modelled dollars so neither figure can be mistaken for the other."""

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True)
class LiveStrategyConfig:
    """How ``PROVIDER_MODE=live`` walks its providers — see ``arie.live.strategy``.

    Two modes, and the distinction is the point of this class existing:

    * ``optimized`` (the default) — providers are called selectively and
      sequentially, cheapest first, stopping as soon as the existing evidence/
      confidence logic says further evidence is unnecessary. This is ARIE's
      actual operating behaviour; calling every provider for every lead would
      defeat the system's purpose.
    * ``evaluation_parallel`` — a private, server-side experiment mode in which
      the person providers are deliberately called *concurrently for the same
      lead* so their coverage, quality, latency, and overlap can be measured
      against each other. It exists to gather the data that will justify (or
      overturn) the optimized order; it is not an operating mode, must never be
      the default, and must never be enabled for the anonymous public demo —
      which runs ``PROVIDER_MODE=simulated`` and structurally never reads this
      class (``arie.jobs.handlers._build_simulated_handlers`` has no reference
      to it, and a test pins that).

    **Deliberately not validated here.** Every other config class validates in
    ``__post_init__``; this one stores raw strings and lets
    ``arie.live.strategy.resolve_strategy`` do the checking, so that the value
    is only ever *read* — and can only ever fail — on the live path. A typo'd
    strategy on a simulated deployment (the public demo) must be inert, not an
    import-time crash of a process that would never have used it.
    """

    strategy: str = field(default_factory=lambda: os.getenv("LIVE_PROVIDER_STRATEGY", "optimized"))

    provider_order: str = field(default_factory=lambda: os.getenv("LIVE_PROVIDER_ORDER", ""))
    """Optional comma-separated provider names overriding the default
    (cheapest-first) acquisition order — an experiment knob for testing
    alternative waterfalls, validated against the registered names by
    ``arie.live.providers.acquisition_order``. Empty means the registered
    default. This is priority only: names listed come first in the given
    order, registered providers not listed keep their relative order after.
    It cannot *exclude* a provider — a live worker runs every registered
    adapter or does not start."""

    quota_cooldown_seconds: float = field(
        default_factory=lambda: _env_float("LIVE_PROVIDER_QUOTA_COOLDOWN_SECONDS", 3600.0)
    )
    """How long a provider that hit a credit/quota wall is left uncalled.

    Enforced by ``arie.live.cooldown.ProviderCooldownGuard`` against the same
    durable ledger the spend caps read, so it holds across workers and
    restarts. An hour by default: long enough that an exhausted monthly quota
    is probed ~24 times a day instead of once per lead (no retry storm, no
    per-lead latency tax), short enough that a topped-up account resumes the
    same hour. ``0`` disables the cooldown entirely."""


@dataclass(frozen=True)
class LiveOutcomeCacheConfig:
    """How long a *settled* provider outcome — a miss, a success that left
    some declared field genuinely unmapped, or an uncertain paid-call
    outcome — is trusted without asking again.

    Distinct from ``ttl_for_field`` (``arie.evidence.ttl_policy``), which
    governs how long an actual *value* stays fresh once a provider supplies
    one. This governs the case that policy cannot: a provider that answered
    and had *nothing new to add* for one or more declared fields leaves no
    evidence row to expire, so without this a moment-later identical request
    re-buys the same answer forever. See ``arie.live.outcome_cache`` for the
    guard that reads this.
    """

    miss_ttl_seconds: float = field(
        default_factory=lambda: _env_float("PROVIDER_MISS_CACHE_TTL_SECONDS", 30.0)
    )
    """A provider that found nothing for this entity is not re-asked for this
    long. Conservative and short by design (30s default): a miss is far more
    likely to reflect a real, durable "this vendor doesn't have this record"
    than a quota wall (which has its own, much longer,
    ``LiveStrategyConfig.quota_cooldown_seconds`` cooldown) — but it is still
    a *belief*, not a fact, and a short window bounds how long ARIE can be
    wrong about a vendor that just started indexing a new record. ``0``
    disables miss suppression entirely (every request re-asks)."""

    uncertain_outcome_ttl_seconds: float = field(
        default_factory=lambda: _env_float("PROVIDER_UNCERTAIN_OUTCOME_CACHE_TTL_SECONDS", 3600.0)
    )
    """Productization M5 Part 7 (retry safety). A provider call whose
    transport outcome was genuinely uncertain — a timeout, or a connection-
    level error, where ARIE cannot tell whether the vendor received (and
    possibly began billing-relevant processing of) the request — is not
    automatically re-attempted for this long. Deliberately much longer than
    ``miss_ttl_seconds``: a miss is a known, settled, cheap-to-repeat fact;
    an uncertain outcome might already have cost money, so the bar for
    letting a worker retry blindly re-issue it is the same order of
    magnitude as the quota cooldown, not the 30-second miss window. ``0``
    disables this suppression entirely (every retry re-asks, the pre-M5
    behaviour). This does not affect a *definite* result (a real HTTP
    response, success/miss/rejection alike) — only genuinely ambiguous
    transport failures."""


@dataclass(frozen=True)
class LiveBudgetConfig:
    """Hard spend ceilings for ``PROVIDER_MODE=live`` (Live V1 Foundation, Phase 6).

    Enforced by ``arie.live.budget.LiveSpendGuard`` *before* any real provider
    call, against the durable ``provider_calls`` ledger. Distinct from
    ``PolicyConfig.lead_budget_usd_cap``, which bounds the *simulated*
    catalogue's fictional prices inside the EVoI loop and is sized to sit above
    the cost of calling all eight simulated providers. Conflating the two would
    force one number to be both "generous enough that the benchmark can reach
    full information" and "tight enough that a live retry storm cannot empty a
    real account".

    **Server-side only.** Nothing here is prefixed ``NEXT_PUBLIC_``/``VITE_``,
    nothing is echoed by an API response, and the frontend lives in a separate
    repository with no access to this process's environment. A spend cap the
    browser can read is a spend cap an attacker knows the exact shape of.
    """

    daily_usd: float = field(
        default_factory=lambda: _env_float("LIVE_PROVIDER_DAILY_BUDGET_USD", 2.00)
    )
    """Account-wide ceiling per UTC day, across every live provider.

    $2.00 at Abstract's estimated $0.00165/call is roughly 1,200 enrichments a
    day — far above any plausible demo or pilot volume, and far below an amount
    worth losing to a stuck queue overnight. Deliberately a *small* number: the
    default should be safe for someone who deploys without reading this
    docstring, and raising it is a one-line env change for someone who has."""

    per_lead_usd: float = field(
        default_factory=lambda: _env_float("LIVE_PROVIDER_PER_LEAD_BUDGET_USD", 0.05)
    )
    """Ceiling for one lead's total live enrichment.

    $0.05 is ~30 Abstract calls, or comfortable headroom for the full live
    acquisition path — one Abstract company call ($0.00165) plus one Apollo
    person call ($0.0196) is $0.02125, well under half the cap. That Apollo
    figure is a *verified* one and it replaces an earlier guess in this very
    docstring that its credits cost "well under a cent"; see
    ``ApolloPersonConfig.cost_usd_per_success`` for the derivation and why the
    correction did not require moving this number. A single lead needing more
    than this is a bug — a retry loop, a cache that is not being read — and the
    honest response is to stop and ask a human, which is exactly what
    exhausting it does."""

    evaluation_per_lead_usd: float = field(
        default_factory=lambda: _env_float("LIVE_EVALUATION_PER_LEAD_BUDGET_USD", 0.10)
    )
    """Per-lead ceiling for the ``evaluation_parallel`` strategy, which
    *deliberately* calls overlapping providers and therefore spends more per
    lead than optimized mode ever should. A separate, explicit number rather
    than a bypass: evaluation runs still go through the same ``LiveSpendGuard``
    with the same predictive check and the same shared daily cap — only the
    per-lead figure differs, and only in the mode that documents why. $0.10
    covers the full three-provider sweep ($0.00165 + $0.0049 + $0.0196 ≈
    $0.026) with headroom, while still bounding a pathological lead at a dime."""

    def __post_init__(self) -> None:
        if self.per_lead_usd > self.daily_usd:
            raise ValueError(
                f"LIVE_PROVIDER_PER_LEAD_BUDGET_USD={self.per_lead_usd} exceeds "
                f"LIVE_PROVIDER_DAILY_BUDGET_USD={self.daily_usd} — one lead could "
                "consume the entire daily budget, which makes the daily cap meaningless"
            )
        if self.daily_usd < 0 or self.per_lead_usd < 0 or self.evaluation_per_lead_usd < 0:
            raise ValueError("live budget caps must not be negative")

    def for_evaluation(self) -> LiveBudgetConfig:
        """This config with the evaluation per-lead cap in the driving seat.

        The evaluation handler builds its ``LiveSpendGuard`` from this, so the
        guard's arithmetic, refusal vocabulary, and daily ceiling are exactly
        the production ones — a separate budget, never a separate code path.

        The exceeds-daily check lives here rather than in ``__post_init__``,
        deliberately: the evaluation cap's default (a dime) only has to fit
        under the daily cap in the one mode that spends it. Validating it
        eagerly would make ``LiveBudgetConfig(daily_usd=0.0, per_lead_usd=0.0)``
        — the legitimate "block all spending" configuration, which several
        tests and any panic-stop deployment use — unconstructible because of a
        default for a mode not in use. Same lazy-where-live discipline as
        ``LiveStrategyConfig``; still loud at startup, because the evaluation
        builder calls this before the first job runs.
        """
        if self.evaluation_per_lead_usd > self.daily_usd:
            raise ValueError(
                f"LIVE_EVALUATION_PER_LEAD_BUDGET_USD={self.evaluation_per_lead_usd} exceeds "
                f"LIVE_PROVIDER_DAILY_BUDGET_USD={self.daily_usd} — one evaluation lead "
                "could consume the entire daily budget"
            )
        return LiveBudgetConfig(
            daily_usd=self.daily_usd,
            per_lead_usd=self.evaluation_per_lead_usd,
            evaluation_per_lead_usd=self.evaluation_per_lead_usd,
        )


@dataclass(frozen=True)
class RuntimeConfig:
    provider_mode: ProviderMode = field(
        default_factory=lambda: cast(ProviderMode, os.getenv("PROVIDER_MODE", "simulated"))
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))
    worker_poll_interval_sec: int = field(
        default_factory=lambda: _env_int("WORKER_POLL_INTERVAL_SEC", 2)
    )
    worker_max_attempts: int = field(default_factory=lambda: _env_int("WORKER_MAX_ATTEMPTS", 4))
    worker_lease_seconds: int = field(default_factory=lambda: _env_int("WORKER_LEASE_SECONDS", 300))
    """How long a claimed job stays locked before another worker may reclaim it.

    Protects against a worker that crashes or is killed mid-job: without this,
    a job claimed by a dead worker would stay `processing` forever. Five
    minutes is generous relative to expected job duration (no real handler
    calls a network provider yet — see arie.jobs.worker) and cheap to widen
    later; the failure mode of too-short a lease (two workers both process the
    same job) is worse than the failure mode of too-long one (a dead worker's
    job waits a few extra minutes to be reclaimed)."""


@dataclass(frozen=True)
class StripeConfig:
    """Stripe payment/subscription authority config (Productization M6).

    Nothing here is ever echoed by an API response or a frontend
    `NEXT_PUBLIC_` variable — see `arie.billing.stripe_gateway`'s own
    docstring for why every Stripe call is made from this server process
    only, never the browser.
    """

    secret_key: str = field(default_factory=lambda: os.getenv("STRIPE_SECRET_KEY", ""))
    webhook_secret: str = field(default_factory=lambda: os.getenv("STRIPE_WEBHOOK_SECRET", ""))
    price_starter: str = field(default_factory=lambda: os.getenv("STRIPE_PRICE_STARTER", ""))
    price_growth: str = field(default_factory=lambda: os.getenv("STRIPE_PRICE_GROWTH", ""))
    price_pro: str = field(default_factory=lambda: os.getenv("STRIPE_PRICE_PRO", ""))

    @property
    def configured(self) -> bool:
        return bool(self.secret_key)

    @property
    def webhook_configured(self) -> bool:
        return bool(self.webhook_secret)

    def price_id_for_plan(self, plan: str) -> str | None:
        """The env-configured Stripe Price id for an internal plan name, or
        `None` if unset. **The only place a browser-submitted plan name is
        ever translated into a Stripe price id** — see
        `arie.billing.service.start_checkout`'s own docstring for why a
        client-supplied price id is never trusted directly."""
        return {
            "starter": self.price_starter,
            "growth": self.price_growth,
            "pro": self.price_pro,
        }.get(plan) or None


@dataclass(frozen=True)
class EmailConfig:
    """Transactional email provider config (Productization M6 Part 13) —
    AhaSend (`https://api.ahasend.com/v2/`). See `arie.email.ahasend` for the
    HTTP client; `configured` gates whether it is used at all versus
    `arie.email.fake.FakeEmailSender` (the default absent both values, safe
    for local dev and CI — no email ever leaves the process)."""

    ahasend_api_key: str = field(default_factory=lambda: os.getenv("AHASEND_API_KEY", ""))
    ahasend_account_id: str = field(default_factory=lambda: os.getenv("AHASEND_ACCOUNT_ID", ""))
    from_email: str = field(
        default_factory=lambda: os.getenv("EMAIL_FROM_ADDRESS", "notifications@arie.invalid")
    )
    from_name: str = field(default_factory=lambda: os.getenv("EMAIL_FROM_NAME", "ARIE"))
    timeout_seconds: float = field(
        default_factory=lambda: _env_float("AHASEND_TIMEOUT_SECONDS", 10.0)
    )

    @property
    def configured(self) -> bool:
        return bool(self.ahasend_api_key and self.ahasend_account_id)


@dataclass(frozen=True)
class TurnstileConfig:
    """Cloudflare Turnstile config (Productization M6 Part 12) — abuse
    protection for self-service organization provisioning. `site_key` is
    public (safe for the frontend widget); `secret_key` never is.
    `configured` being `False` (no account set up yet) is a deliberate,
    documented dev/CI bypass — see `arie.turnstile`'s own docstring — never a
    silent bypass in an environment where it *is* configured."""

    secret_key: str = field(default_factory=lambda: os.getenv("TURNSTILE_SECRET_KEY", ""))
    site_key: str = field(default_factory=lambda: os.getenv("TURNSTILE_SITE_KEY", ""))

    @property
    def configured(self) -> bool:
        return bool(self.secret_key)


@dataclass(frozen=True)
class FrontendConfig:
    """Where the customer-facing console is hosted — used only to build
    absolute links in transactional email (an invitation accept URL, a
    review-required URL) when the caller doesn't supply its own return URL.
    Not a secret; not read by any provider-selection or scoring code."""

    base_url: str = field(default_factory=lambda: os.getenv("FRONTEND_BASE_URL", "").rstrip("/"))


@dataclass(frozen=True)
class NotificationConfig:
    """Thresholds/cadence for `arie.email` notification triggers — centralized
    per Productization M6 Part 15's "pick a sensible threshold... but
    centralize/configure it" instruction."""

    usage_warning_threshold: float = field(
        default_factory=lambda: _env_float("USAGE_WARNING_THRESHOLD_PCT", 0.8)
    )
    """Fraction of a monthly limit (leads or modeled spend) that triggers one
    `send_usage_warning` email per organization per calendar month — see
    `arie.billing.notifications` for the dedup rule."""


@dataclass(frozen=True)
class WorkerHeartbeatConfig:
    """Productization M6 Part 28 — a lightweight, DB-backed "is a worker
    process actually alive" signal, independent of `/healthz` (which only
    proves the API can reach the database, not that anything is consuming
    the job queue)."""

    interval_seconds: float = field(
        default_factory=lambda: _env_float("WORKER_HEARTBEAT_INTERVAL_SECONDS", 15.0)
    )
    stale_after_seconds: float = field(
        default_factory=lambda: _env_float("WORKER_HEARTBEAT_STALE_AFTER_SECONDS", 60.0)
    )
    """How long since the last heartbeat before `GET /healthz/worker` reports
    the worker as down. Generous relative to `interval_seconds` (4x default)
    so one slow poll cycle under load doesn't flap the monitor."""


# Module-level singletons for convenience. Construct fresh instances directly
# (e.g. `PolicyConfig()`) when a test needs to observe patched environment values.
POLICY = PolicyConfig()
DATABASE = DatabaseConfig()
SUPABASE_AUTH = SupabaseAuthConfig()
RUNTIME = RuntimeConfig()
OBSERVABILITY = ObservabilityConfig()
LLM = LLMConfig()
LIVE_PROVIDER = LiveProviderConfig()
APOLLO_PERSON = ApolloPersonConfig()
HUNTER = HunterConfig()
LIVE_STRATEGY = LiveStrategyConfig()
LIVE_OUTCOME_CACHE = LiveOutcomeCacheConfig()
LIVE_BUDGET = LiveBudgetConfig()
STRIPE = StripeConfig()
EMAIL = EmailConfig()
TURNSTILE = TurnstileConfig()
FRONTEND = FrontendConfig()
NOTIFICATIONS = NotificationConfig()
WORKER_HEARTBEAT = WorkerHeartbeatConfig()
