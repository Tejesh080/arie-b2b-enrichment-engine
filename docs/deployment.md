# Deployment

Written for whoever ships this next, on a platform this repo doesn't prescribe.

---

## What actually gets deployed

Two things, both stateless:

- **The API** (`uvicorn arie.api.main:app`) — one write endpoint, four reads,
  `/healthz`. Horizontally scalable behind any load balancer: no local state,
  no sticky sessions, every write goes through Postgres in one transaction.
- **The worker** (`python -m arie.jobs.worker`) — no HTTP surface at all, a
  long-running process that polls the job queue. Horizontally scalable by
  running more of it: `SKIP LOCKED` means workers need no coordination between
  each other (see [ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md)).

Both are built from the one [`Dockerfile`](../Dockerfile); the command at
deploy time is what picks the role. `docker-compose.yml` runs two worker
replicas locally for exactly this reason — it is a smaller version of the
same production shape, not a different one.

Postgres itself is **Supabase**, not something this repo deploys — see
"Migrations" below for the one path allowed to touch its schema.

Nothing here prescribes a hosting platform. A stateless container that reads
its configuration from the environment runs the same way on Fly.io, Render,
Railway, ECS, or a bare VM with `docker compose` on it; picking one is a
platform decision, not an architecture one. What follows applies to all of
them.

---

## Migrations

`migrations/` is the single source of truth; `scripts/migrate.py` is the
**only** path that ever writes schema to the production database, tracked by
checksum in `schema_migrations` so an edited-after-applied migration fails
loudly instead of silently reapplying or skipping. `supabase/migrations/` is a
generated mirror that exists solely so Supabase's GitHub Branching can
provision PR preview databases with the same schema — CI fails the build if
it drifts from `migrations/`. None of this changes for a deploy; see
[ADR 0005](adr/0005-migration-source-of-truth.md) for the full reasoning,
including why Supabase's own Branching *production* deploy stays off and
`scripts/migrate.py` remains the only production path.

**Deploy ordering is the one thing that actually matters here, and it's the
same ordering `docker-compose.yml` already encodes locally**: migrate, then
start API/worker on the new code — never the other way around. Locally this
is `migrate`'s `depends_on: db: condition: service_healthy` plus
`api`/`worker`'s `depends_on: migrate: condition: service_completed_successfully`
(the exact fix for the clean-start race Step 12 found: workers booting before
the schema existed). A platform without Compose's dependency graph needs the
same ordering expressed as a **pre-deploy / release step**: run
`python scripts/migrate.py --target production --apply --confirm-production-write`
(with `DATABASE_DIRECT_URL` set — the *direct* connection, not the pooled one;
see the script's own docstring for why a transaction-mode pooler can't be
trusted with DDL, and why the runner refuses a production `--apply` that could
only resolve the pooled URL) to completion, and only then roll out the new
API/worker revision. Every flag is mandatory: the runner has no default target
and no default mode, so `python scripts/migrate.py` on its own is an error
rather than a production write. To see what a deploy *would* run first, without
writing anything, use `python scripts/migrate.py --target production --dry-run`. Most container platforms have a
named concept for this (a "release phase," "pre-deploy command," or
"one-off task run before rollout"); use whichever one the target platform
offers rather than running migrations from inside the API or worker
processes themselves.

Every migration through `0006` is written idempotent (`CREATE TABLE IF NOT
EXISTS`, `CREATE OR REPLACE VIEW`, `ADD COLUMN IF NOT EXISTS`) — verified, not
assumed, per ADR 0005's own account of re-running `0001`–`0003` directly
against production with zero side effects. That property is what makes a
rolling deploy safe even if old and new containers briefly overlap: an
already-applied migration re-running is a no-op, and `scripts/migrate.py`
records nothing as applied unless it actually committed. Keep future
migrations idempotent the same way — it's a convention, not something enforced
by the tooling.

---

## Configuration and secrets

Everything either process reads from the environment is enumerated in
[`.env.example`](../.env.example) — copy it, never commit the copy (`.env` and
`.env.*` are gitignored; only `.env.example` is tracked). In production,
secrets come from whatever the hosting platform's secret manager is (or CI
secrets, for the pipeline that runs the migration step) — never a file
checked into this repo, and never a build argument baked into the image (the
`Dockerfile` accepts none, on purpose: an image that requires secrets to
*build* would have them sitting in a layer forever).

