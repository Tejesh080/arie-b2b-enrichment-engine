"""The dataset-validity gate. Wired into CI; a failure here blocks the build.

A synthetic benchmark is only worth running if it is hard enough to distinguish
strategies. These tests assert that property directly rather than trusting the
generator's parameters, so the dataset must *earn* the right to be benchmarked
against every time it is regenerated.

The framing to keep in mind: this file is trying to reject the dataset. If it
cannot, the dataset is fit to use.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from sklearn.metrics import f1_score

from arie.evalgen.schema import EvalLead
from arie.providers.catalog import ALL_PROVIDERS, CHEAP_TIER
from arie.scoring.merge import merge_observations, merged_cost
from arie.scoring.rules import decide, score_facts

# Pre-registered in docs/04-eval-dataset.md before any result was produced.
MIN_F1_GAP = 0.15
MIN_FULL_INFO_F1 = 0.70
MAX_FULL_INFO_F1 = 0.99
MIN_COST_RATIO = 10.0


def _decisions(leads: Sequence[EvalLead], providers: Iterable[str] | None) -> list[str]:
    return [
        str(decide(score_facts(merge_observations(lead.observations, providers)).total_score))
        for lead in leads
    ]


def _oracle(leads: Sequence[EvalLead]) -> list[str]:
    return [str(lead.oracle_decision) for lead in leads]


def _macro_f1(leads: Sequence[EvalLead], providers: Iterable[str] | None) -> float:
    return float(
        f1_score(_oracle(leads), _decisions(leads, providers), average="macro", zero_division=0)
    )


def _agreement(leads: Sequence[EvalLead], providers: Iterable[str] | None) -> float:
    predicted = _decisions(leads, providers)
    truth = _oracle(leads)
    return sum(a == b for a, b in zip(truth, predicted, strict=True)) / len(truth)


def test_cheap_baseline_is_materially_below_full_information(test_split: list[EvalLead]) -> None:
    """THE GATE.

    If cheap evidence alone already matches what full enrichment achieves, then
    no acquisition policy can distinguish itself and the benchmark proves
    nothing. The dataset must be regenerated rather than reported.
    """
    cheap = _macro_f1(test_split, CHEAP_TIER)
    full = _macro_f1(test_split, ALL_PROVIDERS)
    gap = full - cheap

    assert gap >= MIN_F1_GAP, (
        f"Dataset is too easy: cheap-tier macro-F1={cheap:.3f} is only {gap:.3f} "
        f"below the full-information ceiling {full:.3f} (need >= {MIN_F1_GAP}). "
        "Regenerate with harder parameters — do not report benchmark results "
        "against this dataset."
    )


def test_full_information_ceiling_is_attainable(test_split: list[EvalLead]) -> None:
    """The opposite failure: so much observation noise that nothing works.

    If even complete enrichment cannot recover the oracle, the benchmark is
    measuring noise rather than acquisition strategy, and differences between
    policies would be meaningless.
    """
    full = _macro_f1(test_split, ALL_PROVIDERS)
    assert full >= MIN_FULL_INFO_F1, (
        f"Full-information macro-F1={full:.3f} is below {MIN_FULL_INFO_F1}. "
        "Provider noise dominates signal; no policy could succeed here."
    )


def test_full_information_is_imperfect(test_split: list[EvalLead]) -> None:
    """Buying everything must not be a guaranteed win.

    A noiseless ceiling would mean uncertainty never survives enrichment, so a
    calibrated confidence model and a human-escalation path would have nothing
    to do — and 'stop early' could only ever lose.
    """
    full = _macro_f1(test_split, ALL_PROVIDERS)
    assert full <= MAX_FULL_INFO_F1, (
        f"Full-information macro-F1={full:.3f} is effectively perfect. "
        "Observations carry no residual uncertainty, so confidence modelling is moot."
    )


def test_cost_spread_is_material(test_split: list[EvalLead]) -> None:
    """There must be real money on the table between cheap and full."""
    cheap_cost = sum(merged_cost(x.observations, CHEAP_TIER) for x in test_split)
    full_cost = sum(merged_cost(x.observations, ALL_PROVIDERS) for x in test_split)
    ratio = full_cost / max(cheap_cost, 1e-9)
    assert ratio >= MIN_COST_RATIO, (
        f"Full enrichment costs only {ratio:.1f}x the cheap tier; "
        "there is insufficient cost spread for an acquisition policy to exploit."
    )


def test_difficulty_bands_are_ordered_by_cheap_resolvability(
    test_split: list[EvalLead],
) -> None:
    """Bands must mean what they claim.

    'Easy' has to be genuinely more resolvable from cheap evidence than 'hard',
    otherwise the labels are decoration and any per-band analysis of *where*
    savings come from would be misleading.
    """
    by_band = {
        band: [lead for lead in test_split if lead.difficulty_band == band]
        for band in ("easy", "medium", "hard")
    }
    agreement = {band: _agreement(rows, CHEAP_TIER) for band, rows in by_band.items() if rows}

    assert agreement["easy"] > agreement["hard"], (
        f"Cheap-evidence agreement is not ordered by difficulty: {agreement}. "
        "The difficulty bands do not correspond to actual resolvability."
    )


def test_all_decision_classes_present_in_both_splits(
    calibration_split: list[EvalLead], test_split: list[EvalLead]
) -> None:
    """Calibration cannot fit a threshold for a class it has never seen."""
    for name, rows in (("calibration", calibration_split), ("test", test_split)):
        classes = {str(lead.oracle_decision) for lead in rows}
        assert classes == {"auto_route", "escalate_human", "reject"}, (
            f"{name} split is missing decision classes: {sorted(classes)}"
        )


def test_escalation_floor_exists(test_split: list[EvalLead]) -> None:
    """Some leads must remain genuinely borderline under full information.

    This is the honest floor for human-escalation rate. A dataset where perfect
    information always yields a clear answer would let us claim an escalation
    rate near zero — which would be an artefact of generation, not a result.
    """
    borderline = [x for x in test_split if str(x.oracle_decision) == "escalate_human"]
    share = len(borderline) / len(test_split)
    assert 0.02 <= share <= 0.30, (
        f"Irreducibly-borderline share is {share:.3f}; expected a modest but "
        "non-trivial band. Outside this range the escalation path is either "
        "vestigial or dominant."
    )
