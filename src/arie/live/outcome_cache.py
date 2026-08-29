"""The provider-outcome suppression guard — stop re-buying a settled miss or
partial answer.

**The gap this closes.** The evidence-freshness cache (``_DurableEvidenceCache``
and the equivalent inline checks in ``arie.jobs.handlers``) only recognises a
provider as "already answered" when *every* field it declares is held, fresh,
from that provider. That is correct for the fields it covers, but it has no
opinion about the two cases where a provider leaves the check unsatisfied on
purpose:

* **A genuine miss.** The provider found nothing, so there is no evidence row
  to hold — the freshness check can never see a miss as "already answered,"
  and without this guard every identical follow-up request re-buys the same
  miss, forever.
* **A partial success.** The provider mapped *some* declared fields and left
  another genuinely unmappable (Hunter's ``role="executive"`` case: a real
  answer, correctly left ``UNKNOWN`` rather than guessed). The freshness
  check requires *all* declared fields to be held before it calls the
  provider "already answered," so a partial success — which is still real,
  paid-for information — gets treated as "not yet answered" and re-bought.

Both were confirmed live in the 2026-08-29 abstract-hunter-live-1 experiment
(the Jason Fried cache test): the fix is not "cache more values" — there is
nothing more to cache, ``UNKNOWN`` is not a value — it is "remember that we
already asked and this is what happened," independent of whether an answer
produced a value.

**Read from the ledger, like the quota-cooldown guard.** Same reasoning as
``arie.live.cooldown.ProviderCooldownGuard``: a worker-local flag resets on
every deploy and is invisible to sibling workers, so the durable signal has
to live in ``provider_calls`` — the same table, the same trust model, one
more indexed query alongside the one the cooldown guard already runs.

**What this guard does NOT decide.** It answers "was there a recent settled
miss for this exact provider+entity?" — nothing about partial-success reuse,
which is a evidence-store question (own_rows non-empty) the caller already
has the answer to before ever reaching this guard. See the call sites in
``arie.jobs.handlers`` for how the two combine.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.config import LIVE_OUTCOME_CACHE, LiveOutcomeCacheConfig
from arie.core.types import EntityType, ProviderStatus

__all__ = [
    "RECENT_MISS",
    "RECENT_PARTIAL",
    "ProviderOutcomeGuard",
    "RecentMiss",
]

RECENT_MISS = "recent_miss"
"""``provider_calls.suppressed_reason`` value for a call skipped because a
recent MISS is still inside its TTL — see migration 0011."""

RECENT_PARTIAL = "recent_partial"
"""``provider_calls.suppressed_reason`` value for a call skipped because this
provider's own prior answer already covers *some* (not necessarily all) of
its declared fields, still fresh — see migration 0011."""

_SELECT_LAST_MISS = """
    SELECT max(completed_at) AS last_miss
    FROM provider_calls
    WHERE provider = %(provider)s
      AND entity_type = %(entity_type)s
      AND entity_id = %(entity_id)s
      AND status = %(miss_status)s
      AND completed_at > now() - make_interval(secs => %(window_seconds)s)
"""


@dataclass(frozen=True)
class RecentMiss:
    """A settled miss still inside its suppression window."""

    since: datetime
    """When the miss was recorded."""
    until: datetime
    """When this provider becomes askable again for this entity."""


class ProviderOutcomeGuard:
    """Answers "did this provider already, recently, come up empty for this
    entity?" — read fresh from the ledger on every call, never cached in
    process memory, for the same reason ``LiveSpendGuard`` and
    ``ProviderCooldownGuard`` never are: a locally-cached "still empty" stops
    being true the moment another worker (or a fresh TTL window) changes it.
    """

    def __init__(self, pool: ConnectionPool, config: LiveOutcomeCacheConfig | None = None) -> None:
        self._pool = pool
        self._config = config if config is not None else LIVE_OUTCOME_CACHE

    @property
    def miss_ttl_seconds(self) -> float:
        return self._config.miss_ttl_seconds

    def recent_miss(
        self, provider_name: str, entity_type: EntityType, entity_id: UUID
    ) -> RecentMiss | None:
        """``None`` if this provider is askable now — including the very
        first ask, and the first ask after a suppression window has expired.
        """
        window = self._config.miss_ttl_seconds
        if window <= 0:
            return None
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_LAST_MISS,
                {
                    "provider": provider_name,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "miss_status": str(ProviderStatus.MISS),
                    "window_seconds": window,
                },
            )
            row = cur.fetchone()
        assert row is not None  # aggregate query always returns one row
        since: datetime | None = row["last_miss"]
        if since is None:
            return None
        return RecentMiss(since=since, until=since + timedelta(seconds=window))