The two settings both processes actually require:

- `DATABASE_URL` — the **pooled** connection string (Supabase's Transaction
  Pooler, port 6543 once anything needs concurrent-connection headroom beyond
  the Session Pooler; see `docs/architecture.md`'s Environment section for
  the IPv6-only direct-host caveat that makes the Session Pooler double as
  both today).
- `PROVIDER_MODE` — `simulated` is the only value with an implemented backend
  (`live` refuses loudly at worker startup; see ADR 0003). Deploying with
  anything else configured is a configuration bug the worker will refuse to
  start under, not a silent no-op.

`DATABASE_DIRECT_URL` is needed only by the migration step, never by the
running API or worker — don't hand it to either process's runtime
environment beyond what the release step uses.

**Nothing is ever logged.** `arie.llm.deepseek` puts the API key in an
`Authorization` header and nowhere else; every `print()` in `arie.jobs.worker`
and `scripts/migrate.py` emits job/migration bookkeeping, never a connection
string or key. If a future change adds logging near either, keep it that way
— grep for `DATABASE_URL`/`_API_KEY` before adding a new log line near
connection setup.

---

## Health checks

`GET /healthz` reports three states, not one, because "database reachable"
and "schema fully migrated" are different failures that call for different
fixes — see `arie.api.main.healthz`'s own docstring:

| `status` | `database` | `schema_ready` | HTTP | Meaning |
|---|---|---|---|---|
| `ok` | `true` | `true` | 200 | Ready for traffic. |
| `degraded` | `true` | `false` | 503 | Reachable, but a migration hasn't finished — wait for the release step, don't restart the process. |
| `down` | `false` | `false` | 503 | Database unreachable. |

Point a load balancer's health check and any container-orchestrator readiness
probe at this endpoint. The `Dockerfile`'s own `HEALTHCHECK` already does —
`docker inspect`/`docker compose ps` report the API container `healthy` only
once it returns `ok`, and `docker-compose.yml` uses that to gate the (opt-in,
dev-only) `n8n` service on `condition: service_healthy` rather than merely
"started." A platform that reads a container's built-in `HEALTHCHECK` (many
do, for restart and rollout decisions) gets this for free; one that wants its
own probe definition should point it at the same `/healthz`.

The worker has no HTTP surface and is explicitly excluded from the image's
`HEALTHCHECK` (`docker-compose.yml`'s `worker` service disables it — nothing
listens on :8000 there). Its liveness signal is the process itself: a crashed
worker exits non-zero and should be restarted by the platform's normal
process-supervision (`restart: unless-stopped` locally; the equivalent
"restart on failure" policy wherever this runs) — not a synthetic health
endpoint bolted on to satisfy a check that doesn't fit its shape.

---

## Shutdown

Both processes handle `SIGTERM` (what `docker stop`, `compose down`, and
every container platform's rolling-deploy drain send — not `SIGINT`/Ctrl-C,
which is a local-dev convenience) by finishing in-flight work and closing
their connection pool before exiting, rather than being killed mid-transaction:

- The API's shutdown runs through Uvicorn's own graceful-shutdown handling —
  finish in-flight requests, then run the FastAPI lifespan's shutdown
  (`state.pool.close()`, `shutdown_tracing()`).
- The worker installs its own handler
  (`arie.jobs.worker.install_graceful_shutdown`) because a raw polling loop
  has no framework doing this for it: `SIGTERM`'s default disposition is
  immediate termination, which would skip cleanup entirely. The handler sets
  a `threading.Event`; the loop finishes whatever `run_worker_cycle` already
  claimed (never abandons a job mid-transaction) before checking whether to
  stop, then closes the queue, the pool, and the tracer.

Verified against a real container, not just read: `docker stop -t 10` on both
roles exits with code 0 in under a second, well inside the grace period,
with the worker's own "Worker stopping." reaching its logs before it does.
Give a rolling deploy at least a few seconds of grace beyond the default
`WORKER_POLL_INTERVAL_SEC` (2s) — the worker checks for the stop signal
between poll cycles, not instantly, though the signal itself interrupts the
wait rather than waiting out the full interval.

---

## Hosted on Railway (P6)

