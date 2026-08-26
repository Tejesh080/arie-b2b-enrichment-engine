"""The provider→scorer adapter boundary (Live V1 Foundation, Phase 5).

One function, :func:`normalize_provider_fields`, and one result type,
:class:`NormalizationReport`. Every live provider adapter passes its raw
response dict through here and returns the report's ``fields``; nothing else is
allowed to construct evidence from a vendor payload.

**What the boundary guarantees**

* ``fields`` contains only canonical values — members of
  ``arie.normalization.taxonomy``'s closed sets, or validated numerics. Raw
  vendor vocabulary cannot pass through it.
* A value that could not be mapped is **absent from** ``fields`` and **present
  in** ``unmapped``. It is never emitted as a canonical value, and never
  emitted as the ``UNKNOWN`` sentinel either — see "why unknowns are dropped"
  below.
* An unknown *field name* (a vendor sending something ARIE does not score) is
  dropped into ``ignored_fields`` rather than passed along. The scorer already
  ignores unscored keys, but silently forwarding them would let a future
  refactor turn a typo into a scored field.

**Why unknowns are dropped from ``fields`` rather than stored**

An ``UNKNOWN``-valued evidence row would be indistinguishable, to the
acquisition loop's "do I already have this field?" check, from a real one — the
loop would believe the field was covered and never re-ask. The existing
provider-MISS path already writes no evidence for exactly this reason, and an
unmappable value is the same situation: the provider answered, and we have
nothing usable. Dropping keeps one rule instead of two.

The information is not lost. ``unmapped`` carries the raw string, the adapter
puts it on its span and in ``ProviderResult.raw``, and the deliberate cost is
stated rather than hidden: the next lead at the same company re-asks the same
provider and gets the same unusable answer. That re-ask is bounded by
``arie.live.budget``'s caps, and the fix is a line in
``arie.normalization.taxonomy``'s alias table — which is precisely the feedback
loop ``unmapped`` exists to feed.

**The scorer defends itself anyway.** ``arie.scoring.rules.is_unknown``
understands the sentinel even though this boundary never emits it, so an
evidence row written by an older build, a fixture, or a future adapter that
chooses to persist unknowns is still scored as *unknown* rather than as a
confident zero.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from arie.core.types import EntityType
from arie.normalization.taxonomy import (
    is_unknown,
    normalize_employee_count,
    normalize_function,
    normalize_industry,
    normalize_seniority,
)

__all__ = [
    "COMPANY_NORMALIZERS",
    "PERSON_NORMALIZERS",
    "MappedValue",
    "NormalizationReport",
    "UnmappedValue",
    "normalize_provider_fields",
]

# Field name -> the canonical normalizer that owns it. This mapping *is* the
# contract: a field with no entry here cannot cross the boundary, which is what
# makes "no raw provider vocabulary reaches the scorer" checkable rather than
# aspirational.
#
# Deliberately absent, and staying absent for Live V1: `buying_intent`,
# `recent_trigger_event`, and `disqualifying_flag`. No live provider ARIE has
# access to supplies trustworthy evidence for any of them, and a normalizer
# here would be an invitation to invent one. They stay unknown, which keeps the
# score bounds honestly wide — see `arie.scoring.engine.compute_bounds`, where
# an unchecked disqualifier pins the score floor at zero.
COMPANY_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "employee_count": normalize_employee_count,
    "industry": normalize_industry,
}

PERSON_NORMALIZERS: dict[str, Callable[[Any], Any]] = {
    "title_seniority": normalize_seniority,
    "title_function": normalize_function,
}

_NORMALIZERS_BY_ENTITY: dict[EntityType, dict[str, Callable[[Any], Any]]] = {
    "company": COMPANY_NORMALIZERS,
    "person": PERSON_NORMALIZERS,
}


@dataclass(frozen=True)
class UnmappedValue:
    """One field a provider answered for, in vocabulary ARIE could not map.

    Carries the raw value verbatim so the operator-facing trail can say *what*
    the vendor said. This is the input to extending
    ``arie.normalization.taxonomy``'s alias tables — an unmapped value seen
    twice is a missing table row, not a mystery.
    """

    field_name: str
    raw: str
    """``str()`` of the provider's value, truncated. Never the whole payload:
    this ends up on spans and in ``ProviderResult.raw``, which are logged."""


@dataclass(frozen=True)
class MappedValue:
    """One field that mapped, recorded as the *pair* rather than the result.

    The canonical value alone is not enough to audit a mapping. "This lead's
    industry is `software`" does not say whether the vendor wrote ``"Computer
    Software"``, ``"SaaS"``, or something a rule matched by accident — and the
    whole risk this layer manages is a mapping being wrong in a way that looks
    perfectly reasonable downstream. Keeping the pair means a live call is
    self-documenting: what was said, and what ARIE decided it meant.
    """

    field_name: str
    raw: str
    canonical: Any


_RAW_PREVIEW_CHARS = 120


@dataclass(frozen=True)
class NormalizationReport:
    """The complete outcome of normalizing one provider response."""

    provider: str
    entity_type: EntityType

    fields: dict[str, Any] = field(default_factory=dict)
    """Canonical, usable evidence — the only thing that may reach the evidence
    store and the scorer."""

    mapped: tuple[MappedValue, ...] = ()
    """The raw→canonical pair behind every entry in ``fields``. Provenance
    only; nothing reads it to make a decision."""

    unmapped: tuple[UnmappedValue, ...] = ()
    """Fields the provider answered for in vocabulary that did not map. Not in
    ``fields``; see the module docstring for why."""

    ignored_fields: tuple[str, ...] = ()
    """Keys in the raw payload this boundary has no normalizer for. Expected and
    ordinary — vendors return far more than ARIE scores."""

    @property
    def has_usable_fields(self) -> bool:
        return bool(self.fields)

    def audit(self) -> dict[str, Any]:
        """The compact, log-safe summary an adapter attaches to its result.

        Only field names and unmapped raw values — never the full vendor
        payload, which may carry PII the evidence store deliberately does not
        hold, and never anything derived from credentials.
        """
        return {
            "mapped": [
                {"field": item.field_name, "raw": item.raw, "canonical": item.canonical}
                for item in sorted(self.mapped, key=lambda item: item.field_name)
            ],
            "unmapped": [
                {"field": item.field_name, "raw": item.raw}
                for item in sorted(self.unmapped, key=lambda item: item.field_name)
            ],
        }


def normalize_provider_fields(
    *,
    provider: str,
    entity_type: EntityType,
    raw_fields: Mapping[str, Any],
) -> NormalizationReport:
    """Normalize one provider's raw response into canonical evidence.

    Pure: no I/O, no config, no clock. A provider adapter's whole normalization
    responsibility is picking which of its response keys map onto ARIE field
    names and calling this — the vocabulary decisions all live one layer down,
    in ``arie.normalization.taxonomy``, shared across every provider.
    """
    normalizers = _NORMALIZERS_BY_ENTITY[entity_type]

    fields: dict[str, Any] = {}
    mapped: list[MappedValue] = []
    unmapped: list[UnmappedValue] = []
    ignored: list[str] = []

    for field_name in sorted(raw_fields):
        normalizer = normalizers.get(field_name)
        if normalizer is None:
            ignored.append(field_name)
            continue

        raw_value = raw_fields[field_name]
        if raw_value is None or (isinstance(raw_value, str) and not raw_value.strip()):
            # Nothing was said. Not an unmapped vocabulary problem — just
            # absence, which needs no audit entry.
            continue

        value = normalizer(raw_value)
        if is_unknown(value):
            unmapped.append(
                UnmappedValue(field_name=field_name, raw=str(raw_value)[:_RAW_PREVIEW_CHARS])
            )
            continue

        fields[field_name] = value
        mapped.append(
            MappedValue(
                field_name=field_name,
                raw=str(raw_value)[:_RAW_PREVIEW_CHARS],
                canonical=value,
            )
        )

    return NormalizationReport(
        provider=provider,
        entity_type=entity_type,
        fields=fields,
        mapped=tuple(mapped),
        unmapped=tuple(unmapped),
        ignored_fields=tuple(ignored),
    )
