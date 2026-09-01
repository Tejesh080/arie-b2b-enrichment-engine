-- =============================================================================
-- 0032_worker_heartbeats.sql — Productization M6 Part 28: a DB-backed
-- liveness signal for `arie.jobs.worker`, independent of `/healthz` (which
-- proves the API can reach the database and the schema is current — nothing
-- about whether anything is actually consuming the job queue).
--
-- No `organization_id` — a worker process is not tenant data, the same
-- reasoning `migrations/0012_organizations_and_members.sql`'s own docstring
-- gives for leaving `jobs`/`lead_events` without one. Not RLS-protected for
-- the same reason those two tables aren't: nothing here is ever read on a
-- customer-facing path, only aggregated into `GET /healthz/worker`'s
-- freshness computation.
--
-- One row per worker *process* (`worker_instance_id` — see
-- `arie.jobs.worker`'s existing `socket.gethostname()`-derived instance
-- prefix, already used in its structured logs), upserted on every heartbeat
-- tick rather than appended — this is current liveness, not a history table.
-- A crashed worker simply stops updating its row; `last_seen_at` going stale
-- *is* the down signal, there is no separate "worker exited cleanly" state to
-- model.
-- =============================================================================

CREATE TABLE IF NOT EXISTS worker_heartbeats (
    worker_instance_id  TEXT PRIMARY KEY,
    hostname             TEXT NOT NULL,
    pid                   INT,
    started_at            TIMESTAMPTZ NOT NULL,
    last_seen_at          TIMESTAMPTZ NOT NULL
);
