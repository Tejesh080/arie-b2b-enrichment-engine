# M1 Handoff

Written to be read cold, in a fresh session, with no memory of how M0 went.

---

## Where things stand

**M0 is complete.** The intelligence engine exists, is benchmarked, and its
result is published — including the part that failed. Everything runs offline
and deterministically; no credentials, no network.

**M1 Steps 6–12 are complete.** Steps 6–9 (schema/migrations/evidence store,
identity resolution, job queue/state machine, ingestion API, cost ledger,
tracing) are verified against the live production Supabase database. Step 10
(`arie.llm` — DeepSeek buying-signal extraction) needs no database at all and
is verified by 43 mocked unit tests plus 5 live-database ledger tests. Step 11
(`arie.approval.workflow` — the human review API) is verified by 15
live-database tests covering approve/reject/edit, idempotent retries,
conflicting submissions, optimistic-concurrency failure with rollback, and the
audit trail. Step 12 (`workflows/n8n/` — the edge workflows) is JSON, not
Python — there is nothing for pytest to exercise, and it is unchanged by the
rest of this session's gate — see "Suggested order" below for exactly what's
built versus deferred within each step. **Live-measured while running Step
11's gate** (not previously recorded anywhere): DeepSeek vs. the deterministic
baseline on the 26-sample corpus — exact-match accuracy 46.2% -> 73.1%
(+26.9pp), $0.0074 total cost, ~1.17s mean latency, zero validation failures
or retries across all 26 calls. `bench/out/llm_signal_eval.json` has the full
per-field breakdown.

**Nothing yet calls `CalibratedBoundsPolicy` in production, nothing yet calls
`arie.llm`, and nothing yet calls `arie.approval.workflow.request_review`
either.** All three are true for the same reason. There is now a request path
that creates leads and queues work for them, a tested LLM extraction module,
and a tested human-review API — all three could be called from a handler, but
`arie.jobs.worker.main()` runs with zero handlers registered, so every job
still fails with "no handler registered" and correctly retries then
dead-letters. Wiring real handlers — scoring, evidence fetching, the policy,
signal extraction, *and* escalating to a human — is one piece of future work,
not several, and it still needs provider adapters that do not exist yet.

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
| `arie.ledger.store` | Durable equivalent of `CallLedger`. A real handler records every call here, with an idempotency key derived from the job so a retry can't double-charge. |
| `arie.evidence.store` | Durable equivalent of `EvidenceCache`. A handler consults this *before* deciding to buy. |

The shape of the missing piece: a `compute_score` handler receives a
`JobContext` (live connection, claimed job, lead status and version), reads the
lead's known facts from `PostgresEvidenceStore`, runs the policy, records what
it bought in `PostgresCostLedger`, and returns the lead's new status for
`apply_transition` to commit alongside the job. Every one of those collaborators
now exists; nothing yet composes them.

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
company-level evidence store. `PostgresCostLedger.record_provider_call` mirrors
this exactly — a hit is a row at zero cost, not a missing row.

**6. The cost ledger must NOT join the caller's transaction.**
Everything else in M1 argues the opposite way, so this reads like a mistake and
isn't. A rollback does not un-spend money: if a provider call succeeded and the
handler then crashed, rolling the ledger row back means the retry pays again
and no record of the first charge exists anywhere. Ledger writes commit
independently and are made safe by `idempotency_key`, not by sharing a
transaction.

**7. A trace must never be able to fail a job.**
`extract_trace_context` returns `None` for a missing, empty, *or unparseable*
carrier, and the worker starts its own trace instead. A malformed
`traceparent` — a hand-edited row, a future spec version — is an observability
problem; dropping work over it would be a much worse one.

**8. A review's own idempotency and the lead's optimistic concurrency are two
different guards, not one.** `submit_decision` completing a review (the CAS on
`human_reviews.responded_at`) and `apply_transition` moving the lead (the CAS
on `leads.version`) fail independently, and both surface as 409 — but only a
version conflict is safe to retry immediately after re-reading the lead's
current version; a content conflict (`ReviewConflictError`) means someone
already decided this review differently, and retrying with the same body
fixes nothing. Collapsing the two into one error type would make a client's
correct response depend on parsing a message string instead of the exception
class it caught.

