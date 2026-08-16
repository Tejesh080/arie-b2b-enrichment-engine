# Adaptive Revenue Intelligence Engine

**Cost-aware evidence acquisition for lead qualification — and an honest account
of which parts of it actually worked.**

Most lead-enrichment pipelines call every data provider for every lead, then ask
an LLM for a score. This system instead treats enrichment as a sequential
decision problem: *given what I already know, is the next API call worth its
price?*

---

## The result, up front

The founding thesis was that **expected-value-of-information** reasoning would
beat fixed enrichment pipelines. It was built, benchmarked against a deliberately
strong baseline, and tested against pre-registered success criteria.

**It did not hold.** A far simpler policy — deterministic score bounds plus a
calibrated confidence gate — beat the EVoI controller on 9 of 10 seeds, at equal
or better decision quality, and is cheaper at every human-review price tested.

That simpler policy is now the product. EVoI is retained as a documented negative
result: [ADR 0004](docs/adr/0004-evoi-is-a-negative-result.md).

### Mean across 10 seeds, 300 held-out test leads

| policy | agreement | API $/lead | calls | autonomy |
|---|---|---|---|---|
| full enrichment (call everything) | 0.8390 | 0.4447 | 8.00 | 0.816 |
| tuned waterfall (industry baseline) | 0.8347 | 0.4205 | 7.58 | 0.795 |
| **calibrated bounds** ← production | **0.8113** | **0.2463** | 5.26 | **0.833** |
| adaptive EVoI | 0.8093 | 0.2906 | 2.19 | 0.786 |

**41.6% cheaper than a tuned waterfall, at 2.3pp lower decision agreement**, and
cheapest on total cost at every human-review price tested. Standard deviation on
that saving is 11.0pp — the variance is large relative to the effect, and
[the results doc](docs/05-results.md) reports it per seed rather than burying it.

> **No policy met the pre-registered criteria** (≤1pp agreement loss at ≥20% cost
> reduction). This is a frontier point with a stated trade-off, not a win.
> Calling it a win would mean moving the line after seeing the data.

---

## What the production policy does

```
1. Walk providers cheapest-first.
2. Stop when the decision is provably SETTLED — no unbought evidence
   could change it, whatever it turned out to say.
3. Stop when calibrated CONFIDENCE clears tau — safe to act without a human.
4. Otherwise buy the next provider.
```

Both rules are load-bearing and answer different questions. *Settled* asks "can
anything still change this?" *Confidence* asks "is this right?" Bounds are
computed from observed facts, which are noisy, so a settled decision can still be
wrong — neither rule subsumes the other.

**Score bounds.** Given what is known, the reachable score has a floor and a
ceiling set by what unknown fields could contribute. When that whole interval
falls on one side of a decision boundary, no purchasable evidence can change the
outcome. One asymmetry drives real behaviour: while the disqualifying flag is
unknown the floor is *zero*, because a blocker can nullify any lead however
strong — so auto-routing can never be *proven* safe until it is checked.

**Calibrated confidence.** Not an LLM saying `0.82`. A logistic model over
uncertainty features, Platt/isotonic-calibrated on out-of-fold predictions
grouped by company, with published ECE and reliability bins. The autonomy
threshold τ comes from a **Clopper-Pearson upper bound** on selective error, not
the observed rate — zero errors in ten accepted predictions is weak evidence, and
the bound says so.

Measured: **ECE 0.061**, τ ≈ 0.79, autonomy ≈ 0.83 — higher autonomy than buying
everything, because extra evidence can introduce conflict between sources and
pull confidence back down.

---

## Things this project found the hard way

Each is a bug or false result caught by measurement, and each is pinned by a
regression test.

| Finding | Why it mattered |
|---|---|
| A 5% autonomous-error guarantee was **small-sample luck** | With 4x the evaluation data the top confidence block shows ~8% error against a stated ~4%. The deployable budget is 10%; at 5% the system correctly automates nothing. |
| The first EVoI estimator **paralysed the policy** | From a cold start no single provider can move the score past a threshold, so every candidate scored zero and nothing was ever bought. Textbook myopia. |
| Raising business value made the policy buy **less** | Budget affordability was checked *after* the argmax, and the priciest provider tends to win it. The inversion in the results table was the bug's signature. |
| Company-scoped providers returned **per-person** answers | Made company-level caching lossy and let full enrichment disagree with itself. |
| Two contacts drew the **same email** | Email is the canonical person key, so two distinct people were silently merged. |
| Difficulty was **confounded** with cache-hit opportunity | Contact count was drawn from the company RNG, so it depended on which code branch ran. |
| `expires_at` could not be a **`GENERATED`** column | `timestamptz + interval` is STABLE, not IMMUTABLE. Found by running the migration against a real database for the first time. |
| Cost per *qualified* lead **counted rejections as qualified** | `SYNCED` is where the reject branch terminates, so rejecting more leads made the metric look better — inverting the failure mode the view exists to expose. |
| Escalation rate counted a lead **once per review** | A `LEFT JOIN` to `human_reviews` fanned each lead out per review, so a twice-reviewed lead was two leads. |
| The lead budget cap defaulted **below the priciest provider** | `$0.50` against `deep_research` at `$0.600` — the only source of the disqualifying flag. The config had fixed this; the column default never got the memo. |

---

## Architecture

