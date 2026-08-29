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
    "APPROXIMATE_AGREEMENT",
    "CONFLICT",
    "PARTIAL",
    "UNKNOWN_AGREEMENT",
    "classify_agreement",
    "classify_field",
    "classify_numeric_agreement",
    "overall_agreement",
]

AGREE = "agree"
"""Every provider that answered gave the same canonical value (two or more
answers required — one voice cannot agree with itself). For a numeric
comparison (:func:`classify_numeric_agreement`), "same" means within a tight
5% band rather than bit-identical — vendors round headcounts differently
without disagreeing about the company."""

APPROXIMATE_AGREEMENT = "approximate_agreement"
"""Numeric comparison only. Two usable values whose relative gap exceeds the
tight AGREE band but stays within the wider tolerance (25% by default, the
same threshold ``scripts/provider_bakeoff.py``'s company-overlap summary
already used before this classifier existed) — close enough that the two
vendors are plausibly describing the same company at different precision,
not usefully described as either agreement or conflict."""

PARTIAL = "partial"
"""Exactly one provider produced a usable canonical value; the rest missed or
were unmappable. Coverage, not corroboration."""

CONFLICT = "conflict"
"""Two providers produced *different* canonical values for the same field of
the same person (or, for a numeric comparison, values whose relative gap
exceeds the approximate-agreement tolerance). The interesting case: it either
means the vendors disagree about reality or a taxonomy mapping is wrong, and
both readings demand a human."""

UNKNOWN_AGREEMENT = "unknown"
"""No provider produced a usable value."""

# Severity order for the roll-up: a single conflicted field makes the whole
# comparison a conflict, full mutual agreement outranks partial coverage, and
# "nobody knew anything" only wins when it is the entire story.
_SEVERITY = (CONFLICT, AGREE, PARTIAL, UNKNOWN_AGREEMENT)

_AGREE_TOLERANCE = 0.05
_APPROXIMATE_AGREEMENT_TOLERANCE = 0.25


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


def classify_numeric_agreement(
    values: Mapping[str, int | float | None],
    *,
    agree_tolerance: float = _AGREE_TOLERANCE,
    approximate_tolerance: float = _APPROXIMATE_AGREEMENT_TOLERANCE,
) -> str:
    """Two vendors' numeric answers to the same question — a headcount, not a
    category — so exact equality (:func:`classify_field`'s rule) would call
    almost every real pair a conflict. ``values`` maps provider name → its
    canonical number, or ``None``/absent for a provider with nothing usable.

    * :data:`UNKNOWN_AGREEMENT` — fewer than two usable values: nothing to
      compare.
    * :data:`AGREE` — the relative gap between the smallest and largest usable
      value is at most ``agree_tolerance`` (5% by default) of the larger —
      vendors rounding the same headcount differently, not disagreeing.
    * :data:`APPROXIMATE_AGREEMENT` — the gap exceeds that but stays within
      ``approximate_tolerance`` (25% — the threshold already used, unnamed,
      by the bake-off's pre-existing company-overlap summary).
    * :data:`CONFLICT` — beyond ``approximate_tolerance``: the Stripe case
      this classifier was built for (Abstract 3,037 vs. Hunter's
      band-lower-bound 10,000 — a 70% gap).

    A three-or-more-provider comparison uses the *widest* pairwise spread
    (min to max), the conservative reading: if any two disagree by more than
    the tolerance, the group does not agree.
    """
    usable = [
        float(value) for value in values.values() if value is not None and not is_unknown(value)
    ]
    if len(usable) < 2:
        return UNKNOWN_AGREEMENT
    smallest, largest = min(usable), max(usable)
    if largest == 0:
        return AGREE  # every usable value is exactly zero
    relative_gap = (largest - smallest) / largest
    if relative_gap <= agree_tolerance:
        return AGREE
    if relative_gap <= approximate_tolerance:
        return APPROXIMATE_AGREEMENT
    return CONFLICT
