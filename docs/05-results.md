# Measured Results

Ten seeds. Dataset regenerated, confidence model refit, and waterfall re-tuned
per seed — each is what a fresh deployment would see. Test split is 300 held-out
leads, never touched by any fitting step.

Reproduce with:

```bash
make dataset && python -m bench.multi_seed --seeds 42 43 44 45 46 47 48 49 50 51
```

`python -m bench.multi_seed` alone now reproduces the same ten seeds by
default — the explicit `--seeds` list above is kept only because it is the
exact command that originally produced every number on this page, not because
it is still necessary. It wasn't always: see
["Reconciling this page against a fresh run"](#reconciling-this-page-against-a-fresh-run)
at the end of this document before assuming a number here is stale just
because it looks old.

---

## Headline: the thesis did not hold, and the ablation won

The project set out to show that expected-value-of-information reasoning beats
fixed enrichment pipelines. An ablation was built alongside it — every stopping
signal *except* EVoI ordering — specifically so any win could be attributed to
the mechanism it claimed.

The ablation won. It is now the production policy; EVoI is the documented
negative result.

### Mean across 10 seeds

| policy | agreement | API $/lead | calls | autonomy | auto err |
|---|---|---|---|---|---|
| full_enrichment | 0.8390 | 0.4447 | 8.00 | 0.816 | 0.0775 |
| waterfall_expensive | 0.8347 | 0.4205 | 7.58 | 0.795 | 0.0807 |
| **calibrated_bounds** (production) | **0.8113** | **0.2463** | 5.26 | **0.833** | 0.1155 |
| adaptive_voi_x1 (EVoI) | 0.8093 | 0.2906 | 2.19 | 0.786 | 0.1092 |
| escalation_aware @ $2.50 | 0.8120 | 0.2813 | 2.17 | 0.786 | 0.1052 |

The EVoI row is the single un-scaled policy (`value_scale=1.0`), held constant
across all ten seeds — the policy as originally proposed, not tuned in
hindsight. Every other EVoI figure on this page (the stability table, the
total-cost table, the break-even price, the per-seed verdicts) instead uses
`best_adaptive`: the best-of-seven `value_scale` variants *for that seed*,
chosen by agreement. That is deliberately the more generous comparison —
`best_adaptive` matched `adaptive_voi_x1` on only 1 of the 10 seeds, so this
is a real methodological difference, not rounding. It means the two are not
directly comparable cost figures: `adaptive_voi_x1` costs $0.2906/lead here,
while `best_adaptive`'s mean is $0.2793/lead in the total-cost table below at
the same $0 review price. Both are real, both reproduce exactly; they just
answer "the plain policy" versus "the best this approach could do with its own
tuning knob."

**The production policy is 41.6% cheaper than the tuned waterfall on average
across seeds (sd 11.0pp — see the stability table below) and 2.33pp lower on
decision agreement (sd 2.07pp).** In aggregate dollar terms — mean production
spend over mean waterfall spend, rather than the mean of each seed's own
saving — that is 41.4% cheaper than the tuned waterfall and 44.6% cheaper than
full enrichment. The two readings differ because a mean of ratios is not the
same number as a ratio of means; both are reported rather than picking one
silently.

It also has the *highest* autonomy of any policy — higher than buying
everything. Extra evidence can introduce conflict between sources and pull
confidence back down, so stopping when confident beats enriching past that
point.

### Against the pre-registered criteria

The criteria in [`03-mvp.md`](03-mvp.md) required ≤1pp agreement loss at ≥20%
cost reduction versus a tuned waterfall.

**No policy met them.** The production policy delivers 41% savings at 2.3pp
loss; EVoI delivers 33% at 1.9pp. Both trade more quality than the
pre-registration allowed. Per-seed verdicts for EVoI:

```
WEAK 4   THESIS_HOLDS 3   INCONCLUSIVE 2   THESIS_FALSIFIED 1
```

This is a frontier point with a stated trade-off, not a win. Stating it as a win
would require moving the line after seeing the data.

---

## Why EVoI did not pay off

`EVoI saving vs production: mean -16.1%` (range -34.8% to +16.9%). The EVoI
layer costs *more* than the policy without it, on 9 of 10 seeds, at equal or
better agreement.

The cause is diagnosable. Business value ranges $5–400 while provider prices
range $0.001–0.60, so the cost term barely moves the argmax and EVoI degenerates
to "buy the most informative provider". It makes 2.19 calls to the production
policy's 5.26 and still spends more, because it reaches for expensive sources
immediately while cheapest-first often reaches confidence before touching them.

Fewer calls is not cheaper when the calls are the expensive ones.

Full reasoning: [`adr/0004-evoi-is-a-negative-result.md`](adr/0004-evoi-is-a-negative-result.md).

---

## Total cost, including human review

Every number above counts API spend only. Escalations land on an analyst who is
not free, and analyst time dominates API prices by orders of magnitude.

Mean $/lead across seeds, at each review price:

| review $ | full | waterfall | **production** | EVoI | escalation-aware | cheapest |
|---|---|---|---|---|---|---|
| 0.00 | 0.4447 | 0.4205 | **0.2463** | 0.2793 | 0.2906 | production |
| 0.25 | 0.4907 | 0.4719 | **0.2880** | 0.3355 | 0.3443 | production |
| 1.00 | 0.6284 | 0.6259 | **0.4130** | 0.5040 | 0.4996 | production |
| 2.50 | 0.9039 | 0.9339 | **0.6630** | 0.8410 | 0.8155 | production |
| 5.00 | 1.3631 | 1.4472 | **1.0797** | 1.4027 | 1.3443 | production |

**The production policy is cheapest at every review price tested.** That is its
strongest claim and the one that should be quoted.

Break-even review price for EVoI versus the waterfall: mean $13.18, worst case
$0.81 (5 of 10 seeds have a crossover at all).

---

## Stability across seeds

| metric | mean | sd | min | max |
|---|---|---|---|---|
| production saving vs waterfall | 0.4156 | 0.1097 | 0.1371 | 0.5019 |
| production agreement gap (pp) | 2.33 | 2.07 | −0.67 | 6.33 |
| production agreement | 0.8113 | 0.0405 | 0.7300 | 0.8900 |
| production API $/lead | 0.2463 | 0.0530 | 0.1949 | 0.3721 |
| production autonomy | 0.8333 | 0.0948 | 0.5733 | 0.9033 |
| EVoI saving vs production | −0.1605 | 0.1525 | −0.3480 | 0.1692 |
| confidence ECE | 0.0614 | 0.0153 | 0.0454 | 0.0918 |
| τ | 0.7885 | 0.0706 | 0.6883 | 0.9253 |

**The variance is large relative to the effect.** Savings range 13.7% to 50.2%;
the agreement gap ranges −0.67pp (production *better* than the waterfall) to
+6.33pp. Per seed:

```
seed 42  48.2%  +4.00pp     seed 47  42.3%  +3.33pp
seed 43  39.3%  +1.00pp     seed 48  13.7%  -0.67pp
seed 44  50.2%  +3.00pp     seed 49  43.1%  +2.67pp
seed 45  47.3%  +2.00pp     seed 50  48.5%  +2.00pp
seed 46  48.6%  +6.33pp     seed 51  34.4%  -0.33pp
```

A single seed is not enough to judge a result this close to the noise floor. The
first run of this benchmark reported 29.0% at 1.00pp on seed 42 and read as a
clean win. It was not one.

---

## What enlarging the calibration split changed

Calibration went from 150 to 600 leads. Test generation was made provably
independent first — company indices, RNG namespaces, and name blocks restart per
split, asserted by
`test_enlarging_calibration_leaves_the_test_split_untouched`. Under the original
shared counter, adding calibration leads slid every test company's index and
silently regenerated the held-out data.

| | 150 leads | 600 leads |
|---|---|---|
| adaptive autonomy | 0.375 | 0.775 |
| τ range | 0.77 – 1.01 (one reject-all) | 0.69 – 0.93 |
| confidence ECE | 0.094 | 0.061 |
| EVoI break-even review price | $0.59 | $13.18 |

It also exposed a false result. At 150 calibration leads a threshold *was* found
at a 5% error budget, reporting zero errors above it. With four times the
evaluation states, the top confidence block has a measured error rate of roughly
8% against a stated 4%, and no operating point survives the bound — the system
correctly refuses to automate anything at 5%.

**The earlier 5% guarantee was small-sample luck.** The deployable budget is 10%,
met at roughly 45% coverage and stable across seeds. That is a policy choice with
a stated cost, recorded in `config.py` and pinned by
`test_five_percent_budget_is_not_achievable`.

---

## Reconciling this page against a fresh run

Two provenance gaps were found and closed here, neither of which changed a
published number — this section exists so that fact is checked, not assumed.

**`bench/multi_seed.py`'s tracked default was seven seeds, not ten.**
`DEFAULT_SEEDS` was `(42, ..., 48)` from the day the sweep was first built.
Every number on this page was actually produced by the `--seeds 42 43 44 45
46 47 48 49 50 51` override shown above — which was always documented as the
reproduction command — but the bare `python -m bench.multi_seed` that
README's Quick Start told a reader to run measured a different, smaller
sweep. Fixed by extending the default to the same ten seeds already
documented here, rather than picking a new sample.

**The committed `data/eval/manifest.json` described a dataset that hasn't
existed since before this page's numbers were measured.** It was last
regenerated before the calibration split was enlarged 150 → 600 (see below),
and never after. It has no effect on any benchmark or test — every consumer
regenerates the dataset in memory via `generate_dataset(seed=...)`, never
reads the committed file — so this was a stale snapshot, not a computational
bug. Regenerated to match current generation.

**What this means for the numbers above: nothing.** Nothing computational
changed between the commit that produced this page's numbers and the one
that made this fix (a verified behaviour-identical policy rename and
CLI-only refactors — see that commit's diff). A fresh run at
`--seeds 42 43 44 45 46 47 48 49 50 51` was executed to confirm rather than
assume this, and reproduced every figure on this page exactly: the headline
table, the full stability table, the complete total-cost table (all 25
cells), the break-even price, and the per-seed verdict counts. The
`adaptive_voi_x1` vs `best_adaptive` distinction noted after the headline
table was a real, previously undocumented convention discovered while
verifying this — consistently applied everywhere on this page, just never
written down until now.

---

## Limitations

1. **Synthetic data throughout.** The relative comparison between policies on
   identical data is the result; absolute numbers are not claims about real
   performance.
2. **Simulator parameters are assumptions**, not measurements — see
   [`ASSUMPTIONS.md`](ASSUMPTIONS.md).
3. **Human review cost is an assumption**, and it decides which policy wins on
   total cost. The sweep is reported instead of a single figure.
4. **The oracle uses the production scoring rules** on latent truth, so scoring
   matches it by construction once fully informed. What is under test is which
   evidence a policy chooses to buy, not the arithmetic.
5. **Ten seeds is a small sample** for a metric whose standard deviation is
   comparable to the effect being measured.