```
   n8n (thin edge: ingest webhook, CRM sync-out)          [M1, not built]
                     |
                     v
        Ingestion API (FastAPI) --> Postgres / Supabase   [M1, BUILT]
          POST /leads: identity resolution + lead +
          first job, in ONE transaction
                                      |
                          jobs.trace_context carries the
                          W3C trace across the process boundary
                                      v
                          Worker (SKIP LOCKED queue)      [M1, mechanism built,
                                      |                    handlers deferred]
  +-----------------------------------+----------------------------+
  |                    M0 - BUILT AND MEASURED                      |
  |                                                                 |
  |   Deterministic scorer --> score bounds --> settled?            |
  |   Calibrated confidence --> tau --> autonomous?                 |
  |                     |                                           |
  |                     v                                           |
  |        Provider adapter layer (Protocol)                        |
  |        SimulatedProvider | real APIs - indistinguishable        |
  +-----------------------------------------------------------------+
```

M0 — the intelligence engine and its benchmark — is complete. M1 is partly
built: schema and migrations, evidence store, identity resolution, the job
queue and transactional state machine, and now the ingestion API, cost ledger,
and end-to-end tracing. **The worker still has no real handlers** — nothing yet
calls `CalibratedBoundsPolicy` in production, on purpose. See
[the handoff](docs/06-m1-handoff.md) for exactly what is and isn't wired.

**Deliberately rejected**, with reasons in [`docs/adr/`](docs/adr/): LangGraph
(the loop is a calculation, not agentic reasoning), Temporal (single-service
scale), Redis (Postgres gives transactional consistency with lead state),
RAG/pgvector (no genuine use case survived scrutiny), "multiple agents" (naming,
not architecture).

---

## Quick start

```bash
pip install -e ".[dev,service]"
make dataset            # generate the seeded evaluation set
make validate-dataset   # CI gate: assert the dataset is non-trivial
make bench              # single-seed benchmark - no API keys, no network
python -m bench.multi_seed   # 10 seeds + human-review cost sweep
```

Everything runs offline against the provider simulator. Anyone cloning this repo
reproduces every number in [`docs/05-results.md`](docs/05-results.md)
byte-for-byte — reproducibility is a design goal enforced in CI, not a claim.

---

## Evaluation design

Each synthetic lead carries a **latent truth vector** never visible to the
policy; providers return noisy, incomplete *observations* of it. The oracle
decision is computed from latent truth alone.

Properties that took real effort to get right:

- **Correlated misses.** One per-company obscurity draw degrades every provider's
  coverage together. With independent misses, "try the next provider" always
  works and the stopping decision is never tested.
- **Company-disjoint splits, structurally.** A company is generated *into* one
  split and cannot appear in both. Enlarging the calibration set provably leaves
  the test set byte-identical — asserted by test, after the original shared
  counter silently regenerated held-out data.
- **The dataset validates itself.** A CI test asserts a cheapest-provider-only
  baseline scores materially below the full-information ceiling. If the dataset
  is too easy it is rejected and regenerated.

Details: [`docs/04-eval-dataset.md`](docs/04-eval-dataset.md).

---

## Repository layout

```
src/arie/
  core/          domain types - pure, no I/O
  evalgen/       latent-truth dataset generator
  providers/     Protocol + simulated (+ real adapters, deferred)
  scoring/       deterministic rules, evidence resolution, score bounds
  confidence/    calibration, conformal threshold, ECE
  policy/
    production.py        <- the recommended policy
    baselines.py         full enrichment, tuned waterfall
    adaptive.py          EVoI - negative result, retained
    escalation_aware.py  EVoI + human-review pricing - also negative
  evidence/      PostgresEvidenceStore - TTL/cache semantics (M1)
  identity/      deterministic company/person resolution (M1)
  jobs/          Postgres job queue - SKIP LOCKED, backoff, dead-letter (M1)
  statemachine/  pure transition graph + atomic apply_transition (M1)
  api/           FastAPI ingestion/runtime API - POST /leads (M1)
  ledger/        durable provider/model cost ledger + model pricing (M1)
  observability/ OpenTelemetry setup and cross-process trace propagation (M1)
bench/           harness, metrics, cost model, seed sweep
migrations/      numbered SQL (M1) - source of truth, applied via scripts/migrate.py
supabase/        migrations/ mirror (generated) + config, for GitHub Branching PR previews
scripts/         migrate.py, sync_supabase_migrations.py
tests/           unit + integration (DB-backed, opt-in via `make test-all`)
docs/            research, architecture, results, ADRs, handoff
```

Two migration directories, one source of truth: see
[ADR 0005](docs/adr/0005-migration-source-of-truth.md).

---

## Documentation

| Doc | Contents |
|---|---|
| [`01-research.md`](docs/01-research.md) | Competitive analysis: 10 comparable projects |
| [`02-architecture.md`](docs/02-architecture.md) | System design and trade-offs |
| [`03-mvp.md`](docs/03-mvp.md) | Scope and the pre-registered success criteria |
| [`04-eval-dataset.md`](docs/04-eval-dataset.md) | Dataset generation and validity testing |
| [`05-results.md`](docs/05-results.md) | **Measured results, 10 seeds, honest verdict** |
| [`06-m1-handoff.md`](docs/06-m1-handoff.md) | What M1 needs to know |
| [`ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) | Every parameter and its justification |
| [`adr/`](docs/adr/) | Architecture decision records |

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

## License

MIT
