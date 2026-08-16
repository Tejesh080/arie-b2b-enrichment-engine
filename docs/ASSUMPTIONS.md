# Assumptions

Every number the simulator depends on, why it has the value it has, and how
wrong it could be. This file exists because the benchmark's credibility rests
entirely on these being reasonable — and on being honest that they are
**assumptions informed by public reporting, not measurements**.

> Rule for this project: no benchmark claim may depend on a parameter that isn't
> listed here with a stated justification.

---

## Provider cost parameters

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

## Coverage rates

| Parameter | Assumed | Basis |
|---|---|---|
| Cheap provider coverage | *TBD* | Reporting that a single email finder may cover ~55% of a list where three stacked cover 80%+ |
| Correlated obscurity draw | shared per-lead | Real providers fail together on messy accounts; independent misses would make the benchmark trivially easy |

## Noise model

- **Categorical fields**: confusable-neighbour swap with provider-specific
  probability (e.g. adjacent employee-count band, not a random band).
- **Continuous fields**: multiplicative log-normal noise.
- **Rationale**: real provider errors are systematically *near-miss*, not
  uniformly random. Uniform noise would make disagreement between providers
  trivially detectable, which would flatter any policy that aggregates them.

## Latency

Log-normal per provider, parameterised by declared (p50, p95). Basis: API latency
distributions are right-skewed; a normal distribution would understate tail
behaviour and therefore understate the latency penalty term in EVoI.

## Business value / deal-value tiers

Relative weights across small/medium/large/whale, not absolute dollars. Only the
*ratio* enters EVoI. **Deliberately asymmetric**: a false-reject on a whale costs
far more than a false-accept on a small lead, and the scoring reflects that.

## Dataset composition

| Parameter | Value | Rationale |
|---|---|---|
| Total leads | ~450 | 150 calibration + 300 test |
| Distinct companies | ~180 | Multiple contacts per company — otherwise cache hit rate is always ~0 and the largest real-world cost lever goes unmeasured |
| Difficulty split | 40 / 35 / 25 (easy/medium/hard) | Difficulty skew is the mechanism cascades exploit; must be present but not dominant |
| Cheap-evidence-misleads subset | ~20–30% of medium+hard | **Bounds the maximum possible gain from adaptive enrichment.** Reported explicitly so gains can't look inflated |
| Ambiguous-identity subset | ~5% | Makes deterministic-match failure rate measurable, converting the Splink decision into a data-driven one |
| Irreducibly-hard subset | subset of `hard` | Sets an honest floor for human escalation rate — deliberately not engineered to zero |

## Human review cost

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

## Business value tiers

Relative weights only; absolute levels are assumptions. They sit far above
provider prices, which is realistic and is precisely why teams over-enrich by
default — almost any call looks justified if it might change the answer.

## Known limitations

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
   arithmetic. See [`04-eval-dataset.md`](04-eval-dataset.md).
4. **Myopic (one-step-lookahead) policy**, not a full POMDP solve. Justified by
   CAM-DF's finding that ranking-plus-stopping is competitive in practice;
   non-myopic planning is future work, not a hidden shortcut.
