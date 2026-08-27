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
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME

__all__ = [
    "LIVE_PROVIDER_NAMES",
    "REGISTERED_LIVE_PROVIDER_NAMES",
    "acquisition_order",
]

REGISTERED_LIVE_PROVIDER_NAMES: tuple[str, ...] = (
    ABSTRACT_PROVIDER_NAME,
    HUNTER_PROVIDER_NAME,
    APOLLO_PROVIDER_NAME,
)
"""Providers the live handler actually builds and calls, **in default
acquisition order**.

The order is the policy, not an accident of how the tuple was typed, and it is
cheapest-first end to end — the same convention ``arie.providers.catalog``'s
simulated ordering uses:

1. **Abstract (company, ~$0.00165/call)** first. Its evidence is
   company-scoped and therefore shared by every future lead at the same
   employer; the cheapest fact is also the most reusable one.
2. **Hunter (person, ~$0.0049/success)** second — the cheaper of the two
   person providers, and only if company evidence left the decision open.
3. **Apollo (person, ~$0.0196/success)** last: the most expensive lookup runs
   only when the cheaper person provider missed, failed, or covered too little.

All three unit prices are modelled from published plan rates, not vendor-billed
figures — see each config class in ``arie.config`` for its derivation. The
default order is overridable for experiments via ``LIVE_PROVIDER_ORDER``
(:func:`acquisition_order`), because "cheapest first" is a reasoned prior, not
a measured result — the provider bake-off (``scripts/provider_bakeoff.py``)
exists to replace the prior with data before any reordering is made default.

This is still **not** a marketplace or an EVoI optimiser over live providers.
Three providers in a configurable deterministic order is enough to measure
which order is right; learning the order is a later, separately-validated step.
"""

LIVE_PROVIDER_NAMES: tuple[str, ...] = (
    ABSTRACT_PROVIDER_NAME,
    HUNTER_PROVIDER_NAME,
    APOLLO_PROVIDER_NAME,
)
"""Every provider name that would bill a real account, wired or not.

Currently identical to :data:`REGISTERED_LIVE_PROVIDER_NAMES`. They are still
separate names, and should stay separate: the budget guard must cover the
superset (anything that *could* bill), while the handler and the receipt's
"providers not called" must reflect the subset actually built. Apollo spent a
phase in this tuple alone — contract defined, adapter not yet wired — so its
spend was capped from its first real call; the next contract that lands ahead
of its adapter goes in this tuple first, same as Apollo did.
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
