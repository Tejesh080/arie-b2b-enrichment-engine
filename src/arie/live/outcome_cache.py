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

**Productization M5 Part 7 — a third question: "was the last attempt's
outcome even known?"** A miss or a partial success are both *settled*: the
vendor answered, the answer is on record, and re-asking is wasteful but
never unsafe to reason about. A timeout or a connection-level transport
failure is not settled — ARIE genuinely does not know whether the vendor
received the request (and, for a provider that bills on lookup rather than
on match, may already be counting it) before the connection dropped. Retrying
that blindly is the one class of "ask again" this module previously had no
opinion on, and the one most likely to cost money twice for one answer.
:meth:`ProviderOutcomeGuard.recent_uncertain_outcome` closes it the same way
:meth:`recent_miss` closes the settled case — read the ledger, suppress a
repeat inside the window — with its own, much longer, TTL
(``LiveOutcomeCacheConfig.uncertain_outcome_ttl_seconds``): a genuine unknown
deserves more caution than a known miss before a worker is allowed to try
again automatically.
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
    "UNCERTAIN_OUTCOME",
    "ProviderOutcomeGuard",
    "RecentMiss",
    "RecentUncertainOutcome",
    "is_uncertain_outcome",
]

RECENT_MISS = "recent_miss"
"""``provider_calls.suppressed_reason`` value for a call skipped because a
recent MISS is still inside its TTL — see migration 0011."""

RECENT_PARTIAL = "recent_partial"
"""``provider_calls.suppressed_reason`` value for a call skipped because this
provider's own prior answer already covers *some* (not necessarily all) of
its declared fields, still fresh — see migration 0011."""

UNCERTAIN_OUTCOME = "uncertain_outcome"
"""``provider_calls.suppressed_reason`` value for a call skipped because a
recent attempt's transport outcome was genuinely unknown (a timeout, or a
connection-level failure — see :func:`is_uncertain_outcome`) and is still
inside its suppression window — see migration 0029."""


def is_uncertain_outcome(error_kind: str | None) -> bool:
    """Whether `error_kind` (from ``ProviderResult.raw``/``provider_calls.
    error_kind``) means "the vendor's outcome for this attempt is unknown",
    as opposed to a *definite* result.

    A definite result is any real HTTP response, success or failure alike —
    the vendor was reached and answered, even if the answer was a rejection
    (``authentication_failed``, ``rate_limited``, ``quota_exhausted``, a
    5xx). Only the two failure modes where no response was ever received are
    uncertain: ``"timeout"`` (``httpx.TimeoutException``) and
    ``"transport_error:*"`` (``httpx.HTTPError`` at the connection level —
    every live adapter's shared vocabulary, see e.g.
    ``arie.providers.live_abstract``). Matches migration 0029's own
    predicate exactly, so the SQL and this pure-Python check can never
    disagree about which rows count.
    """
    if error_kind is None:
        return False
    return error_kind == "timeout" or error_kind.startswith("transport_error:")


_SELECT_LAST_MISS = """
    SELECT max(completed_at) AS last_miss
    FROM provider_calls
    WHERE organization_id = %(organization_id)s
      AND provider = %(provider)s
      AND entity_type = %(entity_type)s
      AND entity_id = %(entity_id)s
      AND status = %(miss_status)s
      AND completed_at > now() - make_interval(secs => %(window_seconds)s)
"""

_SELECT_LAST_UNCERTAIN_OUTCOME = """
    SELECT max(completed_at) AS last_uncertain
    FROM provider_calls
    WHERE organization_id = %(organization_id)s
      AND provider = %(provider)s
      AND entity_type = %(entity_type)s
      AND entity_id = %(entity_id)s
      AND (error_kind = 'timeout' OR error_kind LIKE 'transport_error:%%')
      AND completed_at > now() - make_interval(secs => %(window_seconds)s)
"""


@dataclass(frozen=True)
class RecentMiss:
    """A settled miss still inside its suppression window."""

    since: datetime
    """When the miss was recorded."""
    until: datetime
    """When this provider becomes askable again for this entity."""


@dataclass(frozen=True)
class RecentUncertainOutcome:
    """An unresolved (timeout/transport-error) attempt still inside its
    suppression window — see :func:`is_uncertain_outcome`."""

    since: datetime
    """When the uncertain attempt was recorded."""
    until: datetime
    """When this provider becomes automatically-retryable again for this
    entity. Does not mean "safe" — it means "the worst-case exposure window
    this configuration accepts has passed"; a human/operator can always
    investigate and retry sooner."""


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

    @property
    def uncertain_outcome_ttl_seconds(self) -> float:
        return self._config.uncertain_outcome_ttl_seconds

    def recent_uncertain_outcome(
        self,
        provider_name: str,
        entity_type: EntityType,
        entity_id: UUID,
        *,
        organization_id: UUID,
    ) -> RecentUncertainOutcome | None:
        """``None`` if this provider+entity is safe to attempt now —
        including the very first attempt, and the first attempt after the
        window has expired.

        Productization M5 Part 7 (retry safety). Same tenant-isolation
        requirement as :meth:`recent_miss`, for the identical reason —
        `organization_id` is required, not optional.
        """
        window = self._config.uncertain_outcome_ttl_seconds
        if window <= 0:
            return None
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_LAST_UNCERTAIN_OUTCOME,
                {
                    "organization_id": organization_id,
                    "provider": provider_name,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "window_seconds": window,
                },
            )
            row = cur.fetchone()
        assert row is not None  # aggregate query always returns one row
        since: datetime | None = row["last_uncertain"]
        if since is None:
            return None
        return RecentUncertainOutcome(since=since, until=since + timedelta(seconds=window))

    def recent_miss(
        self,
        provider_name: str,
        entity_type: EntityType,
        entity_id: UUID,
        *,
        organization_id: UUID,
    ) -> RecentMiss | None:
        """``None`` if this provider is askable now — including the very
        first ask, and the first ask after a suppression window has expired.

        `organization_id` is required — Productization M1 made `provider_calls`
        tenant-owned and made `evidence`/provider-outcome reuse non-shared even
        for `company`-entity rows (see
        `migrations/0012_organizations_and_members.sql`'s tenancy-boundary
        note). Without this filter, one organization's MISS for a shared
        `company_id` would silently suppress a different organization's
        legitimate, unrelated request for the same company — exactly the
        automatic cross-tenant cache reuse the boundary correction forbids.
        """
        window = self._config.miss_ttl_seconds
        if window <= 0:
            return None
        with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                _SELECT_LAST_MISS,
                {
                    "organization_id": organization_id,
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