---

## Schema already written

`migrations/0001_init.sql` through `0005_ingestion_ledger_tracing.sql` exist and
have been run against the live production Supabase database (Steps 6–9) — one
real bug was found and fixed doing so: `evidence.expires_at` couldn't be a
`GENERATED` column because `timestamptz + interval` is STABLE, not IMMUTABLE
(see the migration's own comment). `migrations/` is the source of truth and
the only path that touches production, via `scripts/migrate.py`.

**`0005` is worth reading before touching the metrics views**, because three
of its five changes are corrections to things that were quietly wrong:

- `v_pipeline_metrics.cost_per_qualified_lead` counted `SYNCED` as qualified,
  but `SYNCED` is where the *reject* branch terminates
  (`DECISION_OUTCOMES["reject"]`). Rejecting more leads therefore made the
  metric look better — the exact failure mode `0002`'s own comment says the
  view exists to expose, inverted. Now `AUTO_ROUTED`/`ROUTED` only.
  **Known follow-up:** `SYNCED` is ambiguous by construction, and a future
  `ROUTED → SYNCED` CRM-sync step (step 12) makes this filter wrong again in
  the other direction. The durable fix is a distinct terminal status for
  rejection — a state-graph change that belongs with the step adding the sync
  path, not with the one that found it.
- `v_escalation_rate` counted a lead once per `human_reviews` row, because the
  `LEFT JOIN` fans a lead out per review. A twice-reviewed lead was two leads,
  and `escalation_rate` was skewed in both directions at once.
- `leads.budget_usd_cap` defaulted to `0.50`, below `deep_research`'s `$0.600`
  list price — the only source of the disqualifying flag. `PolicyConfig`
  documents this exact bug being found and raised to `1.50`; the column default
  never got the fix. Ingestion now writes the configured cap explicitly, and the
  default is corrected so a direct INSERT doesn't walk into it.

Both view defects returned plausible numbers rather than erroring, which is why
they survived until something checked them arithmetically. Both are pinned by
regression tests in `tests/integration/test_cost_ledger_integration.py` that
were confirmed to *fail* against the pre-`0005` definitions — a regression test
that passes against the bug it describes is worth nothing.
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
   itself) behind the job types `next_status` already names is **still the
   biggest single gap in M1**, and step 6 has now removed the excuse: there is
   a request path creating leads with work queued against them.

   Since step 9, `run_worker_cycle` also takes an optional `job_types` filter,
   for running a pool dedicated to one kind of work. It deliberately does *not*
   default to `handlers.keys()`: a job type with no handler anywhere must still
   be claimed, failed, and dead-lettered, because a job silently never claimed
   looks exactly like a backlog and is far harder to notice.
6. ✅ **FastAPI ingest** — `arie.api`, one `POST /leads` plus `GET /leads/{id}`
   and `GET /healthz`. Identity resolution is now called in the request path
   for the first time. The whole request is **one transaction**: identity rows,
   the lead, its `lead:ingested` event, and its first job commit together or
   not at all. `IdentityResolver` and `PostgresJobQueue` grew `*_in(conn, ...)`
   variants for this; the original self-committing methods are unchanged
   wrappers around them.

   Idempotency is `(source, external_ref)` — the partial unique index `0001`
   already created for it. Redelivering a webhook returns the same `lead_id`
   and the same `job_id` with HTTP 200 instead of 201, and creates no second
   job. A lead posted *without* an `external_ref` can't be deduplicated and
   every POST creates a new one; the response's `created` flag says which
   happened.
