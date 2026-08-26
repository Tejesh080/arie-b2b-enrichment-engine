"""Which provider names spend real money.

Exists so three unrelated callers cannot disagree about it:

* ``arie.live.budget`` filters the daily spend query by these names. Without
  the filter, the simulated catalogue's (fictional) per-call prices would
  count against the live budget and a benchmark run would exhaust it.
* ``arie.api.receipt`` picks which catalogue "providers not called" is a set
  difference against.
* ``arie.jobs.handlers``' live builder registers exactly these.

A provider added to ARIE that costs money and is missing from this tuple is
invisible to the spend caps — the failure mode this module is here to make
impossible to reach by accident.
"""

from __future__ import annotations

from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME

__all__ = ["LIVE_PROVIDER_NAMES", "REGISTERED_LIVE_PROVIDER_NAMES"]

REGISTERED_LIVE_PROVIDER_NAMES: tuple[str, ...] = (ABSTRACT_PROVIDER_NAME,)
"""Providers the live handler actually builds and calls today. Exactly one."""

LIVE_PROVIDER_NAMES: tuple[str, ...] = (ABSTRACT_PROVIDER_NAME, APOLLO_PROVIDER_NAME)
"""Every provider name that would bill a real account, including ones defined
but not yet wired.

Apollo is here while ``arie.providers.apollo_contract`` is a fixture-only
contract with no HTTP client, no key, and no registration. Including it costs
nothing (no rows exist under that name, so the budget query sums zero) and
means the day it is wired, its spend is capped by construction rather than by
someone remembering to add it here. The budget guard must never be the thing
that gets updated *after* a paid provider goes live.
"""
