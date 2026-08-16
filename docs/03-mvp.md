# MVP Definition

## The structural decision

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

## M0 — The Proof

**Objective:** a cost-vs-decision-quality frontier comparing three strategies on
identical data, with counterfactual regret at every point.

| Component | Role |
|---|---|
| `evalgen/` | Latent-truth generator + provider observation model. Seeded, versioned. |
| `providers/simulated.py` | Coverage, correlated noise, latency, price — behind the shared Protocol |
| `scoring/engine.py` | Deterministic scorer. Pure: `evidence → (score, breakdown)` |
| `confidence/` | Isotonic/Platt calibration, conformal threshold τ, reliability diagram, ECE |
| `policy/` | `FullEnrichment`, `TunedWaterfall`, `AdaptiveVoI` behind one interface |
| `bench/` | Threshold sweep → frontier, counterfactual regret, stratified breakdown |

**No LLM in M0.** Signal extraction is stubbed deterministically. Introducing
nondeterminism into the experiment that establishes the baseline result is a
methodological error. The LLM's contribution is measured separately in M1, as a
delta against a known-good deterministic baseline. This is also why M0 costs $0
and reruns identically in CI forever.

### Pre-registered success criteria

Fixed *before* any data exists, so the result cannot be rationalised afterwards.

| Outcome | Verdict |
|---|---|
| Matches full-enrichment decision agreement (within 1pp) at ≥40% lower cost | Thesis holds strongly |
| Beats **tuned waterfall** by ≥20% cost at matched decision agreement | **Thesis holds** — the criterion that actually matters |
| Beats full-enrichment but *not* tuned waterfall | **Thesis as stated is falsified.** Honest finding: waterfall heuristics already capture most available gain. Narrative pivots to calibration + escalation control, which stand independently. |
| Cheaper but loses >2pp decision agreement | Weak result — report the frontier, claim no win |

These are judgment thresholds, not predictions. Row 3 is a genuine possible
outcome and will be reported as such.

**Effort: ~40% of total project time.** Smallest by line count, largest by
intellectual weight.

---

## M1 — The System

| Component | Scope |
|---|---|
| Postgres schema + migrations | Full schema, Supabase-hosted |
| Evidence store w/ field TTL | *This is the cache* — no separate cache layer |
| Identity resolution | Deterministic normalisation + exact-domain match |
| Job queue | `SKIP LOCKED`, backoff, dead-letter |
| State machine | Pure `decide_next_action()` + transactional transitions |
| Ingestion API | FastAPI, one `POST /leads` |
| Cost/latency ledger | `provider_calls`, `model_calls`, SQL metric views |
| LLM signal extraction | DeepSeek, **one narrow task**, measured as delta vs. M0 |
| Human approval | API + CLI recording to `human_reviews`; Slack is a later adapter |
| Observability | OTel spans; Langfuse for LLM traces only |
| CI | Deterministic benchmark + tests, zero API spend |
| n8n | Two thin workflows: webhook ingest, CRM sync-out. **Added last.** |

---

## Build / Mock / Defer

**Mocked — with a real interface behind it**

| Mocked | Why |
|---|---|
| Enrichment providers | Makes the benchmark free and reproducible — a project property, not a shortcut ([ADR 0003](adr/0003-simulator-first-providers.md)) |
| Slack | Approval *logic* is the substance; transport is an adapter |
| CRM | A `crm_sync` sink proves the write path, idempotency, permission gating |
| LLM in M0 | Methodological necessity |

**Deferred — each with its trigger**

| Deferred | Revisit when |
|---|---|
| Email outreach | Out of scope for this project |
| RAG / pgvector | Only justified for outreach personalisation → follows the above |
| LangGraph research subgraph | `hard` band shows leads needing open-ended tool selection |
| Temporal | >1k jobs/sec, or a genuine second service |
| Splink probabilistic matching | The ambiguous-identity subset shows exact-domain match failing materially |
| Provider circuit breakers | Real providers only — simulated ones don't degrade |
| Multi-tenant RLS | Documented as an appendix, not built |
| MCP server | Portfolio garnish; zero thesis value |

**Cut entirely** — these would be resume decoration:

- **Multiple "agents."** One deterministic controller. Calling its steps "agents"
  is naming, not architecture.
- **A React dashboard.** SQL views + a static report answer every metric question.
- **Vector search in the core loop.** No genuine use case survived scrutiny.
- **Three-vendor model routing.** A cheap→strong cascade is justified only if
  escalation frequency is actually measured; wiring three vendors to demo
  "routing" is not.

---

## Sequencing

```
1.  evalgen + dataset validity gate
2.  simulator + provider Protocol
3.  deterministic scorer
4.  confidence model + calibration + τ
5.  three strategies + benchmark harness
    ═══════ M0 GATE — read the result, decide honestly ═══════
6.  schema + migrations + evidence store/TTL
7.  identity resolution + company-level cache sharing
8.  job queue + state machine + retries/DLQ
9.  FastAPI ingest + cost ledger + OTel
10. LLM signal extraction (delta vs. M0)
11. human approval path
12. n8n edge workflows
13. CI hardening
```

Steps 1–5 are the project. Steps 6–13 make it a platform. Both matter — but only
the first five determine whether the README's claim is true.

## Definition of done

`docker compose up` runs the service. `make bench` reproduces the full frontier
offline with zero API keys and byte-identical results. The README's headline
claim links directly to the command that produces it.