7. ✅ **Cost ledger + metric views** — `arie.ledger`. Two things to know:

   **Ledger writes commit in their own transaction**, unlike everything else
   in M1. If a provider call succeeds and the handler then crashes, the work
   transaction rolls back — but the money is still spent, so the row recording
   it must not roll back with it. `idempotency_key` is what makes that safe:
   the retry reproduces the key, the UNIQUE constraint rejects the duplicate,
   and `recorded=False` tells the caller it has already paid for this call.
   That is ADR 0002's "never double-charge on retry", enforced by a constraint.

   **Model prices are unverified assumptions.** Provider costs are *reported*
   by the provider and recorded verbatim; model costs are *derived* from token
   counts times a hand-transcribed price table (`arie.ledger.pricing`, listed in
   [`ASSUMPTIONS.md`](ASSUMPTIONS.md)). A stale price produces a plausible
   number, not an error. Step 10 is where they stop being assumptions —
   reconcile against the API's own usage reporting and correct the table.
8. ✅ **OpenTelemetry tracing**, request → enqueue → worker. The link survives
   the process boundary through `jobs.trace_context`, a W3C carrier written at
   enqueue time and read back when the job is claimed, so a lead's HTTP request
   and every processing attempt it caused land in one trace. Tracing is **off
   unless `OTEL_EXPORTER_OTLP_ENDPOINT` is set** — no separate flag, because
   "enabled but pointed nowhere" isn't a state worth being able to express.
9. ✅ **LLM signal extraction** — `arie.llm`, DeepSeek, one narrow task: buying
   signals from free text. Three things worth knowing before touching it:

   **It has no free text to run on yet, and that's expected.** M0's dataset is
   deliberately structured-only (`recent_trigger_event` is a five-value closed
   enum, not prose — see `docs/03-mvp.md`: "No LLM in M0... introducing
   nondeterminism into the experiment that establishes the baseline result is
   a methodological error"). `arie.llm.eval` is *M1's own* small, hand-labeled
   corpus (26 samples, fully synthetic), structurally separate from
   `arie.evalgen`/`bench/multi_seed.py`/`data/eval/` — it imports nothing from
   them, so nothing about it can perturb the M0 benchmark or the provenance
   reconciled just before this step. `bench/llm_signal_eval.py` runs the delta
   measurement (deterministic baseline vs. DeepSeek); it costs a fraction of a
   cent and needs `DEEPSEEK_API_KEY` set, which nothing in this repo requires
   — unset, it reports the baseline only and exits 0.

   **The schema is the entire safety boundary.** `arie.llm.schema.ExtractedSignal`
   (`extra="forbid"`) has no field shaped like an action — no provider name, no
   lead status, nothing a caller could mistake for an instruction to *do*
   something. `arie.llm.deepseek` never registers tools/functions with the API
   either, so there is nothing in the request a model could invoke even if it
   tried. "The LLM may extract signals but must not choose providers, change
   lead state, or call tools" is enforced by what the code makes possible, not
   by a comment saying so.

   **Retries are stateless on purpose.** A schema-invalid response is retried
   with the *identical* request, up to `LLMConfig.max_attempts` times — no
   multi-turn "here's what you got wrong" correction loop, which would start
   to look like the agentic reasoning this step is scoped to exclude. Every
   billable attempt (DeepSeek returned a completion, whether or not it passed
   validation) is ledgered as its own `model_calls` row via
   `arie.llm.deepseek.record_extraction_cost` — a validation-failing retry
   still cost money and does not disappear from the ledger.

   **Not wired into a worker handler.** Building the module and measuring its
   contribution was Step 10's scope; calling it from a live job is the same
   "still the biggest single gap in M1" future work as the policy itself (see
   item 5 above) — there is no `arie.jobs.worker` handler for any job type
   yet, LLM-backed or not.
10. ✅ **Human approval path** — `arie.approval.workflow`, two endpoints
    (`GET /reviews/{review_id}`, `POST /reviews/{review_id}/decision`) around
    the existing `human_reviews` table, plus a second outcome-branching node
    in the state graph. Three things worth knowing before touching it:

    **`request_review` is built and tested but not called by anything yet** —
    the same posture Step 10 took with `arie.llm`. There is still no
    `finalize_decision` handler to call it from (see item 5 above); once one
    exists, escalating a lead is `request_review(conn, lead_id=...,
    expected_version=..., original_decision=...)`, atomically transitioning
    DECISION -> AWAITING_HUMAN and opening the pending `human_reviews` row in
    the same transaction — a crash between the two would otherwise strand a
    lead in AWAITING_HUMAN with nothing for a reviewer to act on.

    **The review action is not the decision-label vocabulary.**
    `human_reviews.original_decision`/`final_decision` speak the same labels
    `DECISION_OUTCOMES` already uses (`auto_route`/`reject`), because
    `v_escalation_rate`'s `human_overrode` column compares them with a bare
    `IS DISTINCT FROM` — that comparison predates this step and was never
    negotiable. A reviewer's action (`approve`/`reject`/`edit`) is translated
    (`approve` -> `auto_route`, `reject` -> `reject`, `edit` ->
    `manual_review`) before it ever reaches `arie.statemachine.transitions.
    HUMAN_REVIEW_OUTCOMES` or the audit row. `edit` is the one outcome with no
    automatic-path equivalent, landing on `MANUAL_REVIEW` — defined since
    Step 8, unreachable until now.

    **Idempotency is content-based, not a supplied token** — consistent with
    this codebase's preference for natural keys (`leads`' `(source,
    external_ref)`) over synthetic ones. A decision is completed by one
    compare-and-swap `UPDATE human_reviews ... WHERE responded_at IS NULL`;
    whichever attempt loses that race either finds an *identical* decision
    already recorded (a client retry — same result comes back,
    `already_applied=True`) or a *different* one (a genuine conflict —
    `ReviewConflictError`, 409). This is a second, independent guard from
    `apply_transition`'s own `OptimisticConcurrencyError` (also 409, see
    below) — see `submit_decision`'s docstring for exactly which race hits
    which one. Migration `0006` adds one partial unique index
    (`human_reviews(lead_id) WHERE responded_at IS NULL`) and no new columns.
