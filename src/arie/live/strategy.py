"""The two live acquisition strategies, and the vocabulary that names them.

**Two strategies, one purpose each — do not confuse them.**

``optimized`` is ARIE operating: providers called selectively and
sequentially, cheapest first, stopping the moment the existing evidence/
confidence logic says further evidence is unnecessary for the current
shadow/review recommendation. Calling every provider for every lead would
defeat the system's entire premise, so this is and stays the default.

``evaluation_parallel`` is ARIE *measuring its own suppliers*: a private,
server-side experiment mode where the person providers are deliberately called
concurrently for the same lead, so coverage, quality, latency, credit
behaviour, and cross-provider agreement can be compared on identical inputs.
It intentionally spends more per lead (its own explicit budget —
``LiveBudgetConfig.evaluation_per_lead_usd`` — never a bypass of the guard),
records every call separately, and identifies itself on the Decision Receipt
via ``policy_name`` so an evaluation receipt can never be mistaken for an
operating one. It is the instrument that produces the data for choosing the
default waterfall; it is not a waterfall.

**What neither strategy changes.** Live mode remains non-autonomous under
both — the ``arie.live.safety`` guard is upstream of strategy and reads no
strategy config. The public demo runs ``PROVIDER_MODE=simulated`` and never
reads any of this: ``resolve_strategy`` is called from the *live* handler
builder only, which is also why ``LiveStrategyConfig`` deliberately skips
eager ``__post_init__`` validation — a typo'd strategy value on a simulated
deployment must be inert, not an import-time crash.
"""

from __future__ import annotations

from typing import Literal

from arie.config import LIVE_STRATEGY, LiveStrategyConfig

__all__ = [
    "EVALUATION_PARALLEL",
    "EVALUATION_POLICY_NAME",
    "LEGACY_LIVE_POLICY_NAME",
    "LIVE_POLICY_NAMES",
    "OPTIMIZED",
    "OPTIMIZED_POLICY_NAME",
    "LiveStrategy",
    "UnsupportedLiveStrategyError",
    "resolve_strategy",
]

LiveStrategy = Literal["optimized", "evaluation_parallel"]

OPTIMIZED: LiveStrategy = "optimized"
EVALUATION_PARALLEL: LiveStrategy = "evaluation_parallel"

OPTIMIZED_POLICY_NAME = "live_optimized"
"""``decision_receipts.policy_name`` for an optimized-strategy live lead.
Successor to ``live_single_provider``, which stopped being a true description
the day a second provider was wired; rows written under the old name remain
valid live receipts (see :data:`LIVE_POLICY_NAMES`)."""

EVALUATION_POLICY_NAME = "live_evaluation_parallel"
"""``decision_receipts.policy_name`` for an evaluation-strategy lead — the
receipt-level marker Phase 5 requires: an evaluation run must clearly identify
itself, because its cost profile and its calling pattern are deliberately not
the operating ones."""

LEGACY_LIVE_POLICY_NAME = "live_single_provider"
"""What the live handler wrote while exactly one real provider existed. Stored
data carries it forever; it stays in the recognised set so old receipts keep
resolving against the live catalogue."""

LIVE_POLICY_NAMES: frozenset[str] = frozenset(
    {LEGACY_LIVE_POLICY_NAME, OPTIMIZED_POLICY_NAME, EVALUATION_POLICY_NAME}
)
"""Every ``policy_name`` that means "this receipt was produced by the live
path" — the set ``arie.api.receipt`` matches against to pick the live provider
catalogue and to surface the autonomy-guard reason."""


class UnsupportedLiveStrategyError(RuntimeError):
    """Raised at live-handler build time for a strategy string this module
    does not recognise — the same loud-at-startup treatment a missing API key
    gets, and for the same reason: a misconfigured live worker must refuse to
    start, never quietly fall back to a behaviour nobody chose."""


def resolve_strategy(config: LiveStrategyConfig | None = None) -> LiveStrategy:
    """Validate and return the configured strategy.

    Called from ``arie.jobs.handlers._build_live_handlers`` and nowhere else —
    keeping the call site singular is what makes "the simulated path never
    reads the strategy" a checkable structural fact rather than a habit.
    """
    resolved = (config if config is not None else LIVE_STRATEGY).strategy
    if resolved == OPTIMIZED:
        return OPTIMIZED
    if resolved == EVALUATION_PARALLEL:
        return EVALUATION_PARALLEL
    raise UnsupportedLiveStrategyError(
        f"LIVE_PROVIDER_STRATEGY={resolved!r} is not a recognised strategy — only "
        f"{OPTIMIZED!r} (the operating default) and {EVALUATION_PARALLEL!r} (private "
        "provider experiments) exist. See arie.live.strategy."
    )
