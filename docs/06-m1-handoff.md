# M1 Handoff

Written to be read cold, in a fresh session, with no memory of how M0 went.

---

## Where things stand

**M0 is complete.** The intelligence engine exists, is benchmarked, and its
result is published — including the part that failed. Everything runs offline
and deterministically; no credentials, no network.

**M1 Steps 6–13 are complete, and M1 is frozen as of a post-freeze audit pass
that found and fixed 7 real defects** — see "Post-M1 audit freeze fixes"
below for what they were and how each was verified. Steps 6–9 (schema/migrations/evidence store,
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

**`CalibratedBoundsPolicy` and `request_review` now run in production;
`arie.llm` still does not.** The Step 12 runtime fix (found by actually
booting the Compose stack, not by tests) wired `arie.jobs.handlers` into
`arie.jobs.worker.main()`: `compute_score` looks the ingested lead up in the
frozen corpus, runs the policy over the simulated registry with a
write-through durable evidence cache and cost ledger, walks the lead through
the state graph inside the work transaction, persists a `scores` row, and
either lands the lead in `AUTO_ROUTED`/`SYNCED` or escalates via
`request_review`. Two boundaries to keep in mind: **only corpus identities
can be enriched** (simulated providers replay frozen observations; anything
else retries then dead-letters with a message saying so — the honest shape of
"production" until a real vendor adapter exists), and **`arie.llm` remains
uncalled** because ingestion carries no free-text field to extract from —
adding one is its own decision, not an oversight here.

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
5. ✅ **State machine + production handler — the "biggest single gap" is
   closed** (Step 12 runtime fix). `arie.statemachine` gives the pure
   `next_status`/`job_type_for` graph and `apply_transition` (optimistic
   concurrency on `leads.version`, atomic with job completion);
   `arie.jobs.handlers.build_handlers` now populates it. Design worth reading
   before touching (`arie.jobs.handlers`' module docstring has the full
   argument): **one handler, not four.** The policy's score/buy/score loop is
   a single calculation that reads evidence content to decide whether to keep
   buying (ADR 0001), so `compute_score` runs the whole pipeline and walks the
   lead `NEW → SCORING → FETCHING_EVIDENCE → INTEGRATING → DECISION → branch`
   itself, one audited `apply_transition` per hop, all inside the one work
   transaction — the other three job types stay unclaimed on purpose, and the
   worker's no-handler path dead-letters them loudly if anything ever enqueues
   one. Evidence and ledger writes go through write-through subclasses of the
   benchmark's own `EvidenceCache`/`CallLedger` (durable-cache hits are
   provider-keyed, exactly the benchmark's semantics; ledger idempotency keys
   derive from the job id, so a crashed-and-retried job reproduces its keys,
   can't double-charge, and gets served from the evidence its first attempt
   already persisted). The worker fits the confidence model at startup —
   refit, never pickled — and refuses `PROVIDER_MODE=live` outright since no
   real adapter exists.

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

    **`request_review` is now called in production** — `compute_score`'s
    escalation branch (item 5 above) invokes it for every non-autonomous
    decision and every decision the policy itself labels `escalate_human`:
    `request_review(conn, lead_id=..., expected_version=...,
    original_decision=str(outcome.decision))`, atomically transitioning
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
    importable JSON files, verified end to end against a real local n8n
    instance (see below). Two things worth knowing:

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
    dependency; edit the URL on `outcome-sync`'s "POST Mock CRM Sink" node to
    point at a real endpoint later and delete this workflow, nothing else
    changes.

    The three JSON files were hand-authored against n8n's current node
    schemas (confirmed via the n8n MCP server's read-only node-reference
    tools, not by creating anything in a live instance) rather than built
    through that server's live-instance SDK flow — this repo's `.mcp.json`
    doesn't declare an n8n connection, and the task asked for portable,
    importable JSON files, not workflow state tied to somebody's n8n
    instance. First validated structurally only (parses, every `connections`
    target resolves to a real node name, no duplicate node ids/names), then
    **verified for real** in a follow-up session: imported into a local n8n
    (`docker compose --profile n8n up -d`), activated, and driven through
    both webhooks — ingestion returned 201, a worker processed the job to
    `AUTO_ROUTED`, outcome-sync reached the mock sink and returned
    `synced: true`, and redelivering the ingestion payload returned the same
    lead with `created: false`/`job_created: false`.

    **One real bug found doing that, worth knowing before writing another
    expression:** the original JSON read `ARIE_API_BASE_URL`/
    `MOCK_CRM_SINK_URL` via `{{ $env.VAR }}`, set correctly on the container
    in `docker-compose.yml` — and it silently didn't work. n8n blocks `$env`
    access inside node expressions by default
    (`N8N_BLOCK_ENV_ACCESS_IN_NODE`), which this repo's Compose file never
    overrode, so the expression evaluated to nothing and the HTTP Request
    nodes called broken URLs. Fixed by hardcoding the two Docker-network
    service names directly in the JSON (`http://api:8000/leads`,
    `http://n8n:5678/webhook/mock-crm-sink`) rather than routing through env
    vars at all — both are this Compose network's own DNS, not secrets, and
    the now-dead env vars were removed from the `n8n` service. If a future
    change reintroduces `$env.*` in one of these workflows, this is why it
    will look like it should work and silently won't.

    **Local n8n vs. a hosted n8n account — read before touching either.**
    The Docker `n8n` service is a reproducible dev/demo environment shipped
    *with the repo*, same philosophy as everything else here (offline-first,
    zero required credentials). A separately hosted n8n account is a *later*,
    deliberately deferred integration target — relevant once ARIE has a
    publicly reachable deployed API for it to call, which nothing through M1
    sets up. Nothing in this repo connects to or deploys against any hosted
    n8n account; do not wire one up without being asked to, and do not remove
    the local Docker service once a hosted one exists — they serve different
    purposes (reproducible local demo vs. a real integration target) and both
    are wanted.