Everything above is platform-agnostic by design; this section is the one
concrete instance of it — Railway for compute, the *existing* Supabase Pro
project for Postgres. P6 provisions no new database: no Railway Postgres, no
migration off Supabase.

**Two services, one repo, one image.** `arie-api` and `arie-worker` both
build from this repo's [`Dockerfile`](../Dockerfile); only the run-time
command differs, exactly as `docker-compose.yml` already models locally.

- **`arie-api`** — Dockerfile build, no start-command override needed:
  Railway assigns its own `PORT`, and the Dockerfile's `CMD` reads it via
  `${PORT:-8000}` (see the Dockerfile's own comment) so this works without
  changing what every local Compose service still gets — none of them set
  `PORT`, so they keep getting 8000. Public HTTPS domain generated by
  Railway:
  `https://adaptive-revenue-intelligence-engine-production.up.railway.app`
  (project `courageous-success`, environment `production`). Healthcheck
  Path = `/healthz`, the same three-state check
  described above — a `degraded` (503, schema not fully migrated) response
  correctly blocks traffic promotion rather than Railway treating any
  response as "started, good enough." **Pre-Deploy Command** =
  `python scripts/migrate.py --target production --apply --confirm-production-write`
  (this changed in Productization M6 — an older single-word
  `python scripts/migrate.py` now exits non-zero and would block every
  deploy until the Railway setting is updated): Railway runs this in an
  isolated container
  and blocks cutover until it exits 0 — the platform's release-phase
  primitive the Migrations section above asks for. Needs
  `DATABASE_DIRECT_URL`; nothing else does. On this project, that variable's
  value is the **same Session Pooler string** as `DATABASE_URL` below, not
  Supabase's plain `db.<ref>.supabase.co` host — that host is IPv6-only and
  unreachable from Railway, the exact caveat `docs/architecture.md` already
  documents. This is safe, not a workaround: `scripts/migrate.py` opens one
  plain `psycopg.connect()` and runs each migration in its own transaction on
  that single session, which is exactly what the Session Pooler's session-mode
  pgbouncer provides. The property that connection actually needs and the
  *Transaction* Pooler (port 6543) doesn't guarantee is session-level DDL
  semantics — see that script's own module docstring — and the Session Pooler
  was never in question on that count.
- **`arie-worker`** — same repo, same Dockerfile, **Custom Start Command**
  override = `python -m arie.jobs.worker`. No public domain, no Healthcheck
  Path (nothing listens on a port — the Dockerfile's `HEALTHCHECK` comment
  already explains why that's deliberate), no Pre-Deploy Command. Running
  migrations only from `arie-api` avoids both services racing to apply the
  same migration on every deploy; skipping it here is safe only because
  every migration in this repo is idempotent — the same property that
  already makes a rolling deploy safe if old and new containers briefly
  overlap (see Migrations above). A future migration that can't be made
  idempotent would need this reconsidered.

**Database.** Both services use the existing Supabase Pro project's
**Session Pooler** connection string for `DATABASE_URL` — the same one local
dev already uses, per the IPv6-only-direct-host caveat in
`docs/architecture.md`. Set once as a Railway **Shared Variable** and
referenced by both services rather than pasted twice. The pooler choice
didn't change for Railway: nothing surfaced a reason the Transaction Pooler
is needed instead, and switching would first need `prepare_threshold=None`
hardening (psycopg3's server-side auto-prepare is unsafe under pgbouncer's
transaction-mode pooling) that isn't in place today.

**Environment variables — names only; values live in Railway's Variables UI,
never in this repo.**

| Variable | Where | Notes |
|---|---|---|
| `DATABASE_URL` | shared | Session Pooler string |
| `DATABASE_DIRECT_URL` | `arie-api` only | consumed only by the Pre-Deploy Command |
| `PROVIDER_MODE` | `arie-worker` | start at `simulated` — see "Provider safety" below |
| `ABSTRACT_COMPANY_API_KEY`, `ABSTRACT_COMPANY_BASE_URL`, `ABSTRACT_COMPANY_TIMEOUT_SECONDS`, `ABSTRACT_COMPANY_COST_USD_PER_CALL` | `arie-worker`, optional | read only when `PROVIDER_MODE=live` |
| `LIVE_PROVIDER_DAILY_BUDGET_USD`, `LIVE_PROVIDER_PER_LEAD_BUDGET_USD` | `arie-worker`, optional | live spend ceilings; safe defaults apply if unset. Server-side only — never expose to a browser |
| `LEAD_BUDGET_USD_CAP`, `TARGET_AUTONOMOUS_ERROR_RATE`, `LATENCY_PENALTY_USD_PER_SEC`, `WORKER_POLL_INTERVAL_SEC`, `WORKER_MAX_ATTEMPTS`, `WORKER_LEASE_SECONDS` | `arie-worker`, optional | policy/runtime defaults apply if unset |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME` | shared, optional | tracing stays off if unset |

`APOLLO_API_KEY` and `HUNTER_API_KEY` **are** read on any worker running
`PROVIDER_MODE=live` — that mode builds every registered adapter (Abstract,
Hunter, Apollo) at startup and refuses to start with any key missing, rather
than silently running a thinner pipeline that still reports coverage and cost.
`LIVE_PROVIDER_STRATEGY`, `LIVE_PROVIDER_ORDER`,
`LIVE_EVALUATION_PER_LEAD_BUDGET_USD`, and
`LIVE_PROVIDER_QUOTA_COOLDOWN_SECONDS` are likewise live-only (see
`.env.example`); the simulated public demo structurally never reads them.
`DEEPSEEK_API_KEY` and the Firecrawl/Langfuse/OpenAI/Supabase-client
placeholders in `.env.example` are not read by any path the API or worker
actually run — do not configure them on Railway.

**Provider safety.** Deploy with `PROVIDER_MODE=simulated` first and prove
the hosted core — API, worker, Supabase, migrations, health, lead processing
— end to end before ever touching `PROVIDER_MODE=live`: flipping it makes
the worker call the real Abstract API — and, when company evidence leaves the
decision open, the real Hunter and then Apollo APIs — for *any* ingested lead,
not only corpus identities. Never set `LIVE_PROVIDER_STRATEGY=evaluation_parallel`
on a deployment serving anonymous traffic: it deliberately buys overlapping
person evidence per lead and exists only for controlled private experiments. To return to zero real-provider spend, set
`PROVIDER_MODE=simulated` on `arie-worker` and redeploy it; nothing else
needs to change.

Three things bound the blast radius of `PROVIDER_MODE=live`, all enforced in
code rather than by configuration discipline (see
[`provider-integration.md`](provider-integration.md)'s "Live V1 Foundation"):

- **No autonomous business action.** A lead enriched by a real provider always
  terminates at `AWAITING_HUMAN` (or `SHADOW_EVALUATED` for a shadow lead) —
  never `AUTO_ROUTED`, never the reject terminal — because the confidence
  threshold gating autonomy is calibrated on synthetic data. Not a flag.
- **Spend ceilings**, checked against the durable ledger before each call:
  `LIVE_PROVIDER_DAILY_BUDGET_USD` (default `$2.00`) and
  `LIVE_PROVIDER_PER_LEAD_BUDGET_USD` (default `$0.05`).
- **Failures degrade rather than propagate.** A provider timeout, error, or
  exhausted budget stops acquisition with its own stop reason and sends the
  lead to a human; the job is not failed and the lead is not lost.

**A deployed worker polls this database, and the integration suite can no
longer reach it.** `arie-worker` claims `compute_score` jobs within about a
second of ingestion. While the integration fixtures read `DATABASE_URL`, that
meant two things at once: the suite wrote and deleted rows in the deployed
database, and the deployed worker processed the tests' leads with its own
handlers, so assertions failed for reasons unrelated to the code under test.

The fix is structural rather than procedural — "scale the worker to zero before
running tests" is a step someone forgets. The integration fixtures now read
**`TEST_DATABASE_URL`, with no fallback to `DATABASE_URL`**, plus two further
guards (`ARIE_ALLOW_INTEGRATION_TEST_DB=1`, and a designation marker only
`make test-db` creates — a command that refuses to stamp anything matching
`DATABASE_URL` or already holding data). An unset `TEST_DATABASE_URL` skips the
suite; it never silently falls through to a deployment.

Locally that means a *different database on the same Postgres* than the Compose
stack uses — `arie_test` versus `arie`. The Compose workers poll `arie`, whose
`jobs` table is a different table, so the whole stack can stay running during a
test run. See [`.env.example`](../.env.example) and `scripts/test_db.py`.

`arie-worker` therefore needs no special handling around test runs, and
`DATABASE_URL` should never be set on a developer machine for testing purposes
— it is a deployment variable.

**Rollback.** Railway keeps prior deploys; redeploying an older one is the
rollback path, per service, independently. Every migration through `0009`
is idempotent and additive-only, so rolling either service back to an older
image never breaks against a newer schema — it simply doesn't use whatever
columns or tables a later migration added.

**Verified end-to-end against the live deployment.** Beyond `/healthz`
returning `ok`, a full hosted lead lifecycle has actually been exercised
through the public URL above, against the real `arie-worker` consuming the
real Supabase-backed queue: a corpus identity ingested via `POST /leads`
autonomously reached `AUTO_ROUTED` with a Decision Receipt publicly readable
at `GET /leads/{id}/receipt`; a second identity escalated to `AWAITING_HUMAN`,
was approved via `POST /reviews/{id}/decision`, and kept its original machine
recommendation (`reject`) visible in the receipt alongside the human action
and the final `AUTO_ROUTED` outcome — the three never collapse into one
field. A `mode: "shadow"` lead computed a full recommendation with `shadow:
true` and no authoritative routing or review. All of this was then confirmed
to survive an `arie-worker` redeploy: the same lead and receipt, refetched
afterward, were unchanged — proof the state lives in Supabase, not the
worker container.

**One small concurrency check**, deliberately bounded (this is not load
testing — see [`portfolio.md`](portfolio.md) for what that limitation
does and doesn't mean): 5 leads at the same identity, submitted
simultaneously against the public API. All 5 reached `AUTO_ROUTED`, each
with a consistent version history (no lost or duplicated transitions) and
identical evidence — 2 fresh provider calls plus 5 cache hits each.

Stated precisely: five concurrent hosted submissions completed successfully
without duplicate terminal processing, and the evidence cache stayed
consistent across them. That is the observation. It is *consistent with*
`SKIP LOCKED` serializing the claims correctly, but this run captured no
direct contention evidence — no measured lock waits, no trace of two workers
racing the same row — so it is not on its own proof that `SKIP LOCKED` is
what produced the outcome. The mechanism is covered by the queue's own unit
and integration tests; this check confirms the deployed system behaves
correctly under a small amount of real concurrency.

---

## What this deliberately doesn't cover

- **Kubernetes, Temporal, Redis, or Celery.** None of them are load-bearing
  for a single-service Postgres-queue system at this scale — see
  [ADR 0002](adr/0002-postgres-queue-not-temporal-or-redis.md) — and adding
  one wouldn't be a deployment decision, it would be an architecture change
  this project has already deliberately declined.
- **Standing up an n8n account from this repo.** n8n Cloud *is* connected and
  verified against the public API above — the same two edge workflows, pointed
  at the Railway URL instead of Docker network names, driven end to end:
  webhook ingestion → `POST /leads` → Supabase queue → worker → terminal
  decision → Decision Receipt → Outcome Sync → mock CRM sink, with the final
  Outcome Sync returning `synced: true` and the sink echoing the payload back.
  What this document does not cover is *provisioning* it: that lives entirely
  in that account's own UI, is not deployed or contained by this repo, and has
  no environment variables or rollback path here. The local Docker `n8n`
  service remains, deliberately — a reproducible zero-credential demo is a
  different thing from the real integration target, and both are wanted.
- **`PROVIDER_MODE=live` by default.** Real adapters have existed since P5
  (`arie.providers.live_abstract`, Abstract API's Company Enrichment; and now
  `arie.providers.live_apollo`, Apollo People Enrichment) and are
  production-capable — the "no real adapter exists yet (ADR 0003)" framing
  this bullet used to carry no longer describes the code. What's still true:
  the *hosted* deployment starts in `simulated` mode deliberately, not
  because `live` doesn't work — see "Provider safety" above.
- **Autonomous decisions on real-provider evidence.** Deliberately blocked in
  code until the confidence model is validated and recalibrated against real
  evidence. Live mode enriches, scores, and recommends; a human decides. The
  work that would lift this is a measurement, not a configuration change.
