"""A small, hand-labeled free-text corpus, and the delta measurement it exists for.

**Why this is not `arie.evalgen`.** M0's dataset is deliberately structured-only
— `recent_trigger_event` is a five-value closed enum, `buying_intent` a plain
float; there is no free text anywhere in it, on purpose (`docs/benchmark.md`: "No
LLM in M0... introducing nondeterminism into the experiment that establishes
the baseline result is a methodological error"). This module is *M1's own*
small evaluation for *this one narrow task*, and it stays structurally
separate from `arie.evalgen`, `bench/multi_seed.py`, and `data/eval/` — it
imports nothing from them and nothing here can perturb the M0 benchmark's
provenance. See `docs/architecture.md`'s Step 10 section.

**Twenty-six samples is a smoke-test-scale corpus, not a statistically powered
one** — stated plainly, the same way `benchmark.md` states "ten seeds is a
small sample" for the M0 benchmark. A delta this small a sample produces is a
directional signal, not a precise estimate; treat a report of ±1 sample's
worth of accuracy as noise.

**A failed extraction counts as wrong on every field, never excluded.**
Scoring only what an extractor successfully answered would let a policy that
gives up on hard cases look artificially accurate on the cases it kept — the
same trap `v_pipeline_metrics.cost_per_qualified_lead` fell into by treating
rejected leads as free wins (see `migrations/0005`'s fix). The denominator is
always every sample in the corpus.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from arie.llm.schema import ExpectedLabel, ExtractedSignal, LabeledSample

# Fully synthetic: fictional companies, fictional people, no sanitised real
# leads — the same reason `arie.evalgen.generator` uses invented identities
# (docs/benchmark.md: "so an LLM cannot recognise a real company from
# pretraining"). Phrasing is deliberately mixed: some samples use exactly the
# phrases `arie.llm.baseline` matches on (realistic — those phrases are common
# precisely because they're common), and roughly half deliberately do not, so
# the comparison measures something other than "did we write the test cases
# from the baseline's own keyword list."
LABELED_SAMPLES: tuple[LabeledSample, ...] = (
    # --- clear buying intent, mixed phrasing -------------------------------
    LabeledSample(
        "intent-01",
        "We've reviewed your product and would like to move forward — please "
        "send a contract for our team of 40 seats.",
        ExpectedLabel(True, None, False),
    ),
    LabeledSample(
        "intent-02",
        "Can someone from sales call me this week? We want to get started as soon as possible.",
        ExpectedLabel(True, None, False),
    ),
    LabeledSample(
        "intent-03",
        "Our finance team has signed off and we're ready to purchase 3 licenses.",
        ExpectedLabel(True, None, False),
    ),
    LabeledSample(
        "intent-04",
        "I'd like to schedule a demo — we're actively comparing your platform "
        "against two competitors and plan to decide by end of quarter.",
        ExpectedLabel(True, None, False),
    ),
    LabeledSample(
        "intent-05",
        "Please have your account executive reach out — we have budget "
        "allocated for this initiative and want to move quickly.",
        ExpectedLabel(True, None, False),
    ),
    # --- clear disqualifiers, mixed phrasing -------------------------------
    LabeledSample(
        "disqualify-01",
        "Thanks for reaching out but we don't have any budget for new tools this year.",
        ExpectedLabel(False, None, True),
    ),
    LabeledSample(
        "disqualify-02",
        "Please remove me from your mailing list, I'm not interested.",
        ExpectedLabel(False, None, True),
    ),
    LabeledSample(
        "disqualify-03",
        "We already signed with a competitor last month, so this isn't relevant anymore.",
        ExpectedLabel(False, None, True),
    ),
    LabeledSample(
        "disqualify-04",
        "I was just doing some initial research for a paper, not actually shopping for a vendor.",
        ExpectedLabel(False, None, True),
    ),
    LabeledSample(
        "disqualify-05",
        "Our company is far too small for an enterprise platform like this — maybe in a few years.",
        ExpectedLabel(False, None, True),
    ),
    # --- trigger events, one pair per category (one matches the baseline's
    # phrase list, one deliberately doesn't) --------------------------------
    LabeledSample(
        "trigger-funding-01",
        "We just closed our Series B and are scaling the team fast.",
        ExpectedLabel(False, "funding_event", False),
    ),
    LabeledSample(
        "trigger-funding-02",
        "We recently secured $12M in new investment from our lead investor.",
        ExpectedLabel(False, "funding_event", False),
    ),
    LabeledSample(
        "trigger-leadership-01",
        "Our new VP of Engineering asked me to look into this.",
        ExpectedLabel(False, "leadership_change", False),
    ),
    LabeledSample(
        "trigger-leadership-02",
        "We brought on a new Chief Technology Officer last month who is driving this evaluation.",
        ExpectedLabel(False, "leadership_change", False),
    ),
    LabeledSample(
        "trigger-expansion-01",
        "We are expanding to the European market this year.",
        ExpectedLabel(False, "expansion", False),
    ),
    LabeledSample(
        "trigger-expansion-02",
        "We're opening a new office in Austin next quarter.",
        ExpectedLabel(False, "expansion", False),
    ),
    LabeledSample(
        "trigger-product-01",
        "We're in the process of replacing our legacy CRM with something modern.",
        ExpectedLabel(False, "product_or_technology_change", False),
    ),
    LabeledSample(
        "trigger-product-02",
        "Our engineering team is evaluating a full rewrite of our data pipeline.",
        ExpectedLabel(False, "product_or_technology_change", False),
    ),
    # --- genuinely no signal ------------------------------------------------
    LabeledSample("neutral-01", "What are your office hours?", ExpectedLabel(False, None, False)),
    LabeledSample(
        "neutral-02",
        "Do you have documentation available in Spanish?",
        ExpectedLabel(False, None, False),
    ),
    LabeledSample(
        "neutral-03",
        "I saw your talk at the conference last week, great presentation.",
        ExpectedLabel(False, None, False),
    ),
    LabeledSample(
        "neutral-04",
        "Can you confirm receipt of my previous email?",
        ExpectedLabel(False, None, False),
    ),
    # --- traps: a naive substring match misfires here -----------------------
    LabeledSample(
        "trap-01",
        "We are not ready to buy yet, maybe check back with us next year.",
        ExpectedLabel(False, None, False),
    ),
    LabeledSample(
        "trap-02",
        "We're not interested in switching platforms right now, but thanks for the outreach.",
        ExpectedLabel(False, None, True),
    ),
    LabeledSample(
        "trap-03",
        "A friend who works in procurement at another company recommended I "
        "reach out, but we're not evaluating anything ourselves right now.",
        ExpectedLabel(False, None, False),
    ),
    # --- combined signal: trigger event and buying intent together ---------
    LabeledSample(
        "combined-01",
        "Since closing our Series B, we're finally ready to invest in proper "
        "tooling — can we get a quote?",
        ExpectedLabel(True, "funding_event", False),
    ),
)


@dataclass(frozen=True)
class FieldAccuracy:
    field: str
    correct: int
    total: int

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0


@dataclass(frozen=True)
class EvalReport:
    extractor_name: str
    n_samples: int
    buying_intent: FieldAccuracy
    trigger_category: FieldAccuracy
    disqualifying_signal: FieldAccuracy
    exact_match: FieldAccuracy
    """All three scored fields correct on the same sample — the strict metric."""
    failed_extractions: tuple[str, ...]
    """sample_ids where `extract` returned None. Still scored as wrong on every
    field (see module docstring), listed here for what actually failed."""


def evaluate_extractor(
    name: str,
    extract: Callable[[str], ExtractedSignal | None],
    samples: Sequence[LabeledSample] = LABELED_SAMPLES,
) -> EvalReport:
    """Run `extract` over `samples` and score against their `expected` labels.

    `extract` returning `None` (an extraction failure — see
    `arie.llm.deepseek.ExtractionOutcome.signal`) is scored as incorrect on
    every field for that sample, never skipped.
    """
    buying_intent_correct = 0
    trigger_correct = 0
    disqualifying_correct = 0
    exact_correct = 0
    failed: list[str] = []

    for sample in samples:
        result = extract(sample.text)
        if result is None:
            failed.append(sample.sample_id)
            continue

        buying_intent_ok = result.has_buying_intent == sample.expected.has_buying_intent
        trigger_ok = result.trigger_event_category == sample.expected.trigger_event_category
        disqualifying_ok = result.disqualifying_signal == sample.expected.disqualifying_signal

        buying_intent_correct += buying_intent_ok
        trigger_correct += trigger_ok
        disqualifying_correct += disqualifying_ok
        exact_correct += buying_intent_ok and trigger_ok and disqualifying_ok

    n = len(samples)
    return EvalReport(
        extractor_name=name,
        n_samples=n,
        buying_intent=FieldAccuracy("has_buying_intent", buying_intent_correct, n),
        trigger_category=FieldAccuracy("trigger_event_category", trigger_correct, n),
        disqualifying_signal=FieldAccuracy("disqualifying_signal", disqualifying_correct, n),
        exact_match=FieldAccuracy("exact_match", exact_correct, n),
        failed_extractions=tuple(failed),
    )


@dataclass(frozen=True)
class DeltaReport:
    """The LLM's contribution: challenger's accuracy minus the baseline's, per field."""

    baseline: EvalReport
    challenger: EvalReport

    @property
    def buying_intent_delta(self) -> float:
        return self.challenger.buying_intent.accuracy - self.baseline.buying_intent.accuracy

    @property
    def trigger_category_delta(self) -> float:
        return self.challenger.trigger_category.accuracy - self.baseline.trigger_category.accuracy

    @property
    def disqualifying_signal_delta(self) -> float:
        return (
            self.challenger.disqualifying_signal.accuracy
            - self.baseline.disqualifying_signal.accuracy
        )

    @property
    def exact_match_delta(self) -> float:
        return self.challenger.exact_match.accuracy - self.baseline.exact_match.accuracy


def compute_delta(baseline: EvalReport, challenger: EvalReport) -> DeltaReport:
    if baseline.n_samples != challenger.n_samples:
        raise ValueError(
            f"baseline scored {baseline.n_samples} samples, challenger scored "
            f"{challenger.n_samples} — a delta over different corpora is not comparable"
        )
    return DeltaReport(baseline=baseline, challenger=challenger)