12. ✅ **Production-readiness hardening (Step 13).** `/healthz` now reports
    three states, not one — `database` (a real round trip) and `schema_ready`
    (every file in `migrations/` has a `schema_migrations` row, checked by
    the new `arie.migrations.pending_migrations`, factored out of
    `scripts/migrate.py` so a read-only readiness check doesn't depend on
    `scripts/` being importable from wherever the API happens to be launched
    — see the module's own docstring for why that dependency direction
    matters). A reachable-but-not-yet-migrated database used to read
    identically to "database is down"; it no longer does, and both the
    Dockerfile's new `HEALTHCHECK` and `docker-compose.yml`'s `n8n` service
    (now gated on `condition: service_healthy` rather than merely "started")
    read the distinction.

    `arie.jobs.worker` didn't handle `SIGTERM` at all — Docker's actual stop
    signal, not `SIGINT`/Ctrl-C — so the raw polling loop's default
    disposition was immediate termination, skipping cleanup entirely.
    `install_graceful_shutdown` now routes both signals through one
    `threading.Event`; the loop always finishes whatever `run_worker_cycle`
    already claimed before checking whether to stop. Verified against a real
    container, not just read: `docker stop -t 10` on both api and worker
    exits 0 in under a second, worker's own "Worker stopping." reaching its
    logs before it does.

    Compose's clean-start ordering (Step 12's own fix) is now proven on a
    genuinely clean machine every push, not just asserted as YAML. CI's new
    `compose-smoke` job builds and starts the real stack on a GitHub-hosted
    runner that has never seen this image or a `pgdata` volume, waits for the
    API's own `HEALTHCHECK` to report `healthy`, POSTs the same corpus
    identity (`nadia.delacroix@lumen500.com`) the README's Quick Start uses,
    and fails the build if the lead doesn't reach a terminal/escalated status.
    A second new job, `integration`, runs the full `pytest -m integration`
    suite against a disposable `postgres:16-alpine` service container — CI
    had never run these tests at all before Step 13, meaning `arie.evidence`,
    `arie.identity`, `arie.jobs`/`arie.statemachine`, `arie.api`, and
    `arie.ledger` were untested by CI end to end, exactly the gap this doc's
    own warning about `make test` alone was flagging. Neither new job touches
    the shared Supabase database Steps 6–12's manual verification used —
    both are disposable, born and destroyed with their own CI run.

    The escalation path had a real coverage gap: `test_pipeline_integration.py`
    drove a lead to a pending human review and stopped there, and
    `test_human_review_integration.py` exercises the review API against a
    hand-built lead row rather than one that arrived via ingestion and the
    worker — nothing closed the loop.
    `test_the_m1_smoke_path_ingestion_through_human_review_decision` does:
    ingestion → queue → worker → escalation → `GET /reviews/{id}` →
    `POST .../decision` → `AUTO_ROUTED`, the path this milestone is named
    for, driven through the real HTTP surface end to end.

    Smaller fixes alongside these: `_env_float`/`_env_int` used to raise a
    bare Python `ValueError` on a malformed environment value, naming neither
    the variable nor the bad value — both now are. No `.dockerignore`
    existed; one now excludes `.git`, `.venv`, caches, and `.env*` from the
    build context (the Dockerfile never `COPY`s any of them, but an
    unfiltered context still uploads them to the daemon). `db`, `api`, and
    `worker` now `restart: unless-stopped`; `migrate` deliberately does not
    (still pinned by `test_migrate_is_one_shot`).

    New: [`docs/07-deployment.md`](07-deployment.md) — migration ordering for
    a non-Compose target, required environment variables, the `/healthz`
    contract, and the shutdown behaviour above, written for whoever picks a
    hosting platform next (deliberately unprescribed — see ADR 0002 for why
    Kubernetes/Temporal/Redis aren't it).

    **What Step 13 did not change:** the architecture, the Supabase
    production/preview migration split (ADR 0005, untouched), `PROVIDER_MODE`
    (still refuses anything but `simulated`), or the hosted-n8n boundary
    (still nothing connects to one). This step was hardening, not new
    surface area.

---

## Post-M1 audit freeze fixes

An independent audit against commit `0794c62` (Step 13) found 7 genuine
correctness defects worth fixing before freezing M1, and a separate list of
things deliberately *not* worth fixing (Category 2/3 — see the audit itself
for the full list; nothing there was acted on here, on purpose). All 7 are
fixed, each with a regression test, and each is noted below with how it was
verified — several were reproduced against a live database or a real
container before being fixed, not just reasoned about.

1. **Dead-lettered jobs were permanently unrecoverable.** `jobs.idempotency_key`
   is a plain UNIQUE, not scoped by status, so a webhook redelivered after a
   job exhausted its attempts matched the dead row forever —
   `job_created: false`, HTTP 200, and no worker would ever claim the lead
   again. `arie.jobs.queue._ENQUEUE` now requeues a `dead_letter` match
   (fresh attempt budget, lease cleared) instead of leaving it alone; every
   other status keeps the exact prior idempotent behaviour. `EnqueuedJob`
   and the API response both gained `requeued`/`job_requeued` so a caller can
   tell "already in flight" apart from "just recovered from permanent
   failure." **Reproduced twice** — once via a standalone probe script
   against local Postgres during the audit, once for real inside the rebuilt
   Docker container afterward (`job_requeued: true`, immediately
   reprocessed to `AUTO_ROUTED`) — and is now the subject of
   `tests/integration/test_job_queue_integration.py`'s and
   `test_api_ingestion_integration.py`'s dead-letter tests.

2. **`complete()`/`fail()` had no ownership check.** Both matched a job by
   `job_id` alone, so a *stale* worker (lease reclaimed after expiring, or a
   lost claim race) could resurrect a job another worker already finished,
   or double-count a single real failure — once via
   `_RECLAIM_EXPIRED_LEASES`'s own reclaim, again via the stale worker's late
   report, dead-lettering jobs at roughly half their configured budget.
   Fixed with a `WHERE status = 'processing' AND locked_by = %(worker_id)s`
   predicate on every write (`arie.jobs.queue.JobOwnershipError` when it
   doesn't match — the same compare-and-swap-and-raise shape
   `OptimisticConcurrencyError`/`ReviewConflictError` already use elsewhere
   in this codebase, not new infrastructure). `arie.jobs.worker` threads
   `worker_id` through and reports a `"lost_lease"` outcome rather than
   crashing the whole cycle. **Reproduced** via two standalone probe scripts
   against local Postgres (resurrection after completion; double-counted
   attempt after reclaim) during the audit; both scenarios are now
   `tests/integration/test_job_queue_integration.py` tests
   (`test_a_stale_worker_cannot_*`), plus a worker-loop-level test in
   `test_statemachine_integration.py` proving the cycle reports `lost_lease`
   instead of raising.

3. **Migration discovery failed open.** `Path.glob()` on a nonexistent
   directory returns `[]`, not an error, so a wrong `MIGRATIONS_DIR` (e.g.
   after ever switching the Dockerfile to a non-editable install, where the
   `parents[2]` arithmetic `arie.migrations` depends on no longer lands in
   the repo root) made `pending_migrations` report "nothing pending" and
   `/healthz` report `ok` on a database with no schema at all — a readiness
   check whose failure mode was "silently always healthy." `migration_files`
   now raises `MigrationsDirectoryError` if the directory is missing or has
   zero `*.sql` files (this repo always ships at least `0001_init.sql`, so
   zero found means the path is wrong); `/healthz` treats that the same as a
   real pending migration (`degraded`, never `ok`). **Reproduced** — the
   empty-list return was confirmed directly against a missing path during
   the audit — and is now `tests/unit/test_migrate.py`'s and
   `tests/integration/test_api_ingestion_integration.py::
   test_healthz_never_reports_ok_when_migration_discovery_fails`.

4. **A failed CRM sync reported success.** `outcome-sync.json`'s "POST Mock
   CRM Sink" node sets `neverError: true` and connected straight to a node
   hardcoding `synced: true, responseCode: 200` — no status check at all, so
   a 500 from the sink still answered `{"synced": true}`. A new
   `Sink Succeeded?` IF node now gates on `statusCode` before responding; a
   non-2xx routes to a new `Respond (Sink Failed)` node (502, `synced: false`,
   the sink's real status/body preserved). Verified structurally (parses,
   the connection graph actually branches, the failure response never
   contains `synced: true` or a 200) in `tests/unit/test_n8n_workflows.py`
   — **not** re-verified against a live n8n instance; that would need a
   deliberately-failing mock sink wired up in the local Docker n8n, which
   this pass didn't do.

5. **Three inconsistent definitions of "finalized," and permanent failures
   polled forever.** `arie.statemachine.transitions.TERMINAL`, the CI smoke
   test, and `outcome-sync.json`'s gate each listed a different status set,
   and none of them told a `FAILED`/`DEAD_LETTER` lead apart from one still
   genuinely in progress — `outcome-sync` answered
   `{"synced": false, "reason": "lead not finalized"}` forever for a lead
   that would never finalize. `arie.statemachine.transitions` now defines
   the vocabulary once — `QUALIFIED`, `REJECTED`, `AWAITING_REVIEW`,
   `FAILURE`, and `FINALIZED = QUALIFIED | REJECTED` — a **different axis**
   from the pre-existing `TERMINAL` (which is about whether this module's
   own job-queue mechanism auto-advances a status, not business meaning; see
   both sets' docstrings). `outcome-sync.json` gained an
   `Is Permanently Failed?` branch responding `{synced: false, terminal:
   true, reason: 'lead processing failed permanently'}` — distinguishable
   from the still-waiting case, which now explicitly says `terminal: false`.
   `tests/unit/test_n8n_workflows.py` reads the literal status lists back out
   of the committed JSON and asserts them against the Python vocabulary, so
   the two can't silently drift apart again the way they already had.

6. **`cost_per_qualified_lead` still had the wrong qualified set.** 0005
   already fixed this view once (excluding the *reject* terminal `SYNCED`
   from "qualified"), but that fix predates Step 11's human-review path and
   used `('AUTO_ROUTED','ROUTED')` — `ROUTED` is not reachable by anything in
   this codebase (reserved for a future CRM-sync step), and `MANUAL_REVIEW`
   (the terminal a human's `action=edit` decision reaches) was missing
   entirely, so every human-edited lead's spend vanished from both sides of
   the ratio. `migrations/0007_qualified_lead_definition.sql` corrects the
   filter to `('AUTO_ROUTED','ROUTED','MANUAL_REVIEW')` — the same set
   `arie.statemachine.transitions.QUALIFIED` now defines once.
   **Verified as a real regression**, not just reasoned about: the new
   `tests/integration/test_cost_ledger_integration.py::
   test_v_pipeline_metrics_counts_manual_reviewed_leads_as_qualified` was run
   against the pre-fix view definition first (confirmed to fail — a
   MANUAL_REVIEW lead's cost made the metric `None` instead of reflecting
   it) and against the corrected one after (confirmed to pass), by directly
   re-executing both view definitions against the local database, not by
   inference.

7. **Same-source evidence rows could be simultaneously fresh and treated as
   conflicting.** `evidence` has no uniqueness constraint on `(entity_type,
   entity_id, field_name, source)` — deliberate, see `put_many`'s docstring,
   history is the point — so a provider whose declared fields have different
   TTLs (e.g. a 90-day field and a 30-day field) gets re-called once the
   short-TTL field expires and re-writes *all* its fields, including the
   long-TTL one that was still fresh. `_SELECT_ALL_FRESH` had no `DISTINCT
   ON`, so a direct reader got both rows and would score one source as
   disagreeing with its own earlier reading. (The currently-wired production
   path, `arie.policy.evidence_view.candidates_from_results`, turned out to
   be structurally immune — it builds from a `Mapping[str, ProviderResult]`,
   one entry per provider by construction — and `_DurableEvidenceCache.get`
   already deduped defensively; the exposure was in `get_all_fresh` as a
   general-purpose read API future callers would inherit, `arie.scoring.
   merge.candidates_from_evidence` among them.) Fixed with `DISTINCT ON
   (field_name, source)` in the read query — nothing stored is deleted or
   upserted over; older rows stay for audit, only what counts as *current*
   changed. **Verified as a real regression**, the same way as item 6: the
   new mixed-TTL test in `tests/integration/test_evidence_store_integration.py`
   was confirmed to fail against the pre-fix query (returned 2 fresh
   `industry` rows from the same source) and pass against the fix, by
   directly re-executing both query definitions against the local database.

**What this pass deliberately did not touch:** any Category 2 finding from
the audit (missing FK indexes, `ON DELETE` policy gaps, timezone-dependent
`date_trunc` metrics, the `p50`/`p95` latency padding, CI blind spots like
`docker compose ps -q` returning empty for a crashed container, the broken
`arie-bench` console script, and more — all real, all deliberately deferred,
not silently dropped) or any Category 3 item (no distributed locks, no
outbox pattern, no event sourcing, no Kubernetes/Temporal/Redis/Celery/Kafka
— see the audit's own reasoning for why each would be the wrong fix). Nothing
here started Decision Receipts, Policy Lab, the human-review UI, real
provider integration, or a README redesign — those remain explicitly
post-M1, not begun.

---

## Post-M1 P1 — Decision Receipt

M1 is still frozen; this is the first post-M1 product feature, scoped to
exactly one thing: `GET /leads/{lead_id}/receipt`, which answers *why did
ARIE stop spending money and make this decision* from persisted state, for a
reviewer who hasn't read the source.

**Purpose.** Make ARIE's internal decision intelligence legible: the
recommendation, score and bounds, why acquisition stopped, what was spent,
provider activity and cache reuse, and — kept structurally separate — whether
a human overrode the recommendation and what actually happened. See the
README's [Decision Receipt](../README.md#decision-receipt) section for the
worked example and full field-by-field shape.

**Persistence model.** An audit of `leads`, `lead_events`, `scores`,
`evidence`, `provider_calls`, `human_reviews`, and the job queue found most of
the receipt reconstructable live from tables that are already durable and
lead-scoped — `provider_calls`, `human_reviews`, and the `human_review:
decided` event payload never change after being written, so reading them for
an old lead is reading history, not today's state. Three things are neither
durable nor lead-scoped: score bounds (computed transiently in
`arie.scoring.engine.ScoreBounds`, never persisted), the
policy/scorer/confidence-calibration identifiers in effect, and which
evidence source won each field. That last one matters most: `evidence` is
keyed by `(entity_type, entity_id)` — company/person, not lead — and is
shared and mutated by every other lead at that company, so reading it "now"
to explain an old decision would describe today's cache, not what was known
when the decision was made — exactly the trap this section exists to avoid.

Migration `0008_decision_receipts.sql` adds one small table,
`decision_receipts`, holding only those three things plus the decision label,
confidence, and stop reason. `arie.jobs.handlers.compute_score` writes it
once, inside its existing work transaction, right beside the `scores` insert
it sits next to — same atomicity, no new commit boundary. It is never updated
afterward; a later human decision is layered on top by reading
`human_reviews` live at request time, not by rewriting the snapshot. This is
"model machine receipt + human disposition separately" (the simpler of the
two historically-truthful options), not a snapshot-plus-patch design.

**Endpoint.** `GET /leads/{lead_id}/receipt` — 404 for an unknown lead;
otherwise always 200, with `status` distinguishing `"decided"` (a
`decision_receipts` row exists), `"pending"` (still mid-pipeline — nothing to
report yet, not an error), and `"processing_failed"` (dead-lettered before
ever deciding, told apart from "pending" via `arie.statemachine.transitions.
FAILURE`). `arie.api.receipt.build_receipt` composes it; `arie.api.schemas.
ReceiptResponse` is the Pydantic surface.

**Intentionally deferred.** `estimated_cost_avoided_usd` — no per-lead
full-enrichment counterfactual is reliably available yet (would need to
account for cache hits the counterfactual itself would have had); actual
spend is reported instead, per this feature's own scoping note. A per-provider
"why was this one skipped" verdict — `CalibratedBoundsPolicy` doesn't evaluate
the remaining catalogue when it stops, so `providers.not_called` is reported
as a set difference against the catalogue with the shared stop reason, never
as an individually-reasoned skip. No UI — the API is the deliverable for this
phase.

---

## Post-M1 P2 — One-command demo

**Purpose.** P1 made ARIE's decisions legible through an API; P2 makes that
legible to a reviewer who doesn't want to construct JSON or call
`Invoke-RestMethod` by hand. `.\scripts\demo.ps1` starts the stack if needed,
drives a handful of deterministic corpus leads through ARIE's public HTTP API
only, and renders a terminal summary plus a static HTML report — see the
README's [60-second local demo](../README.md#60-second-local-demo) section.

**Files.** `scripts/demo.ps1` is a thin Windows launcher (resolve `.venv`,
forward `-Fresh`, invoke the Python entry point, propagate the exit code) —
all the logic lives in the portable, unit-tested `scripts/demo/` package:
`corpus.py` (selects and verifies deterministic demo identities against the
policy actually running today, never hardcoding an untested assumption),
`client.py` (a bounded HTTP client — every request and every polling loop has
a wall-clock timeout, never hangs), `stack.py` (bounded `docker compose`
orchestration, `db`/`migrate`/`api`/`worker` only, never `n8n`),
`scenarios.py` (orchestrates the scenarios below against a `DemoApiClient`
Protocol both the real client and tests satisfy), `render.py` (receipt dict
-> presentation mapping — the one place `providers.called` gets split into
fresh calls vs. cache reuses), `report.py` (HTML/JSON generation, every
interpolated value escaped), and `cli.py` (orchestration entry point).

**One small API addition, not a demo-only backdoor.** The receipt's
`human_review` section didn't expose `review_id` in P1 — there is no
`GET /leads/{lead_id}/reviews` endpoint, so a caller who only reads receipts
had no way to act on a pending review at all. Adding `review_id` there is
generally useful (any client of the receipt, not just this demo) and is the
only way `scenarios.py`'s human-review flow avoids querying Postgres
directly.

**Scenarios demonstrated:**

- **A — autonomous decision.** `nadia.delacroix@lumen500.com`
  (`AUTO_ROUTED`), rendered with score/bounds/confidence/tau/stop-reason/cost,
  and fresh-vs-cache provider activity.
- **B — human escalation and override.** `nadia.haddad@cobalt500.com`
  escalates (`recommended_action="reject"`, `autonomous=False`); the demo
  approves it via `POST /reviews/{review_id}/decision` (reviewer
  `arie-demo`) and re-fetches the receipt. The frozen recommendation and the
  live override are shown side by side — never collapsed into "ARIE decided
  AUTO_ROUTED," the exact failure mode P1's own brief warned against.
- **C — idempotent redelivery.** Scenario A's exact `(source, external_ref)`
  is POSTed a second time in the same run; the report shows `created`/
  `job_created` both `False` on the redelivery.
- **D — company-level evidence reuse.** Two distinct corpus contacts sharing
  a company, chosen deterministically (lowest-sorting domain with >=2 people).
  Only rendered if the second lead's receipt shows at least one measured
  cache reuse this run — never a fabricated saving; omitted entirely if the
  corpus has no such pair or no reuse was observed.

**Intentional omissions.** `estimated_cost_avoided_usd` — same reasoning as
P1, not revisited here. No claim that a `not_called` provider was
individually evaluated and rejected — `CalibratedBoundsPolicy` doesn't do
that (see P1's own note above); the report says "not called this run," not
"skipped because it couldn't change the decision." No M0 benchmark framing
(cost/quality trade-off numbers) anywhere in the generated report — the
footer points to `docs/05-results.md` instead of restating or summarizing it.
No UI framework, no CDN dependency, no build step — plain generated HTML/CSS.
`n8n` is never started or required.

---

## Post-M1 P3 — Policy Lab

**Purpose.** P1 and P2 made individual decisions legible; P3 makes the M0
benchmark's aggregate result legible — *why* `CalibratedBoundsPolicy` is the
production policy, visually, for a reviewer who won't read
`docs/05-results.md` end to end. `.\scripts\policy-lab.ps1` reads the frozen
10-seed benchmark artifact and renders a static Pareto chart plus narrative —
see the README's [Policy Lab](../README.md#policy-lab) section. This is
visualization and interpretation of already-frozen M0 results, not a new
benchmark run: it does not touch scoring, calibration, provider simulation,
the dataset generator, or any policy, and it does not retune EVoI.

**Canonical source of truth.** `bench/out/multi_seed.json`, written by
`python -m bench.multi_seed` (`bench/multi_seed.py`'s `DEFAULT_SEEDS`, 42–51).
Confirmed byte-for-byte against every figure in `docs/05-results.md` before
building anything on top of it. **`bench/out/` is gitignored** — the artifact
only exists locally once the benchmark has been run at least once; a fresh
clone does not have it. `scripts/policy_lab/cli.py`'s default mode fails with
a clear "run `python -m bench.multi_seed`" message rather than silently
regenerating a ~15-minute benchmark; `-Regenerate` is the explicit, separate
opt-in that does. `data/eval/manifest.json` (tracked) supplies dataset
generator/rules version for the report's provenance section; it describes
seed 42's dataset specifically; every seed in the sweep shares the same
generator and rules version.

**Why the artifact's own `stability` array isn't the source for
`adaptive_voi_x1`.** Its precomputed "adaptive" rows summarize
`best_adaptive` (the best-of-seven `value_scale` variant *for that seed*),
not the single un-scaled `adaptive_voi_x1` policy this report names — the
same distinction `docs/05-results.md` calls out for its own headline table.
`scripts/policy_lab/stats.py` recomputes mean/stdev/min/max directly from
`per_seed[].policies[]`'s raw rows for exactly the four named policies
(`full_enrichment`, `waterfall_expensive`, `calibrated_bounds`,
`adaptive_voi_x1`), using the same method (`statistics.fmean`/`stdev`)
`bench/multi_seed.py` uses — self-contained and independently checkable
against the artifact rather than trusting a convenience field that means
something subtly different.

**Pareto frontier is computed, not asserted.** `scripts/policy_lab/pareto.py`
implements dominance directly (no more expensive, no worse agreement,
strictly better on at least one axis) and derives frontier membership from
whatever the four policies' mean cost/agreement actually are. On the current
frozen numbers: `full_enrichment`, `waterfall_expensive`, and
`calibrated_bounds` are each other's cheaper-but-worse / pricier-but-better
trade-off and all sit on the frontier; `adaptive_voi_x1` is dominated
outright by `calibrated_bounds` (cheaper *and* better agreement, on this
benchmark's means) — the same "adaptive EVoI did not establish a win"
finding `docs/adr/0004-evoi-is-a-negative-result.md` reports, arrived at
independently by a different computation over the same data.

**Wording discipline.** The report never says "cheaper with the same
quality," "no accuracy loss," "highest quality," "human-level," or "EVoI
won," never calls a frontier point "Pareto optimal" casually (always
"Pareto-efficient" / "on the Pareto frontier," and only for policies the
dominance computation actually places there), and never states a cost saving
without the paired agreement decrease in the same sentence. Calibrated
Bounds is labeled "Production policy," never "Best policy" — it has neither
the highest agreement (full enrichment and the tuned waterfall both score
higher, at higher cost) nor is it framed as such; it's cheapest at every
human-review price tested and reaches the highest autonomous rate of any
evaluated policy. `tests/unit/test_policy_lab_report.py` pins the prohibited
phrases' absence and the cost/agreement pairing as regression tests, not just
a review-time check.

**Variability shown honestly.** Min–max range across the ten seeds, drawn as
whiskers on the chart and as explicit ranges in the comparison table — no
fabricated confidence interval. The whiskers overlap substantially between
policies, which is itself the finding `docs/05-results.md`'s stability table
already reports in prose ("the variance is large relative to the effect");
the chart just makes that visible instead of restating it as text.

**Files.** `scripts/policy_lab/artifacts.py` (locate/parse/validate the
artifact, `ArtifactError` for anything malformed), `stats.py` (per-policy
`SeedSeries`/`PolicyStats` from raw per-seed rows), `pareto.py`
(`ParetoPoint`/`dominates`/`compute_frontier`, pure), `comparison.py`
(Calibrated Bounds vs. tuned waterfall — both the mean-of-ratios and
ratio-of-means cost readings, matching `docs/05-results.md`'s own convention
of reporting both), `chart.py` (hand-built inline SVG, deterministic
bounding-box collision avoidance for point labels and the production
callout — no charting library, no CDN), `report.py` (HTML/JSON assembly,
every interpolated value escaped), and `cli.py` (orchestration entry point;
`-Regenerate` is the only path that shells out to `bench.multi_seed`).

**Intentional omissions.** No policy tuning, no new benchmark run by default,
no server, no database, no React/Next.js/Streamlit — plain generated
HTML/CSS/SVG, same posture as P2. The main chart compares exactly the four
policies the brief named; the raw artifact's other 14 tuning variants
(per-tier waterfalls, the EVoI `value_scale` sweep, the escalation-aware
review-price sweep) are named by count in the provenance section for
auditability, not plotted — diluting the four-policy comparison with tuning
internals was judged to work against the "understand this in under a minute"
goal.

---

## Post-M1 P5 — One real provider + shadow mode

**Purpose.** P1–P3 made ARIE's decisions and the M0 result legible; P5 proves
two narrower things: (1) the provider abstraction built for the simulator
holds up against one real vendor, and (2) ARIE can safely observe a real
enrichment workflow without controlling it. Both are deliberately narrow —
see "Non-goals" below and the P5 task brief's own scoping.

**Provider selected: Abstract API — Company Enrichment.** Chosen over
Hunter.io (API access gated behind a paid tier) and other candidates because
its response fields (`employee_count`, `industry`) are an exact match for two
of `arie.scoring.rules.SCORED_FIELDS`, its auth is a plain query parameter, and
its free tier (100 requests/month, no card) supports both this handoff's
failure-path unit tests and one deliberately controlled live call. See
`docs/adr/0003-simulator-first-providers.md`'s "When to revisit" — comparing
this adapter's observed behaviour against the simulator's assumed profile
remains future work, not done here.

**Why the live handler can't reuse `CalibratedBoundsPolicy.run`.** That
method takes an `EvalLead` and walks the frozen `CATALOG` via
`arie.providers.catalog.BY_NAME` — a real lead has neither. Adding the live
provider to `CATALOG` was rejected outright: `arie.evalgen.generator` iterates
that tuple to freeze the M0 dataset, so touching it would perturb the frozen
benchmark this project has already published. Instead,
`arie.jobs.handlers._build_live_handlers` is a second, much smaller
acquisition loop (trivial with one provider instead of eight) built from the
same *lead-independent* primitives the simulated policy already sits on —
`arie.scoring.engine.score_evidence` and `ConfidenceModel.predict` both take a
bare `ScoringResult`, never an `EvalLead`. It reuses the exact same
`PostgresEvidenceStore`/`PostgresCostLedger`/`decision_receipts`/`scores`
writes as the simulated path. One explicitly stated, unvalidated assumption:
the confidence model applied to live evidence is the same one calibrated on
synthetic corpus signals — no other calibration data exists, and this is
named rather than treated as equivalent to the simulated path's own
guarantee.

**`PROVIDER_MODE=live` no longer refuses outright.** `build_handlers` now
dispatches on `provider_mode` to `_build_simulated_handlers` (unchanged) or
`_build_live_handlers` (new); an unrecognised mode string still raises
`UnsupportedProviderModeError`. A configured-but-keyless live mode raises
`AbstractCompanyConfigurationError` instead — a different failure ("live mode
is misconfigured") from "live mode doesn't exist," both loud, neither a
silent fallback to the simulator.

**Shadow semantics.** `leads.is_shadow` (migration `0009`, plain boolean,
default `false`) is set once at ingestion — `POST /leads {"mode": "shadow"}`
— and never updated after; a redelivery of the same `(source, external_ref)`
with a different requested mode keeps whatever was persisted the first time,
the same rule every other optional ingestion field already follows. A shadow
lead runs the identical acquisition loop and gets a real `decision_receipts`
row (recommendation, confidence, cost, stop reason, evidence snapshot — all
of it), but `arie.jobs.handlers._finalize_decision` — the one function both
provider-mode handlers share for the DECISION-node branch — routes it to a
new terminal, `LeadStatus.SHADOW_EVALUATED`, instead of calling
`request_review` or applying `DECISION_OUTCOMES`. `SHADOW_EVALUATED` is in
`arie.statemachine.transitions.TERMINAL` (nothing auto-advances it) but
deliberately excluded from every business-semantic group
(`QUALIFIED`/`REJECTED`/`AWAITING_REVIEW`/`FAILURE`/`FINALIZED`) — so
`workflows/n8n/outcome-sync.json`'s FINALIZED-gated sync, `v_pipeline_metrics`,
and `v_escalation_rate` all treat a shadow lead as exactly what it is: not a
business outcome. `v_lead_cost` stays universal (a shadow lead's own receipt
still needs its own cost row); `v_pipeline_metrics`/`v_escalation_rate` gained
a `WHERE NOT is_shadow` filter in the same migration. Zero changes to
`workflows/n8n/*.json` were needed — a status outside their hardcoded
FINALIZED list already falls into the safe "not finalized" branch.

**Cache hits must still be recorded, not skipped — item #5 all over again.**
The live handler's "evidence already fresh, don't call the real API" branch
records a zero-cost, `cache_hit=True` `provider_calls` row rather than
silently doing nothing, mirroring `_DurableCallLedger`'s existing rule for the
simulated path exactly. This is the actual interesting live-provider story:
ARIE deciding it does *not* need to spend real money, not merely that it can
make one real HTTP call — measurable in the receipt, not just asserted.

**Schema.** Migration `0009_live_provider_and_shadow_mode.sql`: one column
(`leads.is_shadow`), and `v_lead_cost`/`v_pipeline_metrics`/`v_escalation_rate`
replaced (additive filter only, no column removed or retyped).
`decision_receipts` (0008) is untouched — the live handler writes
`policy_name = "live_single_provider"` into the same columns the simulated
path already writes, which is also how `arie.api.receipt` decides whether
`providers.not_called` is a set difference against the 8-provider simulated
catalogue or the one-provider live catalogue.

**Test status.** `tests/unit/test_live_abstract_provider.py` covers every
adapter failure mode (missing key, timeout, connection error, 401/403/429/422,
5xx, malformed JSON, a non-dict JSON body, a miss, a partially-unusable field)
against `httpx.MockTransport` — no network, no key, no spend.
`tests/unit/test_jobs_handlers.py` covers build-time dispatch (unrecognised
mode, an injected fake live provider, a missing key via a monkeypatched
config singleton). `tests/integration/test_live_provider_integration.py`
drives a non-corpus lead through the real pipeline with the HTTP layer mocked
(never a real Abstract API call in CI) and pins cache reuse and the no-domain
case. `tests/integration/test_shadow_mode_integration.py` is item 22's
deterministic demonstration: the same corpus lead, once normal (authoritative
`AWAITING_HUMAN` + a real pending review) and once shadow
(`SHADOW_EVALUATED`, zero reviews, identical frozen recommendation), plus a
metrics-exclusion test paired with a normal-mode control so "unchanged" can't
pass vacuously.

**Live verification status.** Depends on whether `ABSTRACT_COMPANY_API_KEY`
was available when this phase's final gate ran — see the session's own final
report for whether `scripts/live_provider_smoke.py` actually executed a real
call, or stopped cleanly with "implementation complete; live verification
blocked pending that key," per the task brief's own explicit instruction not
to fabricate this section.

**Deliberately deferred / non-goals**, restated because they're easy to drift
back into: a second real provider, a provider registry/marketplace, dynamic
provider discovery or an LLM provider-selector, CRM OAuth
(HubSpot/Salesforce), a hosted n8n deployment, frontend changes (`arie-web` is
untouched), authentication, multi-tenancy, billing, Kafka/Redis/Celery/
Temporal/Kubernetes, RAG/vector DB, and any claim of production savings,
better-than-human quality, or shadow-mode superiority — P5 proves the adapter
abstraction works against something real and that ARIE can measure its own
behaviour safely, nothing about real-world economic superiority.

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
- Connect to, configure, or deploy anything against a hosted n8n account
  (cloud or otherwise) without being explicitly asked to in that session. The
  local Docker `n8n` service is the reproducible dev/demo environment this
  repo ships; a hosted instance is a deliberately deferred later integration,
  relevant once ARIE has a public deployed API — not before, and not by
  inference from "n8n" appearing in a task.

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
covered" from a green `make test` alone locally — as of Step 13, CI's
`integration` job does run this suite on every push, against a disposable
`postgres:16-alpine` service container it creates and destroys itself, never
the shared Supabase database. Locally the distinction above still holds.

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
make worker   # python -m arie.jobs.worker  (fits the confidence model at boot,
              # then registers compute_score — simulated providers only)
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
