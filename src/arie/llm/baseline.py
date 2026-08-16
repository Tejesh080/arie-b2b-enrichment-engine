"""The deterministic baseline `arie.llm.deepseek` has to beat.

Every experiment in this project follows the same shape: build the
sophisticated version, build the simplest thing that could plausibly work, and
report which one actually wins (see ``docs/adr/0004-evoi-is-a-negative-result.md``
— EVoI lost this exact comparison against ``CalibratedBoundsPolicy``). This is
that comparison for LLM signal extraction: substring matching against a fixed
phrase list, the thing a team would reach for before paying for an API call.

Zero cost, zero latency, zero network — which is also why it is a fair
baseline and not a strawman. A team already has this option for free; the LLM
has to earn its place against it, not against nothing.
"""

from __future__ import annotations

import re

from arie.llm.schema import ExtractedSignal, TriggerCategory

# Case-insensitive phrase lists. Substrings, not whole-word matches — "poc" as
# a whole word would miss "run a poc for us"; matching loosely and accepting
# the occasional false positive is the realistic failure mode of a rule list
# like this, and part of what the LLM comparison is meant to measure.
_BUYING_INTENT_PHRASES: tuple[str, ...] = (
    "ready to buy",
    "ready to purchase",
    "evaluating vendors",
    "evaluating options",
    "budget approved",
    "budget has been approved",
    "looking to purchase",
    "want to get started",
    "book a demo",
    "request a demo",
    "interested in pricing",
    "send over pricing",
    "compare pricing",
    "procurement",
    "purchase order",
    "run a poc",
    "pilot program",
    "want a quote",
    "sign up today",
)

_DISQUALIFYING_PHRASES: tuple[str, ...] = (
    "not interested",
    "no budget",
    "not looking to buy",
    "just researching",
    "just browsing",
    "unsubscribe",
    "wrong department",
    "already have a solution",
    "already use a competitor",
    "not the right fit",
    "no plans to purchase",
    "please remove me",
)

_TRIGGER_PHRASES: tuple[tuple[TriggerCategory, str], ...] = (
    ("funding_event", "series a"),
    ("funding_event", "series b"),
    ("funding_event", "series c"),
    ("funding_event", "raised a round"),
    ("funding_event", "raised funding"),
    ("funding_event", "closed a round"),
    ("funding_event", "seed round"),
    ("leadership_change", "new cto"),
    ("leadership_change", "new ceo"),
    ("leadership_change", "new vp"),
    ("leadership_change", "hired a vp"),
    ("leadership_change", "joined as"),
    ("leadership_change", "promoted to"),
    ("expansion", "opened a new office"),
    ("expansion", "opening our second"),
    ("expansion", "expanding to"),
    ("expansion", "international expansion"),
    ("expansion", "new market"),
    ("product_or_technology_change", "migrating to"),
    ("product_or_technology_change", "switching from"),
    ("product_or_technology_change", "replacing our"),
    ("product_or_technology_change", "upgrading our stack"),
    ("product_or_technology_change", "new platform"),
)


def _find_phrase(text_lower: str, phrases: tuple[str, ...]) -> str | None:
    return next((phrase for phrase in phrases if phrase in text_lower), None)


def extract_signal_deterministic(text: str) -> ExtractedSignal:
    """Substring-match `text` against fixed phrase lists. Pure, no I/O.

    Confidence is always 1.0: not a claim about ground truth, only that the
    rule fired unambiguously (or, for a clean miss, that no rule fired). A
    keyword-matcher has no basis for reporting anything else.
    """
    text_lower = re.sub(r"\s+", " ", text.lower())

    buying_hit = _find_phrase(text_lower, _BUYING_INTENT_PHRASES)
    disqualifying_hit = _find_phrase(text_lower, _DISQUALIFYING_PHRASES)
    trigger_hit = next(
        ((category, phrase) for category, phrase in _TRIGGER_PHRASES if phrase in text_lower),
        None,
    )

    matched = [
        part
        for part in (
            f"buying-intent phrase {buying_hit!r}" if buying_hit else None,
            f"disqualifying phrase {disqualifying_hit!r}" if disqualifying_hit else None,
            f"trigger phrase {trigger_hit[1]!r}" if trigger_hit else None,
        )
        if part is not None
    ]
    rationale = f"matched: {', '.join(matched)}" if matched else "no phrase from the list matched"

    return ExtractedSignal(
        has_buying_intent=buying_hit is not None,
        trigger_event_category=trigger_hit[0] if trigger_hit else None,
        trigger_event_detail=trigger_hit[1] if trigger_hit else None,
        disqualifying_signal=disqualifying_hit is not None,
        confidence=1.0,
        rationale=rationale[:280],
    )
