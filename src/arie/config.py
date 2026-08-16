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
    return float(raw) if raw else default


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    return int(raw) if raw else default


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
