"""Cross-provider agreement — what two vendors' answers to one question mean.

Pure functions, no I/O: the evaluation-parallel handler and the bake-off
harness both classify with these, which is what keeps "conflict" meaning the
same thing in a Decision Receipt and in a comparison report.

**The classification is over canonical values, with raw alongside.** Two
vendors writing "VP Sales" and "Vice President of Sales" agree — the whole
point of the canonical layer is that surface variation is not disagreement.
Raw strings still travel in the record, because a *canonical* conflict has two
possible causes (the vendors genuinely disagree about the person, or one
mapping is wrong) and only the raw pair lets a human tell which.

**Neither vendor wins a conflict, by design.** No rule here (or anywhere)
prefers Apollo over Hunter or vice versa: the adapters declare equal source
confidence, so a conflicting pair reaches the evidence merge layer as a
genuine contest — ``FieldResolution.contested`` goes true, the conflict
signals depress the calibrated confidence, and the lead lands in front of a
human with both provenance rows intact. A meaningful conflict *increases*
uncertainty; it does not silently pick a vendor. Choosing a winner is a
decision for measured agreement data (the bake-off), not for a default.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from arie.scoring.rules import is_unknown

__all__ = [
    "AGREE",
    "CONFLICT",
    "PARTIAL",
    "UNKNOWN_AGREEMENT",
    "classify_agreement",
    "classify_field",
    "overall_agreement",
]

AGREE = "agree"
"""Every provider that answered gave the same canonical value (two or more
answers required — one voice cannot agree with itself)."""

PARTIAL = "partial"
"""Exactly one provider produced a usable canonical value; the rest missed or
were unmappable. Coverage, not corroboration."""

CONFLICT = "conflict"
"""Two providers produced *different* canonical values for the same field of
the same person. The interesting case: it either means the vendors disagree
about reality or a taxonomy mapping is wrong, and both readings demand a
human."""

UNKNOWN_AGREEMENT = "unknown"
"""No provider produced a usable value."""

# Severity order for the roll-up: a single conflicted field makes the whole
# comparison a conflict, full mutual agreement outranks partial coverage, and
# "nobody knew anything" only wins when it is the entire story.
_SEVERITY = (CONFLICT, AGREE, PARTIAL, UNKNOWN_AGREEMENT)


def classify_field(values: Mapping[str, Any]) -> str:
    """Classify one field's per-provider canonical values.

    ``values`` maps provider name → the canonical value that provider yielded,
    with ``None`` (or the UNKNOWN sentinel) for a provider that answered
    nothing usable. Providers that were never called for this field simply
    should not appear — absence of a call is not an opinion.
    """
    usable = [value for value in values.values() if not is_unknown(value)]
    if not usable:
        return UNKNOWN_AGREEMENT
    if len(usable) == 1:
        return PARTIAL
    if len({str(value) for value in usable}) == 1:
        return AGREE
    return CONFLICT


def classify_agreement(
    per_provider_fields: Mapping[str, Mapping[str, Any]],
    field_names: Sequence[str],
) -> dict[str, str]:
    """Per-field classification across providers.

    ``per_provider_fields`` maps provider name → that provider's canonical
    fields dict (exactly ``ProviderResult.fields`` shape). Only providers
    present in the mapping are compared — pass the ones that were actually
    called.
    """
    return {
        field_name: classify_field(
            {provider: fields.get(field_name) for provider, fields in per_provider_fields.items()}
        )
        for field_name in field_names
    }


def overall_agreement(field_classifications: Mapping[str, str]) -> str:
    """The single-word roll-up: worst-first by :data:`_SEVERITY`."""
    if not field_classifications:
        return UNKNOWN_AGREEMENT
    present = set(field_classifications.values())
    for label in _SEVERITY:
        if label in present:
            return label
    return UNKNOWN_AGREEMENT
