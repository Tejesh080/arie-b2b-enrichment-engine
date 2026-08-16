"""The extraction contract — what the LLM is allowed to say, and nothing else.

``ExtractedSignal`` is the entire interface between free text and the rest of
the system. It has no field for "which provider to call next", no field for a
lead's new status, no field that could be interpreted as a tool invocation —
by construction, not by convention. A model that wanted to "act" would have
nowhere to put the instruction; the schema only has room for what was observed
in the text.

``extra="forbid"`` matters as much as the declared fields do: without it, a
model free-associating an extra key would validate silently, and "strict
structured output" would be a description of the happy path rather than a
guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# A small, closed vocabulary — independent of `arie.evalgen.generator`'s
# `_TRIGGER_EVENTS` on purpose. That list is a private detail of the M0 dataset
# generator; free text from a real lead was never going to match it verbatim,
# and importing it would couple this module to M0 internals for a coincidental
# naming overlap rather than a real dependency. "other" exists because forcing
# every real trigger into five categories designed for a synthetic benchmark
# would make the model choose between lying and refusing.
TriggerCategory = Literal[
    "funding_event",
    "leadership_change",
    "expansion",
    "product_or_technology_change",
    "other",
]

TRIGGER_CATEGORIES: tuple[TriggerCategory, ...] = (
    "funding_event",
    "leadership_change",
    "expansion",
    "product_or_technology_change",
    "other",
)


class ExtractedSignal(BaseModel):
    """One extraction result — from the LLM, or from the deterministic baseline.

    Both `arie.llm.deepseek` and `arie.llm.baseline` return exactly this type,
    which is what makes "delta against the frozen deterministic baseline" a
    same-shape comparison rather than an apples-to-oranges one.
    """

    model_config = ConfigDict(extra="forbid")

    has_buying_intent: bool = Field(
        description="True only if the text itself states or clearly implies "
        "active interest in buying/evaluating — not merely mentioning the "
        "product category."
    )
    trigger_event_category: TriggerCategory | None = Field(
        default=None,
        description="The best-fitting category for a trigger event explicitly "
        "described in the text, or null if none is described.",
    )
    trigger_event_detail: str | None = Field(
        default=None,
        max_length=200,
        description="A short paraphrase (not a verbatim quote) of the trigger "
        "event, or null if trigger_event_category is null.",
    )
    disqualifying_signal: bool = Field(
        description="True only if the text states a concrete reason this lead "
        "cannot buy (e.g. no budget, wrong company size, explicitly not "
        "interested) — not merely the absence of enthusiasm."
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Your own confidence that the fields above are correctly "
        "read from the text, not a probability of the lead buying.",
    )
    rationale: str = Field(
        max_length=280,
        description="One short sentence citing what in the text supports the "
        "fields above. Not shown to the lead or used to make any decision.",
    )


@dataclass(frozen=True)
class ExpectedLabel:
    """Ground truth for one eval sample — deliberately smaller than
    `ExtractedSignal`. Confidence and rationale are properties of an answer,
    not of a fact; a label has no need for either."""

    has_buying_intent: bool
    trigger_event_category: TriggerCategory | None
    disqualifying_signal: bool


@dataclass(frozen=True)
class LabeledSample:
    """One row of the small hand-labeled eval corpus (`arie.llm.eval`)."""

    sample_id: str
    text: str
    expected: ExpectedLabel
