# Evaluation Dataset Design

The benchmark's credibility rests entirely on this dataset. A synthetic dataset
generated carelessly measures the generator, not the policy.

## Three failure modes this design defends against

1. **Single-source recoverability** — if the oracle label is derivable from one
   cheap field, an adaptive policy "wins" by doing nothing interesting.
2. **Independent provider noise** — if misses are i.i.d., "try the next provider"
   always eventually works, and the *stopping* decision is never tested.
3. **No genuine ambiguity** — if every lead is cleanly separable given enough
   calls, calibrated confidence and human escalation have nothing to do.

## Latent truth vs. observations

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

### On the oracle using the production scorer

The oracle applies the *same* deterministic scoring function as production,
evaluated on latent truth instead of partial evidence. Since scoring is
rule-based by design, the scorer matches the oracle **by construction** once
fully informed.

This is intentional, and stating it plainly matters. What is under test is the
**controller's behaviour** — which sources it calls, how many, at what cost, and
whether it stops appropriately — not whether the arithmetic agrees with itself. A
reviewer who assumed otherwise would rightly suspect a rigged benchmark.

## Provider observation model

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
[`ASSUMPTIONS.md`](ASSUMPTIONS.md).

## Difficulty bands

| Band | Share | Property |
|---|---|---|
| Easy | ~40% | Recoverable from the 1–2 cheapest providers. Adaptive should stop almost immediately — most savings originate here, and that gets reported |
| Medium | ~35% | Cheap providers give ambiguous/conflicting signals; a mid-cost provider is needed. Where EVoI earns its keep |
| Hard | ~25% | Genuine ambiguity even fully informed, and/or the decision-flipping signal sits behind the most expensive provider. A sub-fraction is irreducibly hard and stays unresolved after full spend — this sets an honest floor for escalation rate and **is deliberately not engineered to zero** |

A sized subset (~20–30% of medium+hard) is built so **cheap-only evidence gives
the wrong answer** and only paid evidence corrects it. This subset bounds the
maximum achievable gain from adaptive enrichment, and its size is reported
directly so gains cannot look inflated.

## Structure

- **~900 leads across ~420 companies.** Multiple contacts per company is
  essential — without it, cache hit rate is always ~0 and the largest real-world
  cost lever goes unmeasured.
- **~5% ambiguous-identity subset** (`Acme Inc` / `Acme Corporation` / `acme.com`)
  making deterministic-match failure rate measurable, which converts the Splink
  decision from taste into data.
- **Splits:** 600 calibration (fits the calibration model and conformal τ; never
  touched by the benchmark) + 300 test, stratified across difficulty × value tier.
  Calibration was enlarged 4x from an original 150 — see
  [`05-results.md`](05-results.md#what-enlarging-the-calibration-split-changed) —
  after which the test split is unaffected by construction: company indices,
  RNG namespaces, and name blocks restart per split, asserted by
  `test_enlarging_calibration_leaves_the_test_split_untouched`.
- **Fully synthetic identities** — no sanitised real leads, specifically so an LLM
  cannot recognise a real company from pretraining and defeat the
  must-combine-sources premise.
- **Seeded and versioned** (`generator_version`, `seed`), so results are
  byte-reproducible.

## The dataset validates itself

Before any benchmark result is trusted, a CI test fits a trivial
cheapest-provider-only baseline against the oracle across the test set. If its F1
is not materially below the full-information ceiling (target: 15–25 points
lower), **the dataset is rejected as too easy and regenerated.**

Implemented as `tests/unit/test_dataset_is_nontrivial.py` and wired into CI. A
benchmark you can pass without trying proves nothing.

## Guardrails against p-hacking

Simulator parameters are set from documented rationale *before* any benchmark run
and then frozen. If they are ever retuned, that is a new dataset version, and
**both versions' results are reported** — never silently replaced. No post-hoc
selection of "good" leads.
