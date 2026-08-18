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
| adaptive EVoI (single un-scaled variant) | 0.8093 | 0.2906 | 2.19 | 0.786 |

**41.6% cheaper than a tuned waterfall, at 2.3pp lower decision agreement**, and
cheapest on total cost at every human-review price tested. Standard deviation on
that saving is 11.0pp — the variance is large relative to the effect, and
[the results doc](docs/05-results.md) reports it per seed rather than burying it,
along with exactly what "single un-scaled variant" means for the EVoI row above.

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
   n8n (thin edge: ingest webhook, outcome sync)          [M1, BUILT]
                     |
                     v
        Ingestion API (FastAPI) --> Postgres / Supabase   [M1, BUILT]
          POST /leads: identity resolution + lead +
          first job, in ONE transaction
                                      |
                          jobs.trace_context carries the
                          W3C trace across the process boundary
                                      v
                          Worker (SKIP LOCKED queue)      [M1, BUILT — runs the
                                      |                    policy, simulated mode]
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

   arie.llm (DeepSeek) -- buying-signal extraction from free text  [M1, BUILT,
   Pydantic-validated, ledgered, traced -- not called by the worker  standalone]
   above yet. Delta vs. a deterministic baseline: bench/llm_signal_eval.py
```

M0 — the intelligence engine and its benchmark — is complete. M1 is built
through the worker: schema and migrations, evidence store, identity
resolution, the job queue and transactional state machine, the ingestion API,
cost ledger, end-to-end tracing, LLM signal extraction, a human review API,
the n8n edge workflows — and the worker handler that composes them.
`CalibratedBoundsPolicy` now runs in production (over the simulated provider
registry — no real vendor adapter exists yet, so only identities from the
frozen eval corpus can be enriched), buying evidence into the durable store,
ledgering every call, and escalating low-confidence decisions through
`request_review`. `arie.llm` alone remains uncalled by the worker — ingestion
carries no free-text field for it to extract from yet. See
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

## 60-second local demo

The fastest way to see ARIE decide, escalate, and honor a human override —
no PowerShell/API knowledge, no raw JSON. Requires
[Docker](https://www.docker.com/products/docker-desktop/) installed and running.

```powershell
git clone https://github.com/Tejesh080/Adaptive-Revenue-Intelligence-Engine.git
cd Adaptive-Revenue-Intelligence-Engine
.\scripts\demo.ps1
```

```text
Demo complete.
Report: demo-output\arie-demo.html
```

Open that file directly in a browser — no server required. It starts the
required Docker services if they aren't already running (`db`, `migrate`,
`api`, `worker` — never `n8n`), submits a handful of deterministic corpus
leads, and renders every number in the report from a live
[`GET /leads/{lead_id}/receipt`](#decision-receipt) response: an autonomous
decision, a human escalation with the override preserved next to the original
recommendation, company-level evidence reuse, and proof that redelivering the
same request doesn't create duplicate work. The run is non-destructive by
default; `.\scripts\demo.ps1 -Fresh` wipes local Docker volumes first if you
want a clean-slate run (this deletes local ARIE demo data). See
[`scripts/demo/`](scripts/demo/) for the Python runner the script wraps.

---

## n8n edge workflows

Two thin workflows, importable as-is, plus a local helper. No business logic
lives in n8n — validation, identity resolution, scoring, policy, and review
outcomes are all decided by ARIE; n8n only maps field shapes and relays
ARIE's response.

```
external webhook --> [has email?] --> ARIE POST /leads --> relay ARIE's response
                                                             (lead_id, job_id, status, ...)

{lead_id} webhook --> ARIE GET /leads/{id} --> [found? finalized?] --> CRM-shaped
                                                                        payload --> sink
