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

    @property
    def configured(self) -> bool:
        return bool(self.api_key)


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

    $0.05 is ~30 Abstract calls, or comfortable headroom for one Abstract call
    plus one person-enrichment call once a second provider exists (Apollo's
    published per-credit pricing sits well under a cent at plan volumes). A
    single lead needing more than this is a bug — a retry loop, a cache that is
    not being read — and the honest response is to stop and ask a human, which
    is exactly what exhausting it does."""

    def __post_init__(self) -> None:
        if self.per_lead_usd > self.daily_usd:
            raise ValueError(
                f"LIVE_PROVIDER_PER_LEAD_BUDGET_USD={self.per_lead_usd} exceeds "
                f"LIVE_PROVIDER_DAILY_BUDGET_USD={self.daily_usd} — one lead could "
                "consume the entire daily budget, which makes the daily cap meaningless"
            )
        if self.daily_usd < 0 or self.per_lead_usd < 0:
            raise ValueError("live budget caps must not be negative")


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


# Module-level singletons for convenience. Construct fresh instances directly
# (e.g. `PolicyConfig()`) when a test needs to observe patched environment values.
POLICY = PolicyConfig()
DATABASE = DatabaseConfig()
RUNTIME = RuntimeConfig()
OBSERVABILITY = ObservabilityConfig()
LLM = LLMConfig()
LIVE_PROVIDER = LiveProviderConfig()
LIVE_BUDGET = LiveBudgetConfig()