11. ✅ **n8n edge workflows** — `workflows/n8n/`, three hand-authored,
    importable JSON files (no live n8n instance was used to build or verify
    them — see below). Two things worth knowing:

    **Every decision stays in ARIE; n8n only maps field shapes and relays
    responses.** `lead-ingestion.json` checks a webhook body has a non-empty
    `email` before forwarding (a presence check, not a format/domain check —
    those are still ARIE's `POST /leads` 422s) and passes `source`/
    `external_ref` straight through unchanged so ARIE's own `(source,
    external_ref)` uniqueness is what makes redelivery idempotent, not
    anything n8n does. `outcome-sync.json`'s "finalized" gate and its
    `qualified` field both restate status labels ARIE already computed (the
    same `AUTO_ROUTED`/`ROUTED` split `v_pipeline_metrics.
    cost_per_qualified_lead` uses) — relabeling for a CRM shape, not a new
    rule invented in n8n.

    **`outcome-sync` is receive-triggered, not polling, on purpose.** ARIE has
    no "list leads by status" endpoint today; building client-side
    discovery/retry logic into n8n to fake one would be exactly the
    backend-duplicating logic this step was scoped to avoid (see "Do not"
    below). It expects to be called with `{"lead_id": "..."}` once something
    already knows a decision finalized — there is no caller wired up to do
    that yet, the same "built and tested, not yet wired into anything that
    runs automatically" posture Steps 10 and 11 left `arie.llm` and
    `arie.approval.workflow.request_review` in. A third file,
    `mock-crm-sink.json`, is **not** one of the two required workflows — a
    minimal local stand-in (webhook that echoes what it received) so
    `outcome-sync` has somewhere real to `POST` to without a real CRM
    dependency; swap `MOCK_CRM_SINK_URL` for a real endpoint later and delete
    it, nothing else changes.

    The three JSON files were hand-authored against n8n's current node
    schemas (confirmed via the n8n MCP server's read-only node-reference
    tools, not by creating anything in a live instance) rather than built
    through that server's live-instance SDK flow — this repo's `.mcp.json`
    doesn't declare an n8n connection, and the task asked for portable,
    importable JSON files, not workflow state tied to somebody's n8n
    instance. **Consequently these have never been imported into a running
    n8n and executed** — no n8n / Docker was available in this environment to
    do that in. The JSON was validated structurally (parses, every
    `connections` target resolves to a real node name, no duplicate node
    ids/names) but not by an actual n8n import. Treat "does this import
    cleanly and run" as the one thing about Step 12 still worth verifying by
    hand before relying on it — see `docker compose --profile n8n up -d n8n`
    in the README.

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
- Give `arie.llm` a second task, a tool-calling loop, or a second model to
  escalate to. One narrow extraction task, one fixed cheap model, zero tools
  registered with the API — each of those was a deliberate Step 10 boundary,
  not an oversight to fill in later. A second task is a second prompt to
  validate and a second reason the schema might grow an action-shaped field;
  a tool-calling loop or a model cascade is exactly the "multi-model routing"
  and agentic behaviour this step was scoped to exclude by name.
- Put scoring, confidence logic, policy decisions, identity resolution, or
  retry semantics in `workflows/n8n/`. Field mapping and routing on a status
  ARIE already decided are fine (see Step 11 above); re-deriving what that
  status *should* be, or client-side polling/discovery logic standing in for
  an endpoint ARIE doesn't have, is not — that logic would live in two
  places, in two languages, and drift apart the first time one of them
  changes without the other.

---

## Guard rails to keep green

```bash
make lint && make type && make test     # ruff, mypy --strict, unit tests (no DB, no network)
make check-supabase-migrations          # supabase/migrations/ must match migrations/ — see ADR 0005
make validate-dataset                   # dataset must stay non-trivial
python -m bench.multi_seed              # ~15 min for 10 seeds; run before claiming an improvement
```

`python -m bench.multi_seed` runs its default seed set now, no `--seeds` needed
— `DEFAULT_SEEDS` was 7 seeds until the M1 Step 9 gate's benchmark-provenance
reconciliation found it didn't match the 10 seeds every published number
actually used (that command was always documented at the top of
[`05-results.md`](05-results.md), just not the tracked default). Fixed, and
`data/eval/manifest.json` regenerated to match — both were provenance gaps, not
wrong numbers: a fresh 10-seed run reproduced every figure in
`05-results.md`/README/ADR 0004 exactly. Full account:
[`05-results.md`'s reconciliation section](05-results.md#reconciling-this-page-against-a-fresh-run).
If a future run of this command *doesn't* reproduce those numbers, that is a
real regression worth investigating, not something to assume is "probably the
same provenance gap again."

CI additionally runs `ruff format --check`, which `make lint` does not — a
change that passes locally can still fail the build on formatting. Run
`make fmt` before pushing.

> **`make` is not installed on the Windows dev machine this was built on.**
> Run the underlying commands directly instead: `ruff check src tests bench
> scripts`, `ruff format --check src tests bench scripts`, `mypy src tests
> scripts`, `pytest -m "not integration"`, `python
> scripts/sync_supabase_migrations.py --check`.

`make test` runs offline; it does not exercise anything in `arie.evidence`,
`arie.identity`, `arie.jobs`/`arie.statemachine`, `arie.api`, or `arie.ledger`
against a real database — that needs `make test-all` with
`DATABASE_URL`/`DATABASE_DIRECT_URL` set, which is opt-in specifically because
it writes to whatever database those point at (see
`tests/integration/conftest.py`'s own warning). Don't infer "the M1 pieces are
covered" from a green `make test` alone.

**Two things about the integration tests worth knowing before adding more.**

They run against a *shared, live* database that also holds real rows, so no
test can assert an absolute value out of a metrics view. The view tests measure,
make one known change, measure again, and assert on the difference — and each
pairs that with an assertion that the change was visible at all, so "unchanged"
can't pass vacuously.

OTel's global tracer provider is **set-once**. The `span_exporter` fixture is
session-scoped and lives in `tests/conftest.py`, not the integration conftest,
precisely so unit and integration tests share one provider; split across two
packages, whichever ran first would win and the other's exporter would silently
receive nothing.

Running the service locally:

```bash
make serve    # uvicorn arie.api.main:app --reload --port 8000
make worker   # python -m arie.jobs.worker  (still zero handlers registered)
```

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
