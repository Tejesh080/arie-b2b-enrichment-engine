# M1 Handoff

Written to be read cold, in a fresh session, with no memory of how M0 went.

---

## Where things stand

**M0 is complete.** The intelligence engine exists, is benchmarked, and its
result is published — including the part that failed. Everything runs offline
and deterministically; no credentials, no network.

**M1 Steps 6–8 are complete and verified against the live production Supabase
database** (schema/migrations/evidence store, identity resolution, job
queue/state machine). Step 9 (FastAPI ingest) has not started — see
"Suggested order" below for exactly what's built versus deferred within each
completed step; none of them wire up real provider adapters or
`CalibratedBoundsPolicy` yet, on purpose.

Read [`05-results.md`](05-results.md) before writing code. The single most
important thing to absorb: **the sophisticated policy lost to the simple one.**
Do not resurrect EVoI. Do not tune it. It is retained deliberately as a negative
result and [ADR 0004](adr/0004-evoi-is-a-negative-result.md) explains why.

---

## What to build on

The production policy is `arie.policy.production.CalibratedBoundsPolicy`.

```python
from arie.confidence.model import fit_confidence_model
from arie.policy.production import CalibratedBoundsPolicy

model = fit_confidence_model(calibration_leads, target_error_rate=0.10)
policy = CalibratedBoundsPolicy(model=model)
outcome = policy.run(lead, ctx)   # -> PolicyOutcome
```

Its dependencies, in the order M1 will need to wire them:

| Module | Role in M1 |
|---|---|
| `arie.scoring.rules` | ICP rules. Shared by oracle and runtime — do not fork. |
| `arie.scoring.engine` | Score bounds, evidence signals. Pure, no I/O. |
| `arie.confidence.model` | Fits on calibration data; produces τ. Refit, never pickle. |
| `arie.providers.base` | `EnrichmentProvider` Protocol — real adapters implement this. |
| `arie.providers.simulated` | Replays frozen observations. Keep for tests and CI. |
| `arie.policy.base` | `RunContext`, `EvidenceCache`, `CallLedger`. |

Everything in `src/arie/scoring/` and `src/arie/core/` is pure — no database, no
network, no framework imports. That is deliberate and worth preserving: it is
what lets the benchmark exercise the same code that runs in production rather
than a reimplementation of it.

---

## The five things most likely to be got wrong

**1. `is_settled` does not mean "certainly correct".**
Bounds are computed from *observed* facts, which are noisy. A settled decision
can still disagree with the oracle when a provider lied. Settled means "nothing
left worth buying"; confidence means "this is probably right". Wiring the
autonomy gate to `is_settled` would hand out unearned autonomy. Use
`confidence >= model.tau`.

**2. The confidence model must be refit, not pickled.**
It trains in seconds. A checked-in artefact drifts out of sync with the code that
produced it, silently. `fit_confidence_model` raises if handed a test-split lead
— keep that guard when you wire real data.

**3. τ is unstable and will move.**
Across seeds it ranges 0.69–0.93. It is a function of the calibration data, so it
will change every time you refit on new leads. Do not hardcode it, do not surface
it as a config constant, and do log it per fit. If τ ever comes back as `1.01`
(`REJECT_ALL_THRESHOLD`), the model could not certify any operating point and the
correct behaviour is to escalate everything — not to lower the bar.

**4. The 10% error budget is a policy choice, not a law.**
5% is not achievable on this model — see
`test_five_percent_budget_is_not_achievable`. If a stakeholder asks for 5%, the
honest answer is "then we automate nothing", not "we'll tune it".

**5. Cache hits must be recorded, not skipped.**
`CallLedger.record(..., cache_hit=True)` logs a zero-cost call. Dropping them
makes cache hit rate unmeasurable, and that metric is the justification for the
company-level evidence store.

---

## Schema already written

