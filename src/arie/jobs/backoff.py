"""Retry backoff — exponential with full jitter.

Pure and DB-free, deliberately: how long to wait before retrying a failed job
shouldn't need a database connection to compute, and keeping it pure is what
makes the growth curve and jitter bounds directly unit-testable without a live
queue.

Uses the "full jitter" algorithm (AWS's own recommendation over plain or
"equal" jitter): the delay is a uniform random draw over ``[0, cap)`` rather
than a fixed exponential value plus a small wobble. Full jitter spreads
retries out the most — the property that actually matters here, since a batch
of jobs failing together (e.g. a transient DB hiccup) retrying in lockstep
would just recreate the contention that caused the failure.
"""

from __future__ import annotations

import random

DEFAULT_BASE_SECONDS = 2.0
DEFAULT_CAP_SECONDS = 300.0
"""Five minutes — long enough that a persistently-failing job stops hammering
the system, short enough that a real transient failure recovers within a
worker's typical lifetime."""


def backoff_cap(
    attempt: int,
    *,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    cap_seconds: float = DEFAULT_CAP_SECONDS,
) -> float:
    """The exponential ceiling for one attempt, before jitter is applied.

    ``attempt`` is 1-indexed (the delay *before* the 1st retry, i.e. after the
    1st failure). Exposed separately from ``compute_backoff`` so tests can
    pin the deterministic growth curve independently of randomness.
    """
    if attempt < 1:
        raise ValueError(f"attempt must be >= 1, got {attempt}")
    return min(cap_seconds, base_seconds * (2.0 ** (attempt - 1)))


def compute_backoff(
    attempt: int,
    *,
    base_seconds: float = DEFAULT_BASE_SECONDS,
    cap_seconds: float = DEFAULT_CAP_SECONDS,
    rng: random.Random | None = None,
) -> float:
    """Seconds to wait before the next retry, with full jitter applied.

    ``rng`` is injectable (defaults to the module-level ``random`` instance)
    so tests can seed it for reproducibility rather than asserting on ranges
    alone.
    """
    cap = backoff_cap(attempt, base_seconds=base_seconds, cap_seconds=cap_seconds)
    if rng is not None:
        return rng.uniform(0.0, cap)
    return random.uniform(0.0, cap)