```

| File | Role |
|---|---|
| [`workflows/n8n/lead-ingestion.json`](workflows/n8n/lead-ingestion.json) | `POST /webhook/lead-ingest` → ARIE `POST /leads`. Preserves `source`/`external_ref` untouched so ARIE's own idempotency handles redelivery — n8n never invents or drops them. |
| [`workflows/n8n/outcome-sync.json`](workflows/n8n/outcome-sync.json) | `POST /webhook/outcome-sync` with `{"lead_id": "..."}` → ARIE `GET /leads/{id}` → CRM-shaped payload → sink, once the lead's status is one ARIE already treats as finalized. |
| [`workflows/n8n/mock-crm-sink.json`](workflows/n8n/mock-crm-sink.json) | Not one of the two edge workflows — a minimal local stand-in so `outcome-sync` has somewhere real to `POST` to. Swap the URL on `outcome-sync`'s "POST Mock CRM Sink" node for a real CRM endpoint later; nothing else changes. |

`outcome-sync` is receive-triggered rather than polling ARIE for a list of
finalized leads, because no such endpoint exists yet and building
discovery/retry logic into n8n to fake one would be exactly the
backend-duplicating logic this step is scoped to avoid. Call it once you
already know a decision finalized; see the sticky note in the workflow for
the schedule-trigger swap if ARIE ever adds one.

### Run n8n locally

Not part of the default stack — opt in with Compose's `--profile` flag:

```bash
docker compose --profile n8n up -d n8n
```

Open [http://localhost:5678](http://localhost:5678) (first run asks you to
create a local owner account; nothing leaves your machine). The workflow JSON
calls the `api` and `n8n` services by their Docker Compose network names
directly (`http://api:8000/...`, `http://n8n:5678/webhook/mock-crm-sink`) —
not via a container env var, because n8n blocks `$env` access inside node
expressions by default and this repo doesn't override that (found during
manual end-to-end verification, not assumed). Nothing to configure; import
and go.