`migrations/0001_init.sql` through `0004_job_queue_lease_index.sql` exist and
have been run against the live production Supabase database (Steps 6–8) — one
real bug was found and fixed doing so: `evidence.expires_at` couldn't be a
`GENERATED` column because `timestamptz + interval` is STABLE, not IMMUTABLE
(see the migration's own comment). `migrations/` is the source of truth and
the only path that touches production, via `scripts/migrate.py`.
`supabase/migrations/` is a generated mirror kept in sync by
`scripts/sync_supabase_migrations.py` and CI, added so Supabase's GitHub
Branching provisions PR preview databases with the same schema — see
[ADR 0005](adr/0005-migration-source-of-truth.md) for why both directories
exist and why production deploys still go through neither Branching nor the
Supabase CLI.

Design notes worth honouring:

- `lead_events` is append-only and authoritative. `leads.status` is a
  materialised convenience that can be rebuilt by replay.
- **`evidence` IS the cache.** There is no separate cache subsystem to keep in
  sync — a hit is a row whose `expires_at` is still in the future.
- `voi_decisions` stores rejected candidates too. With EVoI out of production
  this table is either unused or repurposed to record *why enrichment stopped*
  (`settled` / `confidence_reached`). Repurposing is the better option; the audit
  trail is worth keeping.

The `EvidenceCache` in `arie.policy.base` is an in-memory stand-in for benchmark
runs. M1 replaces it with the `evidence` table plus TTL, behind the same
interface.

---

## Suggested order

1. ✅ **Supabase + migrations.** `0001`/`0002` ran for real. Pooled connection
   string for workers, direct for migrations — *except* the plain direct-connect
   host turned out to be IPv6-only and unreachable from the dev machine used so
   far; the Session Pooler (port 5432) stood in for both. See "Environment"
   below before assuming `DATABASE_DIRECT_URL` will just work.
2. ✅ **Evidence store with TTL**, replacing `EvidenceCache` for real DB use —
   `arie.evidence.store.PostgresEvidenceStore`. Note it is *not* wired into
   `RunContext`/the policy loop; that's still ahead, once there's a worker
   handler that actually needs it (see step 5 below).
3. ✅ **Identity resolution** — `arie.identity`, deterministic domain/email
   normalisation, exact-match only. Measured live against the dataset's
   ambiguous-identity subset: **0% failure, 0 false merges**. Splink is not
   currently justified — do not add it on spec.
4. ✅ **Job queue** — `arie.jobs`, `SKIP LOCKED`, exponential backoff + full
   jitter, dead-letter (including lease-expiry reclaim counting toward the
   attempt budget, so a hard-crashed worker's job still terminates). Claiming
   and processing are deliberately two transactions (claim commits fast so
   `SKIP LOCKED` stays cheap for other workers; the work transaction is where
   completion and the lead's state transition commit together).
5. **Partially done — state machine mechanism built, real handlers not wired.**
   `arie.statemachine` gives a pure `next_status`/`job_type_for` graph
   (`NEW → SCORING → FETCHING_EVIDENCE → INTEGRATING → DECISION → …`) and
   `apply_transition` (optimistic concurrency on `leads.version`, atomic with
   job completion via `arie.jobs.worker.run_worker_cycle`). What's *not* done:
   the graph is a linear scaffold, not `CalibratedBoundsPolicy`'s real
   score/fetch-evidence loop (that loop reads evidence *content*, not just a
   status label, and needs real provider adapters this scaffold has no
   dependency on). `arie.jobs.worker.main()` runs today with zero handlers
   registered — wiring real handlers (scoring, evidence fetching, the policy
   itself) behind the job types `next_status` already names is the next real
   step, once step 6 gives a way to create leads with something to work on.
6. **FastAPI ingest**, one `POST /leads`. Not started. This is also where
   identity resolution actually gets called for the first time in the request
   path — `leads` currently only carries resolved `person_id`/`company_id`,
   not raw inbound fields, so ingestion is what decides how raw lead data
   becomes those IDs.
7. **Cost ledger + metric views** — `0002_metrics_views.sql` is already written.
8. **LLM signal extraction** (DeepSeek), one narrow task: buying signals from
   free text. Measure it as a delta against the deterministic baseline. If it
   does not move decision agreement, cut it — that is the precedent this project
   has already set once.
9. **Human approval path**, then **n8n edge workflows** last.

---

## Do not

- Resurrect or tune EVoI.
- Add RAG or pgvector. No use case survived scrutiny.
- Add LangGraph. The loop is a calculation, not agentic reasoning
  ([ADR 0001](adr/0001-no-langgraph-for-core-loop.md)).
- Add Temporal or Redis at this scale ([ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md)).
- Call anything an "agent" that is a deterministic function.
- Replace the simulator. It is what keeps CI free and the benchmark reproducible;
  real adapters go *alongside* it behind the same Protocol.

---

## Guard rails to keep green

```bash
make lint && make type && make test     # ruff, mypy --strict, unit tests (no DB, no network)
make check-supabase-migrations          # supabase/migrations/ must match migrations/ — see ADR 0005
make validate-dataset                   # dataset must stay non-trivial
python -m bench.multi_seed              # ~10 min; run before claiming an improvement
```

`make test` runs offline; it does not exercise anything in `arie.evidence`,
`arie.identity`, or `arie.jobs`/`arie.statemachine` against a real database —
that needs `make test-all` with `DATABASE_URL`/`DATABASE_DIRECT_URL` set,
which is opt-in specifically because it writes to whatever database those
point at (see `tests/integration/conftest.py`'s own warning). Don't infer
"the M1 pieces are covered" from a green `make test` alone.

CI runs the benchmark on every push with zero credentials. If a change to
scoring, confidence, or the policy moves the numbers, that is a result and
belongs in `05-results.md` — not a regression to be silenced.

The dataset content hash is in `data/eval/manifest.json`. If it changes
unexpectedly, something in the generator changed and every prior number is no
longer comparable.

---

## Environment

- Supabase project `lobsbijgazlpurymxynd` (region `ap-northeast-2`), MCP server
  registered in `.mcp.json` at project scope. OAuth must be completed
  interactively (`claude` then `/mcp`) — it cannot be done from a
  non-interactive session; all live-DB work through Step 8 has gone through
  direct `psycopg` connections instead (`scripts/migrate.py`,
  `arie.evidence.store`, `arie.identity.resolver`, `arie.jobs.queue`).
- **The plain direct-connection host is IPv6-only.** `db.<ref>.supabase.co` has
  no IPv4 (A) record on this project; a dev machine without outbound IPv6
  cannot reach it (confirmed via `nslookup`/`getaddrinfo`, not assumed). Use
  the **Session Pooler** instead —
  `postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres`
  — for both `DATABASE_URL` and `DATABASE_DIRECT_URL` until a worker actually
  needs the Transaction Pooler (port 6543) for concurrent-connection headroom,
  at which point the connection pool needs `prepare_threshold=None`:
  psycopg3's default server-side auto-prepare is unsafe under pgbouncer's
  transaction-mode pooling (a prepared statement can outlive the backend
  connection pgbouncer handed it).
- DeepSeek API key available. Anthropic/OpenAI not confirmed.
- n8n connected via MCP.
- Firecrawl key to be provided.
- `gh` CLI is **not** installed; push over HTTPS with the existing git remote.
- Python 3.11 target (`pyproject.toml`); this environment's dev venv actually
  runs 3.14, which is why `mypy` cannot follow numpy's stubs (3.12-only `type`
  statement syntax) — already handled via a `follow_imports = "skip"`
  override in `pyproject.toml`.

---

## The honest framing, for the README and for interviews

This project built a sophisticated thing, built the ablation that could kill it,
and reported that the ablation won. The temptation in M1 will be to quietly make
the sophisticated version the story again because it sounds better.

Do not. The negative result *is* the story, and it is a stronger one.
