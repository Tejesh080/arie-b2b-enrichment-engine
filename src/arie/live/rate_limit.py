"""Client-side pacing for one adapter instance — never a global limiter.

**The failure this prevents.** The 2026-08-29 abstract-hunter-live-1
experiment fired five sequential Abstract calls with no pacing between them
and hit a real ``rate_limited`` (429) on the fifth — Abstract publishes only a
monthly volume cap (100 requests/month, free tier), never a documented
requests-per-second figure, so the burst limit that actually fired is
undocumented and this pacer's default interval is a conservative estimate,
not a number taken from Abstract's docs (there is nothing there to take).

**Provider-specific, not global, by construction.** A :class:`MinIntervalPacer`
belongs to one adapter *instance* — ``AbstractCompanyEnrichmentProvider``
holds its own, Hunter holds none. A single ``time.sleep`` bolted onto the
shared acquisition loop would pace every provider at whichever vendor is
slowest, which is exactly the mistake this module exists to avoid: Hunter's
own (documented, generous — 15 req/s) limit has nothing to do with Abstract's.

**Bounded concurrency, for free.** Because ARIE's acquisition loop only ever
calls the company provider once per lead, sequentially (see
``arie.jobs.handlers``), pacing one provider instance across leads already
gives it an effective concurrency of 1 against itself — there is no separate
semaphore to add for a provider a lead never calls twice.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

__all__ = ["MinIntervalPacer"]


@dataclass
class MinIntervalPacer:
    """Blocks the calling thread until this pacer's next call is allowed.

    Two independent floors, and a wait honours whichever is later:

    * **The pace** — at least ``min_interval_seconds`` since this pacer's own
      last call. ``0`` disables pacing entirely (every call proceeds
      immediately), the safe default for a provider with no known burst
      limit.
    * **A live Retry-After** — set by :meth:`note_retry_after` after a 429/503
      response that named one; cleared automatically once it passes. A vendor
      that says "wait 12 seconds" is more informative than a guessed pace,
      and this pacer prefers it without discarding the ordinary interval.

    ``sleep``/``monotonic`` are injectable so a test can assert exact wait
    durations without a real sleep — every test in
    ``tests/unit/test_rate_limit.py`` runs in well under a second.
    """

    min_interval_seconds: float
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    monotonic: Callable[[], float] = field(default=time.monotonic, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, init=False)
    _next_call_no_earlier_than: float | None = field(default=None, repr=False, init=False)
    _retry_after_until: float | None = field(default=None, repr=False, init=False)

    def wait(self) -> None:
        """Block (if needed) until this pacer's next call is allowed."""
        with self._lock:
            now = self.monotonic()
            deadline = now
            if self._next_call_no_earlier_than is not None:
                deadline = max(deadline, self._next_call_no_earlier_than)
            if self._retry_after_until is not None:
                deadline = max(deadline, self._retry_after_until)
            delay = deadline - now
            self._next_call_no_earlier_than = deadline + self.min_interval_seconds
        if delay > 0:
            self.sleep(delay)

    def note_retry_after(self, seconds: float) -> None:
        """Record a vendor-supplied ``Retry-After``: the next :meth:`wait`
        will not return before this deadline, on top of the ordinary pace.
        Never shortens an already-later deadline from a prior call."""
        with self._lock:
            candidate = self.monotonic() + max(0.0, seconds)
            self._retry_after_until = max(self._retry_after_until or 0.0, candidate)