**Local n8n vs. a hosted n8n account.** This Docker service is a
reproducible dev/demo environment shipped *with the repo* — the same
philosophy as everything else here (offline-first, zero required
credentials, runs the same way on anyone's machine). A separately hosted
n8n (cloud or otherwise) is a *later*, deliberately deferred step: it only
becomes relevant once ARIE has a publicly reachable deployed API for it to
call, which nothing in M1 sets up yet. Nothing in this repo connects to or
deploys against any hosted n8n account, and this local service is not meant
to be removed once one exists — the two serve different purposes
(reproducible local demo vs. a real integration target).

### Import and try it

In the n8n UI: **Workflows → Import from File** for each of the three JSON
files above, then open each one and click **Activate** (n8n serves an
inactive workflow's webhook only while its editor tab is open and listening).

This exact sequence has been run end to end against a real n8n instance:
ingestion returned 201, a worker processed the job to `AUTO_ROUTED`,
outcome-sync reached the mock sink and returned `synced: true`, and
redelivering the same ingestion payload returned the same lead with
`created: false`/`job_created: false`. The demo identity below is a real
person in the frozen eval corpus (seed 42),
which matters: the workers run in `PROVIDER_MODE=simulated`, where the only
data source is the corpus's frozen observations — an identity outside it has
nothing to enrich from, and its job retries then dead-letters with a message
saying exactly that.

```bash
curl -X POST http://localhost:5678/webhook/lead-ingest \
  -H "Content-Type: application/json" \
  -d '{"source": "landing-page", "email": "nadia.delacroix@lumen500.com", "company_domain": "lumen500.com", "full_name": "Nadia Delacroix", "external_ref": "demo-1"}'
# -> {"lead_id": "...", "status": "NEW", "created": true, "job_id": "...", ...}

# Within a few seconds a worker claims the job, runs the policy, and the lead
# lands in AUTO_ROUTED (7 provider calls, ~$0.41 simulated spend). Then:
curl -X POST http://localhost:5678/webhook/outcome-sync \
  -H "Content-Type: application/json" \
  -d '{"lead_id": "<the lead_id from above>"}'
# -> {"synced": true, "sink_status": 200, ...} — the mock sink's execution log
#    in n8n shows the CRM-shaped payload it received. Calling outcome-sync
#    before the worker has finished returns {"synced": false, "reason": "lead
#    not finalized"} — poll GET /leads/{id} or just retry a moment later.
```

Redelivering the same ingestion payload is safe — that's ARIE's `(source,
external_ref)` uniqueness doing the work, not anything n8n does. And on a
completely fresh clone, `docker compose --profile n8n up -d` needs no manual
migration step: a one-shot `migrate` service applies `migrations/` via the
canonical runner before the API or workers are allowed to start.

---

## Decision Receipt

`GET /leads/{lead_id}/receipt` answers, from persisted state only, *why did
ARIE stop spending money and make this decision?* — the recommendation, the
score and its bounds, why the acquisition loop stopped, what was spent, which
providers were called (and which weren't), and — separately — whether a human
overrode the recommendation and what actually happened.

```bash
curl http://localhost:8000/leads/<lead_id>/receipt | python3 -m json.tool
```

```json
{
  "receipt_version": "1",
  "status": "decided",
  "lead_status": "AUTO_ROUTED",
  "decision": {
    "recommended_action": "reject",
    "autonomous": false,
    "final_status": "AUTO_ROUTED",
    "human_override": true
  },
  "score": {
    "value": 51.0,
    "bounds": { "lower": 0.0, "upper": 81.0 },
    "confidence": 0.248,
    "tau": 0.8038
  },
  "stopping": {
    "reason_code": "all_providers_called",
    "explanation": "Every available data provider was called; there was no further evidence left to purchase."
  },
  "cost": { "provider_cost_usd": "0.34", "total_cost_usd": "0.34", "budget_usd_cap": "1.5" },
  "human_review": {
    "original_decision": "reject", "action": "approve", "final_decision": "auto_route"
  }
}
```

That example is real, not illustrative — the same `nadia.haddad@cobalt500.com`
corpus identity below, escalated (confidence 0.248 against τ 0.804) and then
approved by a reviewer. `recommended_action` and the score/stopping snapshot
stay exactly as they were the moment the policy decided; `final_status` and
`human_review` are read live, which is how the receipt shows a human override
without rewriting what ARIE actually recommended.

Three response shapes, distinguished by `status`: `"decided"` (a
`decision_receipts` row exists — see below), `"pending"` (still mid-pipeline,
`decision`/`score`/`stopping` are `null`), and `"processing_failed"` (dead-
lettered before ever deciding). Unknown `lead_id` is a 404, not a shape.

**Persistence model.** Most of the receipt is read live from tables that were
already durable and lead-scoped (`provider_calls`, `human_reviews`,
`lead_events`) — they don't change after being written, so reading them now is
reading history. A few facts are neither durable nor lead-scoped in the
existing schema — score bounds, the policy/scorer/calibration identifiers in
effect, and which evidence source won each field — because `evidence` (M1's
cache table) is keyed by company/person and mutates as later leads reuse it.
Migration `0008_decision_receipts.sql` adds one small table,
`decision_receipts`, written once inside `compute_score`'s own work
transaction, that freezes exactly those facts at decision time. Full reasoning
in [`docs/06-m1-handoff.md`](docs/06-m1-handoff.md#post-m1-p1--decision-receipt).

**Deliberately deferred:** `estimated_cost_avoided_usd` (no defensible
per-lead counterfactual baseline yet — actual spend is reported instead), a
per-provider "why was this one skipped" verdict (the production policy
doesn't evaluate skipped providers individually; `providers.not_called` is an
honest set difference against the catalogue, not a claim about reasoning that
didn't happen), and any UI.

---

## Policy Lab

```powershell
.\scripts\policy-lab.ps1
```

```text
Policy Lab generated.
Report: demo-output\policy-lab.html
```

A static, offline report that turns the frozen M0 benchmark
([`docs/05-results.md`](docs/05-results.md)) into a Pareto chart of API cost
per lead versus synthetic-oracle agreement, computes which evaluated policies
are actually non-dominated from the data (never a hardcoded list), and walks
through why `CalibratedBoundsPolicy` shipped to production despite scoring
lower on raw agreement than full enrichment or the tuned waterfall — a
measured trade-off, not the most sophisticated policy winning by default. It
reads the already-frozen `bench/out/multi_seed.json` and never re-runs the
benchmark; pass `-Regenerate` if that artifact doesn't exist yet (a fresh
clone won't have it — `bench/out/` is gitignored). See
[`docs/06-m1-handoff.md`](docs/06-m1-handoff.md#post-m1-p3--policy-lab) for
what it does and does not claim.

---

## Real provider

P5 wires ARIE's provider abstraction to one real external service —
**Abstract API's Company Enrichment endpoint** — proving the same
`EnrichmentProvider` Protocol the simulator implements can also front a live
vendor, without touching the frozen M0 catalogue or benchmark.

- **Why this one.** A plain `GET` with `api_key`/`domain` query params, a free
  tier (100 requests/month, no card), and a response whose field names —
  `employee_count`, `industry` — match ARIE's own `SCORED_FIELDS` exactly. No
  scraping, no browser automation, no new normalization layer.
- **Env:** `ABSTRACT_COMPANY_API_KEY` (see [`.env.example`](.env.example)).
  `PROVIDER_MODE=live` refuses to build a worker without it — no silent
  fallback to the simulator.
- **What it populates:** `employee_count` and `industry` for the company
  entity of *any* ingested lead (unlike simulated mode, live mode has no
  frozen-corpus restriction). Every other `SCORED_FIELDS` entry
  (`buying_intent`, `recent_trigger_event`, `disqualifying_flag`,
  `title_seniority`, `title_function`) stays unknown in live mode — there is
  no live source for them yet.
- **Cost model:** an *estimated* unit cost
  (`ABSTRACT_COMPANY_COST_USD_PER_CALL`, derived from Abstract's list price,
  not a per-call figure the provider reports), recorded through the same
  `provider_calls` ledger as every simulated call.
- **Cache reuse, not just a real API call.** A second lead at an
  already-enriched company is served from the durable `evidence` table at
  zero cost — recorded as a zero-cost cache hit, never a silent skip. That is
  the actual interesting claim: ARIE can decide it does *not* need to spend
  real money, not merely that it can make one real HTTP call.
- **Live verification:**

  ```bash
  python scripts/live_provider_smoke.py --domain github.com --confirm-live-spend
  ```

  Calls the real adapter once and prints the normalized result. Pass
  `--api-base-url http://localhost:8000` (with a `PROVIDER_MODE=live` worker
  already running) to also drive a real lead through ingestion and print its
  Decision Receipt — a second real call, which the script says out loud
  before making it.

**Deliberately not built:** a second provider, a provider registry/marketplace,
retries beyond one bounded attempt, or any claim that this adapter's accuracy
matches the simulator's declared assumptions — that comparison is exactly what
ADR 0003 defers to "once a real provider is wired," and remains unmeasured
here. See [`docs/06-m1-handoff.md`](docs/06-m1-handoff.md#post-m1-p5--one-real-provider--shadow-mode)
for the full account, including live-verification status.

---

## Shadow mode

ARIE can observe a live enrichment workflow and record what it would have
done — evidence acquired, cost spent, confidence reached, decision reached —
**without controlling the downstream routing outcome.** No autonomous action,
no human review opened, nothing an outcome-sync consumer would treat as
finalized.

Requested per lead, not with a server-wide switch — `POST /leads` takes an
optional `"mode": "shadow"` (default `"normal"`, and normal mode's behaviour
is completely unchanged):

```bash
curl -X POST http://localhost:8000/leads \
  -H "Content-Type: application/json" \
  -d '{"source": "shadow-demo", "email": "nadia.haddad@cobalt500.com", "external_ref": "shadow-1", "mode": "shadow"}'
```

The lead still runs the full acquisition loop — real evidence, real cost if
`PROVIDER_MODE=live` is enabled, a real `decision_receipts` row — but instead
of branching into `AUTO_ROUTED`/`AWAITING_HUMAN`/`REJECT`, it lands on a
dedicated terminal, `SHADOW_EVALUATED`, and never calls `request_review`. The
receipt's top-level `"shadow": true` field makes this explicit, and
`v_pipeline_metrics`/`v_escalation_rate` exclude shadow leads so a shadow run
never inflates real business metrics.

**"Shadow" does not mean "free."** If `PROVIDER_MODE=live` is on, a shadow
lead's evidence acquisition can still make a real, billed provider call —
shadow mode only suppresses the *authoritative outcome*, not the underlying
work. And no counterfactual savings are claimed here: P5 does not compare
shadow-mode spend against a baseline enrichment workflow's actual cost unless
one is explicitly supplied, and none is in this repo.

See [`docs/06-m1-handoff.md`](docs/06-m1-handoff.md#post-m1-p5--one-real-provider--shadow-mode)
for exactly what is and isn't suppressed, and why.

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
  providers/     Protocol + simulated + live_abstract.py (post-M1 P5, one real adapter)
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
  llm/           DeepSeek buying-signal extraction - one narrow task (M1)
    schema.py      ExtractedSignal - the entire LLM/state boundary
    baseline.py     the deterministic comparison point, zero cost
    deepseek.py      client + retries + ledger + tracing, no tools registered
    eval.py           small synthetic corpus + delta scoring, separate from M0
  approval/      human review workflow - request/decide around human_reviews (M1)
bench/           harness, metrics, cost model, seed sweep, llm_signal_eval.py
migrations/      numbered SQL (M1) - source of truth, applied via scripts/migrate.py
supabase/        migrations/ mirror (generated) + config, for GitHub Branching PR previews
scripts/         migrate.py, sync_supabase_migrations.py
workflows/n8n/   thin transport-edge workflows (M1) - importable JSON, no business logic
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
| [`07-deployment.md`](docs/07-deployment.md) | Migrations, config, health checks, shutdown — deploying the API and worker |
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
