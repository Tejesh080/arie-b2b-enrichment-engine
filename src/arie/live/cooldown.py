"""Quota cooldown — stop re-dialling a provider whose credits are gone.

**The failure this prevents.** A vendor whose monthly allowance is exhausted
answers every call with the same quota error until the allowance resets — for
Apollo and Hunter that can be weeks away. Without a cooldown, every live lead
pays that provider's timeout-or-error latency, writes another identical error
row, and (for a vendor that rate-limits error responses too) can escalate a
dead quota into a rate-limit storm. The brief's words: no retry storm, no
repeated credit-exhaustion calls.

**Read from the ledger, like the spend caps — not from process memory.** A
worker-local "unavailable until" flag resets on every deploy and is invisible
to sibling workers, so one quota wall would be rediscovered once per process
per restart. ``provider_calls.error_kind`` (migration 0010) makes the wall
durable: the guard asks "did this provider return a quota error inside the
cooldown window?" with one indexed query against the same table
``arie.live.budget`` already reads. Same table, same trust model, same
cross-worker semantics — and nothing new to keep consistent.

**What counts as a quota error.** ``quota_exhausted`` (Apollo 402, Hunter 429
— Hunter's documented meaning for 429 *is* plan-quota exceeded) and
``insufficient_credits`` (Abstract 422). Deliberately not ``rate_limited``: a
rate limit is transient back-pressure that clears in seconds and needs no
hour-long lockout — cooling a provider for an hour because a burst grazed
15 req/s would throw away a healthy vendor.

**Fail closed toward calling, loud toward errors.** If the window has no quota
row, the provider is callable — including the very first call after a restart,
and the first probe after a cooldown expires (which is how a topped-up account
is rediscovered without anyone flipping a flag). If the ledger itself cannot
be answered, the exception propagates into the ordinary job retry path, same
as ``LiveSpendGuard``: a guard that silently fails open or closed is worse
than one that fails visibly.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.config import LIVE_STRATEGY, LiveStrategyConfig

__all__ = ["PROVIDER_UNAVAILABLE", "QUOTA_ERROR_KINDS", "ProviderCooldownGuard"]

QUOTA_ERROR_KINDS: frozenset[str] = frozenset({"quota_exhausted", "insufficient_credits"})
"""The ``provider_calls.error_kind`` values that mean "the account's allowance
is spent" — the durable condition worth a cooldown, as opposed to transient
transport or rate-limit noise. Matches migration 0010's partial index exactly,
so the guard's query stays an index-only lookup."""

PROVIDER_UNAVAILABLE = "provider_unavailable"
"""The stop reason a lead records when acquisition ended with a provider
skipped for cooldown and nothing better to report — a distinct claim from
``provider_failed`` (a provider broke *on this lead*) and from
``all_providers_called`` (which would assert a provider was consulted when it
deliberately was not)."""

_SELECT_LAST_QUOTA_ERROR = """
    SELECT max(completed_at) AS last_quota_error
    FROM provider_calls
    WHERE provider = %(provider)s
      AND error_kind = ANY(%(kinds)s)
      AND completed_at > now() - make_interval(secs => %(window_seconds)s)
"""


class ProviderCooldownGuard:
    """Answers "is this provider inside a quota cooldown right now?".

    Holds no mutable state — every check re-reads the ledger, for the same
    reason ``LiveSpendGuard`` does: cached unavailability is unavailability
    that silently stops being true.
    """

    def __init__(self, pool: ConnectionPool, config: LiveStrategyConfig | None = None) -> None:
        self._pool = pool
        self._config = config if config is not None else LIVE_STRATEGY

    @property
    def cooldown_seconds(self) -> float:
        return self._config.quota_cooldown_seconds

    def cooling_down_until(self, provider_name: str) -> datetime | None:
        """When this provider becomes callable again, or ``None`` if it is now.

        ``None`` whenever the cooldown is disabled (``0`` seconds) or no quota
        error landed inside the window. Otherwise the last quota error's
        timestamp plus the window — purely informational (the caller only
        branches on truthiness), but putting a *time* on the skip is what lets
        the audit trail say "Apollo returns at 14:32" instead of "Apollo is
        off".
        """
        window = self._config.quota_cooldown_seconds
        if window <= 0:
            return None
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_LAST_QUOTA_ERROR,
                {
                    "provider": provider_name,
                    "kinds": sorted(QUOTA_ERROR_KINDS),
                    "window_seconds": window,
                },
            )
            row = cur.fetchone()
        assert row is not None  # aggregate query always returns one row
        last: datetime | None = row["last_quota_error"]
        if last is None:
            return None
        return last + timedelta(seconds=window)
