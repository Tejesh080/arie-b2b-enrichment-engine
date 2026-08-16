# Adaptive Revenue Intelligence Engine

**Cost-aware active feature acquisition for lead qualification.**

Most lead-enrichment pipelines call every data provider for every lead, then ask an
LLM for a score. This system instead treats enrichment as a sequential decision
problem: *given what I already know, is the next API call worth its price?*

> **Engineering thesis**
>
> Lead qualification is an **active feature acquisition** problem, not a
> data-completeness problem. Enrichment should stop when the **decision**
> stabilises — not when the **record** fills up. A calibrated uncertainty model
> plus an explicit expected-value-of-information stopping rule should dominate
> both fixed pipelines and coverage-driven waterfalls on the cost / decision-quality
> frontier.

> ### ⚠️ Status: in development — the thesis above is **not yet verified**
>
> This project pre-registers its success criteria (see
> [`docs/03-mvp.md`](docs/03-mvp.md)) and will publish the measured frontier
> whether or not it supports the thesis. **No benchmark numbers appear in this
> README until `make bench` produces them.** If adaptive enrichment fails to beat
> a *tuned* waterfall baseline, that result gets reported here as prominently as
> a positive one would.

---

## Why this is not another "AI lead scoring" demo

| Common approach | This system |
|---|---|
| Call every provider, every lead | Call a provider only when its expected information value exceeds its cost |
| LLM emits `score: 87, confidence: 0.82` | Deterministic scorer; confidence **calibrated** against held-out data with a published reliability diagram |
| Waterfall stops when a field gets filled | Stops when the **decision** stops changing |
| Thresholds live in a prompt and drift | Thresholds derived from a calibration split via a conformal procedure |
| "It saved 60% of API calls" | Counterfactual regret: *what fraction of decisions changed, and how many got worse* |
| Re-researches the same company per contact | Company-level evidence cache with per-field TTLs |

The distinction that drives everything: **coverage-driven stopping vs.
decision-driven stopping.** A field can stay empty forever if filling it cannot
flip the routing decision.

---

## The decision rule

```
EVoI(provider) = P(its evidence flips the decision) × business_value(lead)
                 − cost_usd(provider)
                 − latency_penalty(provider, sla)
```

Enrichment continues while `max EVoI > 0`, and stops on whichever comes first:
decision confidence clears the calibrated threshold, no candidate has positive
EVoI, or the hard per-lead budget cap trips.

Cached evidence enters this calculation at **zero cost but staleness-discounted
confidence** — which is what lets free-but-stale facts compete honestly against a
fresh paid call, rather than being either blindly trusted or ignored.

---

## Architecture

```
   n8n (thin edge: ingest webhook, CRM sync-out)
                     │
                     ▼
        Ingestion API (FastAPI) ──► Postgres / Supabase
                                      │  leads · lead_events · jobs
                                      │  evidence (= the cache)
                                      │  voi_decisions · provider_calls
                                      ▼
                          Worker (SKIP LOCKED queue)
                                      │
             ┌────────────────────────┼────────────────────────┐
             ▼                        ▼                        ▼
    Deterministic scorer    EVoI policy controller     Calibrated confidence
                                      │
                                      ▼
                          Provider adapter layer
                    (SimulatedProvider │ real APIs — same Protocol)
                                      │
                       confidence ≥ τ ?  ──► auto-route
                                      └──► Slack approval ──► human_reviews
```

Full design and the reasoning behind each decision:
[`docs/02-architecture.md`](docs/02-architecture.md).

**Deliberately rejected**, with reasons in [`docs/adr/`](docs/adr/):
LangGraph (the loop is a calculation, not agentic reasoning), Temporal
(single-service scale), Redis (Postgres gives transactional consistency with lead
state), RAG/pgvector (no genuine use case survived scrutiny), "multiple agents"
(naming, not architecture).

---

## Quick start

