# Benchmark

How ARIE's stopping policy was evaluated, what it scored, and every assumption
the result depends on.

The short version: a calibrated two-rule stopping policy buys **41.6% less
enrichment data** than a tuned waterfall baseline, and pays **2.3 percentage
points** of decision agreement for it. That did not meet the bar set before
running it (≤1pp loss at ≥20% cost reduction), so it is reported as a
trade-off, not a win.

Reproduce it:

```bash
make dataset && python -m bench.multi_seed --seeds 42 43 44 45 46 47 48 49 50 51
```

**Contents** — [Why no infrastructure is needed](#why-the-benchmark-needs-no-infrastructure) ·
[Dataset design](#dataset-design) · [Results](#results) · [Assumptions](#assumptions)

---

## Why the benchmark needs no infrastructure

Almost none of the infrastructure is required to prove the thesis. Postgres, the
job queue, the state machine, FastAPI, n8n, Slack, durable execution — none of it
changes whether EVoI-based stopping beats a tuned waterfall. That question is
settled entirely by the dataset, the simulator, the scorer, the confidence model,
and the controller: pure functions, in memory, no service.

That matters because of the main project risk: **adaptive gains may be small if
cheap signals are already good enough.** If that's true, it should be discovered
in week one — not after building a platform around a claim that doesn't hold.

So the MVP is two milestones with a hard gate between them:

- **M0 — The Proof.** Pure Python, no infrastructure. *Is the thesis true?*
- **M1 — The System.** The runtime that makes it a credible platform.

**M1 does not begin until M0 produces a result.**

---

---

## Dataset design

The benchmark's credibility rests entirely on this dataset. A synthetic dataset
generated carelessly measures the generator, not the policy.

### Three failure modes this design defends against

1. **Single-source recoverability** — if the oracle label is derivable from one
   cheap field, an adaptive policy "wins" by doing nothing interesting.
2. **Independent provider noise** — if misses are i.i.d., "try the next provider"
   always eventually works, and the *stopping* decision is never tested.
3. **No genuine ambiguity** — if every lead is cleanly separable given enough
   calls, calibrated confidence and human escalation have nothing to do.

### Latent truth vs. observations

Every lead has a **latent ground-truth vector** (never visible to the policy) and
separately **per-provider observations** of it (noisy, incomplete, sometimes
wrong). The oracle decision is computed from latent truth alone.

| Latent field | Role |
|---|---|
| `company_size_true` (banded) | firmographic fit |
| `industry_true` | firmographic fit |
| `title_seniority_true` / `title_function_true` | persona fit |
| `buying_intent_true` (0–1) | the hard-to-observe signal |
| `recent_trigger_event` (sparse) | only a late/expensive provider surfaces this |
| `disqualifying_flag` (rare) | overrides everything; often visible only late |
| `deal_value_tier` | feeds `business_value` in EVoI directly |
| `difficulty_band` | controls generation; never visible; used for stratified reporting |

#### On the oracle using the production scorer

The oracle applies the *same* deterministic scoring function as production,
evaluated on latent truth instead of partial evidence. Since scoring is
rule-based by design, the scorer matches the oracle **by construction** once
fully informed.

This is intentional, and stating it plainly matters. What is under test is the
**controller's behaviour** — which sources it calls, how many, at what cost, and
whether it stops appropriately — not whether the arithmetic agrees with itself. A
reviewer who assumed otherwise would rightly suspect a rigged benchmark.

### Provider observation model

For each (lead, provider):

1. **Coverage** — does this provider have anything? Reduced for `hard` leads via
   a **shared per-lead obscurity draw**, so misses correlate across providers the
   way real vendors fail together on the same messy accounts.
2. **Noise** — categorical fields get confusable-neighbour swaps; continuous
   fields get multiplicative log-normal noise. Real provider errors are
   systematically *near-miss*, not uniformly random.
3. **Latency** — log-normal from declared (p50, p95).
4. **Failure** — occasional hard error at a small base rate.

All parameters are documented assumptions, not measurements — see
[`ASSUMPTIONS.md`](#assumptions).

### Difficulty bands

| Band | Share | Property |
|---|---|---|
| Easy | ~40% | Recoverable from the 1–2 cheapest providers. Adaptive should stop almost immediately — most savings originate here, and that gets reported |
| Medium | ~35% | Cheap providers give ambiguous/conflicting signals; a mid-cost provider is needed. Where EVoI earns its keep |
| Hard | ~25% | Genuine ambiguity even fully informed, and/or the decision-flipping signal sits behind the most expensive provider. A sub-fraction is irreducibly hard and stays unresolved after full spend — this sets an honest floor for escalation rate and **is deliberately not engineered to zero** |

A sized subset (~20–30% of medium+hard) is built so **cheap-only evidence gives
the wrong answer** and only paid evidence corrects it. This subset bounds the
maximum achievable gain from adaptive enrichment, and its size is reported
directly so gains cannot look inflated.

### Structure

- **~900 leads across ~420 companies.** Multiple contacts per company is
  essential — without it, cache hit rate is always ~0 and the largest real-world
  cost lever goes unmeasured.
- **~5% ambiguous-identity subset** (`Acme Inc` / `Acme Corporation` / `acme.com`)
  making deterministic-match failure rate measurable, which converts the Splink
  decision from taste into data.
- **Splits:** 600 calibration (fits the calibration model and conformal τ; never
  touched by the benchmark) + 300 test, stratified across difficulty × value tier.
  Calibration was enlarged 4x from an original 150 — see
  [`05-results.md`](#results) —
  after which the test split is unaffected by construction: company indices,
  RNG namespaces, and name blocks restart per split, asserted by
  `test_enlarging_calibration_leaves_the_test_split_untouched`.
- **Fully synthetic identities** — no sanitised real leads, specifically so an LLM
  cannot recognise a real company from pretraining and defeat the
  must-combine-sources premise.
- **Seeded and versioned** (`generator_version`, `seed`), so results are
  byte-reproducible.

### The dataset validates itself

Before any benchmark result is trusted, a CI test fits a trivial
cheapest-provider-only baseline against the oracle across the test set. If its F1
is not materially below the full-information ceiling (target: 15–25 points
lower), **the dataset is rejected as too easy and regenerated.**

Implemented as `tests/unit/test_dataset_is_nontrivial.py` and wired into CI. A
benchmark you can pass without trying proves nothing.

### Guardrails against p-hacking

Simulator parameters are set from documented rationale *before* any benchmark run
and then frozen. If they are ever retuned, that is a new dataset version, and
**both versions' results are reported** — never silently replaced. No post-hoc
selection of "good" leads.


---

## Results

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

### Headline: the thesis did not hold, and the ablation won

The project set out to show that expected-value-of-information reasoning beats
fixed enrichment pipelines. An ablation was built alongside it — every stopping
signal *except* EVoI ordering — specifically so any win could be attributed to
the mechanism it claimed.

The ablation won. It is now the production policy; EVoI is the documented
negative result.

#### Mean across 10 seeds

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

#### Against the pre-registered criteria

The criteria in [`03-mvp.md`](#why-the-benchmark-needs-no-infrastructure) required ≤1pp agreement loss at ≥20%
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

### Why EVoI did not pay off

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

### Total cost, including human review

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

### Stability across seeds

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

### What enlarging the calibration split changed

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

### Reconciling this page against a fresh run

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

### Limitations

1. **Synthetic data throughout.** The relative comparison between policies on
   identical data is the result; absolute numbers are not claims about real
   performance.
2. **Simulator parameters are assumptions**, not measurements — see
   [`ASSUMPTIONS.md`](#assumptions).
3. **Human review cost is an assumption**, and it decides which policy wins on
   total cost. The sweep is reported instead of a single figure.
4. **The oracle uses the production scoring rules** on latent truth, so scoring
   matches it by construction once fully informed. What is under test is which
   evidence a policy chooses to buy, not the arithmetic.
5. **Ten seeds is a small sample** for a metric whose standard deviation is
   comparable to the effect being measured.


---

## Assumptions

Every number the simulator depends on, why it has the value it has, and how
wrong it could be. This file exists because the benchmark's credibility rests
entirely on these being reasonable — and on being honest that they are
**assumptions informed by public reporting, not measurements**.

> Rule for this project: no benchmark claim may depend on a parameter that isn't
> listed here with a stated justification.

---

### Provider cost parameters

| Parameter | Assumed value | Basis | Confidence |
|---|---|---|---|
| Cheap-tier provider cost | *TBD at implementation* | Public list pricing for basic firmographic lookups | Medium |
| Mid-tier provider cost | *TBD* | Public list pricing for contact/email finders | Medium |
| Expensive-tier cost | *TBD* | Public pricing for intent/trigger-signal sources | Low |

Directional basis: industry reporting on waterfall enrichment describes a
5-provider waterfall consuming roughly 13–24 credits per contact depending on
provider mix, and a 2-step waterfall roughly 4–6 — i.e. **costs differ by roughly
an order of magnitude across tiers**, which is the property that matters for EVoI.
Absolute prices matter far less than the *ratio* between tiers.

⚠️ Sources conflict on whether misses are billed. Clay's documentation states
credits are charged only when a provider returns data; its community forum
describes per-attempt charging with refunds on miss. **The simulator models both**
(`bill_on_miss: bool`) and the benchmark reports results under both regimes,
since this materially changes waterfall economics.

### Coverage rates

| Parameter | Assumed | Basis |
|---|---|---|
| Cheap provider coverage | *TBD* | Reporting that a single email finder may cover ~55% of a list where three stacked cover 80%+ |
| Correlated obscurity draw | shared per-lead | Real providers fail together on messy accounts; independent misses would make the benchmark trivially easy |

### Noise model

- **Categorical fields**: confusable-neighbour swap with provider-specific
  probability (e.g. adjacent employee-count band, not a random band).
- **Continuous fields**: multiplicative log-normal noise.
- **Rationale**: real provider errors are systematically *near-miss*, not
  uniformly random. Uniform noise would make disagreement between providers
  trivially detectable, which would flatter any policy that aggregates them.

### Latency

Log-normal per provider, parameterised by declared (p50, p95). Basis: API latency
distributions are right-skewed; a normal distribution would understate tail
behaviour and therefore understate the latency penalty term in EVoI.

### Business value / deal-value tiers

Relative weights across small/medium/large/whale, not absolute dollars. Only the
*ratio* enters EVoI. **Deliberately asymmetric**: a false-reject on a whale costs
far more than a false-accept on a small lead, and the scoring reflects that.

### Dataset composition

| Parameter | Value | Rationale |
|---|---|---|
| Total leads | ~900 | 600 calibration + 300 test. Calibration was 150 (~450 total) before the calibration split was enlarged 4x — see [`05-results.md`](#results) — and this row went stale for a while after that change; corrected as part of the M1 Step 9 gate's benchmark-provenance reconciliation |
| Distinct companies | ~420 | Multiple contacts per company — otherwise cache hit rate is always ~0 and the largest real-world cost lever goes unmeasured |
| Difficulty split | 40 / 35 / 25 (easy/medium/hard) | Difficulty skew is the mechanism cascades exploit; must be present but not dominant |
| Cheap-evidence-misleads subset | ~20–30% of medium+hard | **Bounds the maximum possible gain from adaptive enrichment.** Reported explicitly so gains can't look inflated |
| Ambiguous-identity subset | ~5% | Makes deterministic-match failure rate measurable, converting the Splink decision into a data-driven one |
| Irreducibly-hard subset | subset of `hard` | Sets an honest floor for human escalation rate — deliberately not engineered to zero |

### Human review cost

The single most consequential assumption in the project, because it decides
whether the headline API saving survives a full accounting.

| Price / escalated lead | Roughly equivalent to |
|---|---|
| $0.00 | the implicit assumption of an API-only accounting |
| $0.25 | ~20 seconds of a $45/hr analyst |
| $1.00 | ~80 seconds |
| $2.50 | ~3 minutes — a realistic floor for a considered look at a lead |
| $5.00 | ~6 minutes — a careful review |

Rather than pick one, the benchmark sweeps the range and reports the
**break-even price**: the review cost above which the cheaper-on-API policy
stops being cheaper overall. Measured at **$0.59 mean / $0.16 worst case**
across ten seeds — i.e. under a minute of analyst time. Above that, adaptive
enrichment is the *most* expensive option, not the least.

Any claim about savings that does not state a review price is incomplete.

### Business value tiers

Relative weights only; absolute levels are assumptions. They sit far above
provider prices, which is realistic and is precisely why teams over-enrich by
default — almost any call looks justified if it might change the answer.

### Model prices (M1 cost ledger)

Added with the cost ledger in M1 Step 9 (`arie.ledger.pricing`). These are the
figures the ledger multiplies token counts by to produce `model_calls.cost_usd`,
which flows into `v_lead_cost` and `v_model_escalation`.

| Model | Tier | Input $/1M | Output $/1M | Basis | Confidence |
|---|---|---|---|---|---|
| `deepseek-chat` | cheap | 0.27 | 1.10 | Published list price, transcribed by hand | **Unverified** |
| `deepseek-reasoner` | strong | 0.55 | 2.19 | Published list price, transcribed by hand | **Unverified** |

⚠️ **None of this is measured, and no live model call has ever been made by this
project.** If a price is stale, or a discount or cache-token rate applies, every
cost figure derived from it is wrong by exactly that factor and nothing in the
system will notice — a wrong price produces a plausible number, not an error.

Unlike provider costs, which the provider *reports* and the ledger records
verbatim, model costs are **derived**. That asymmetry is why these sit in this
file and provider prices largely don't.

**Step 10 wired the first real model call (`arie.llm.deepseek`) but did not
reconcile this table** — worth being precise about, since the sentence above
predicted it would. No live DeepSeek call was made while building Step 10: no
`DEEPSEEK_API_KEY` was available in that environment, and the module was
built and verified entirely against `httpx.MockTransport` (see
`tests/unit/test_llm_deepseek_client.py`) plus a live-database ledger test
that constructs its `ExtractionOutcome` directly rather than calling the real
API (`tests/integration/test_llm_ledger_integration.py`). The prices above are
therefore still exactly as unverified as they were before Step 10, and the
first live run — `python -m bench.llm_signal_eval` with a real key set — is
still the reconciliation this section is waiting for. Treat any model-cost
number this system produces as conditional on the above until that happens.

An unpriced model raises `UnknownModelError` rather than being ledgered at
$0.00 — free is indistinguishable downstream from genuinely cheap, so the
cascade would appear to be saving money at exactly the moment it was spending
money nobody was tracking.

### LLM signal-extraction eval corpus (M1 Step 10)

`arie.llm.eval.LABELED_SAMPLES` — 26 hand-written, fully synthetic samples,
hand-labeled by the same person who wrote them. Stated the same way
`05-results.md` states its own sample-size caveat: **this is a smoke-test-scale
corpus, not a statistically powered one.** A field with 26 samples moves in
steps of ~3.8 percentage points; a reported delta smaller than that is noise,
not a finding. It exists to give `bench/llm_signal_eval.py` something concrete
to measure, not to support a claim like "the LLM is N% more accurate" at any
real confidence level.

Two things keep it a fair comparison rather than a rigged one, both worth
stating because neither is enforced by any test — a future edit to the corpus
could quietly break either:

- **Self-labeled, not adjudicated.** No second labeler checked these against
  the deterministic baseline's own phrase list beforehand — see
  `test_the_labeled_corpus_is_not_degenerate_for_the_baseline`, which is the
  one mechanical check that exists: the baseline must score neither 0% nor
  100% on any field, which at least rules out a corpus trivially rigged
  either for or against it.
- **Deliberately mixed phrasing.** Roughly half the samples use phrasing the
  baseline's own keyword list matches; the rest use different real-world
  phrasing (different tense, a synonym, a spelled-out title instead of an
  abbreviation) chosen to be things a keyword list plausibly misses. If every
  sample were written *from* the baseline's phrase list, of course the
  baseline would score perfectly and the delta would only measure that
  circularity.

### Known limitations

1. **Synthetic data throughout.** No real lead data. Absolute numbers are not
   claims about real-world performance; the *relative* comparison between
   strategies on identical data is the result.
2. **Parameters are not fitted to observed provider behaviour.** Until a real
   adapter is wired and its distribution compared against the assumed profile,
   these remain informed guesses.
3. **The oracle uses the same scoring function as production**, evaluated on
   latent truth. Scoring therefore matches the oracle by construction once fully
   informed — this is intentional. What's under test is the *controller's*
   behaviour (which sources, how many, what cost, when to stop), not the
   arithmetic. See [`04-eval-dataset.md`](#dataset-design).
4. **Myopic (one-step-lookahead) policy**, not a full POMDP solve. Justified by
   CAM-DF's finding that ranking-plus-stopping is competitive in practice;
   non-myopic planning is future work, not a hidden shortcut.

---

## Prior art

- **Active feature acquisition** — [Survey](https://arxiv.org/abs/2502.11067), [AFABench](https://arxiv.org/html/2508.14734)
- **Cost-aware stopping** — [Scores Are Not Decisions](https://arxiv.org/abs/2607.27083)
- **Model cascades** — [FrugalGPT](https://arxiv.org/abs/2305.05176)
- **Router evaluation** — [RouteLLM](https://github.com/lm-sys/RouteLLM) — publish a frontier, not a point
- **Entity resolution** — [Splink](https://github.com/moj-analytical-services/splink)

The contribution is not the theory. It is applying it to a domain where the
prevailing practice is coverage heuristics, measuring whether it helps, and
reporting that the sophisticated half of it did not.
