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

from collections.abc import Iterable

from arie.providers.apollo_contract import APOLLO_PROVIDER_NAME
from arie.providers.base import EnrichmentProvider
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME

__all__ = [
    "LIVE_PROVIDER_NAMES",
    "REGISTERED_LIVE_PROVIDER_NAMES",
    "acquisition_order",
]

REGISTERED_LIVE_PROVIDER_NAMES: tuple[str, ...] = (ABSTRACT_PROVIDER_NAME, APOLLO_PROVIDER_NAME)
"""Providers the live handler actually builds and calls, **in acquisition order**.

The order is the policy, not an accident of how the tuple was typed, and it is
deliberately fixed rather than optimised:

1. **Abstract (company)** first. It is an order of magnitude cheaper
   ($0.00165 vs $0.0196 — see ``arie.config.ApolloPersonConfig``), its evidence
   is company-scoped and therefore shared by every future lead at the same
   employer, and cheapest-first is the same convention
   ``arie.providers.catalog``'s simulated ordering already uses.
2. **Apollo (person)** second, and only if company evidence left the decision
   genuinely open. Person evidence is per-person by construction, so it can
   never be amortised across a company the way firmographics can — buying it
   before finding out whether it is needed is the expensive mistake.

This is **not** a marketplace or an EVoI optimiser over live providers, and
turning it into one is a separate step with its own validation. Two providers
in a fixed order is enough to make the interesting question — *was the second
call necessary?* — a real one that ``arie.jobs.handlers``' live loop answers
per lead, which a single-provider pipeline could not.
"""

LIVE_PROVIDER_NAMES: tuple[str, ...] = (ABSTRACT_PROVIDER_NAME, APOLLO_PROVIDER_NAME)
"""Every provider name that would bill a real account, wired or not.

Currently identical to :data:`REGISTERED_LIVE_PROVIDER_NAMES` — Apollo used to
sit here alone, as a contract with no client and no key, precisely so its spend
would be capped from the moment it was wired rather than from the moment
someone remembered to add it. It is now wired, and the two tuples agree again.

They are still separate names, and should stay separate: the budget guard must
cover the superset (anything that *could* bill), while the handler and the
receipt's "providers not called" must reflect the subset actually built. The
day a third contract lands ahead of its adapter, this is the tuple it goes in
first.
"""


def acquisition_order(providers: Iterable[EnrichmentProvider]) -> tuple[EnrichmentProvider, ...]:
    """Sort ``providers`` into :data:`REGISTERED_LIVE_PROVIDER_NAMES` order.

    Called by ``arie.jobs.handlers``' live builder on whatever set it ends up
    with — the ones it constructed itself, or ones a test or
    ``scripts/live_provider_smoke.py`` injected. Sorting here rather than
    trusting the caller's sequence means acquisition order is a property of the
    system that a test can rely on, not of the argument list a caller happened
    to type; an injected pair in the wrong order would otherwise silently
    exercise a different policy than production runs.

    A provider whose name is not in the tuple sorts last, preserving its
    relative position among other unknowns. That keeps an experimental adapter
    runnable without editing this module, while guaranteeing it can never
    displace a known provider from its reviewed position.
    """
    known = {name: index for index, name in enumerate(REGISTERED_LIVE_PROVIDER_NAMES)}
    ordered = list(providers)
    return tuple(sorted(ordered, key=lambda provider: known.get(provider.name, len(known))))