```bash
pip install -e ".[dev,service]"
make dataset          # generate the seeded evaluation set
make validate-dataset # CI gate: assert the dataset is non-trivial
make bench            # full benchmark — no API keys, no network, deterministic
```

`make bench` runs entirely offline against the provider simulator. Anyone
cloning this repo reproduces the exact numbers in `docs/06-benchmark.md`,
byte-for-byte — that reproducibility is a design goal, not a convenience.

---

## Evaluation design

The benchmark compares three strategies on identical data:

1. **Full enrichment** — call every provider (the naive baseline)
2. **Tuned waterfall** — cheapest-first ordering with ICP pre-gating (*the honest
   industry baseline*, and the one that actually matters)
3. **Adaptive EVoI** — this system

Each synthetic lead carries a **latent truth vector** never visible to the policy;
providers return noisy, incomplete *observations* of it. Provider misses are
**correlated** via a shared per-lead obscurity draw, because real providers fail
together on the same messy accounts. The oracle decision is computed from latent
truth alone.

The dataset validates itself: a CI test asserts that a cheapest-provider-only
baseline scores materially below the full-information ceiling. **If the dataset
is too easy, it is rejected and regenerated** — a benchmark you can pass without
trying proves nothing.

Details: [`docs/04-eval-dataset.md`](docs/04-eval-dataset.md).

---

## Repository layout

```
src/arie/
  core/          domain types — pure, no I/O
  evalgen/       latent-truth dataset generator
  providers/     Protocol + simulated + (later) real adapters
  scoring/       deterministic rule-based scorer
  confidence/    calibration, conformal threshold selection, ECE
  policy/        the three strategies, incl. the EVoI controller
  evidence/      evidence store with per-field TTL  (= the cache)
  identity/      domain/name normalisation, entity resolution
  jobs/          SKIP LOCKED queue, retries, dead-letter
  statemachine/  transitions — pure decide_next_action()
  llm/           signal extraction (M1 only, one narrow task)
  api/           FastAPI ingestion
  approval/      human-in-the-loop
  observability/ OpenTelemetry wiring
bench/           benchmark harness + report generation
migrations/      numbered SQL
tests/           unit (no network) + integration (needs DB)
docs/            research, architecture, ADRs, results
workflows/n8n/   exported n8n workflow JSON
```

---

## Documentation

| Doc | Contents |
|---|---|
| [`docs/01-research.md`](docs/01-research.md) | Competitive analysis: 10 comparable projects, what to borrow, what to avoid |
| [`docs/02-architecture.md`](docs/02-architecture.md) | Full system design and trade-offs |
| [`docs/03-mvp.md`](docs/03-mvp.md) | Scope, pre-registered success criteria, build/mock/defer |
| [`docs/04-eval-dataset.md`](docs/04-eval-dataset.md) | Dataset generation and validity testing |
| [`docs/ASSUMPTIONS.md`](docs/ASSUMPTIONS.md) | Every simulator parameter and its justification |
| [`docs/adr/`](docs/adr/) | Architecture decision records |

---

## Prior art

This project builds on established work rather than claiming novelty it doesn't have:

- **Active feature acquisition** — [A Survey on AFA Strategies](https://arxiv.org/abs/2502.11067)
  (POMDP formulation), [AFABench](https://arxiv.org/html/2508.14734) (accuracy-cost curves)
- **Cost-aware stopping** — [Scores Are Not Decisions](https://arxiv.org/abs/2607.27083)
  (ranking ≠ how many to buy; Pandora's-box reservation values)
- **Model cascades** — [FrugalGPT](https://arxiv.org/abs/2305.05176) (gains come from difficulty skew)
- **Router evaluation** — [RouteLLM](https://github.com/lm-sys/RouteLLM) (publish a frontier, not a point)
- **Entity resolution** — [Splink](https://github.com/moj-analytical-services/splink) (Fellegi–Sunter)

The contribution here is not the theory — it is applying it to a domain where the
prevailing practice is coverage heuristics, and **measuring whether it actually helps**.

## License

MIT
