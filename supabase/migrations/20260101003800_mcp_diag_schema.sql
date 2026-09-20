-- =============================================================================
-- 0039_mcp_diag_schema.sql — MCP Engineering Interface V0.1, vertical slice 1
-- (specs/mcp-engineering-interface.md).
--
-- Creates exactly what `get_system_health`, `inspect_job`, and
-- `list_failed_jobs` need and nothing else. Later MCP tool slices add their
-- own views in their own additive migrations — the same one-thing-at-a-time
-- discipline every prior migration in this file's ancestry (0010, 0011,
-- 0028, 0029) already follows, rather than pre-declaring a schema for tools
-- that don't exist yet.
--
-- Security shape (spec's Decision 3): every `mcp_diag` object gets its own
-- explicit `GRANT SELECT`, one statement per view. There is deliberately no
-- `ALTER DEFAULT PRIVILEGES` here — a future view is invisible to
-- `arie_mcp_readonly` until a human migration grants it, on purpose, so
-- "what can this role see" is always answerable by reading this file's
-- history rather than a standing default that could silently widen.
--
-- No password is ever set for `arie_mcp_readonly` here. `CREATE ROLE ...
-- LOGIN` with no password means the role exists but cannot authenticate
-- until an operator runs `ALTER ROLE arie_mcp_readonly PASSWORD '...'`
-- out-of-band, per environment (local dev, CI, staging, prod) — the same
-- reason no migration in this repo has ever set the main `arie` role's
-- password either. Shipped inert; activated deliberately.
--
-- Must stay re-runnable against a database that already has it (ADR 0005).
-- =============================================================================

-- ---------------------------------------------------------------- the role --

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'arie_mcp_readonly') THEN
        CREATE ROLE arie_mcp_readonly LOGIN;
    END IF;
END $$;

-- Belt-and-suspenders: even a future grant mistake in this file cannot
-- produce a write, because the session itself refuses one. A role-level
-- default (as opposed to a per-session SET) is applied when the Postgres
-- backend session starts, which — per the spec's Decision 4 — is the
-- property being relied on for the statement_timeout default below too.
-- Decision 4 also asks for explicit per-connection enforcement as defense in
-- depth in case a pooler doesn't preserve this; `src/arie_mcp/db.py` does
-- that unconditionally rather than assuming this default is honoured.
ALTER ROLE arie_mcp_readonly SET default_transaction_read_only = on;
ALTER ROLE arie_mcp_readonly SET statement_timeout = '5000ms';

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO arie_mcp_readonly', current_database());
END $$;

-- vault schema/tables: Supabase's Vault extension is not installed on a
-- plain local/CI Postgres (`postgres:16-alpine`), the same reason
-- `migrations/0012_organizations_and_members.sql` gives for not FK-ing
-- `organization_members.user_id` to `auth.users`. Guarded so this migration
-- stays re-runnable in both environments. Where vault *does* exist
-- (production Supabase), arie_mcp_readonly is never granted anything on it
-- in the first place — this is documentation of intent, not a privilege
-- this role would otherwise have reached.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = 'vault') THEN
        EXECUTE 'REVOKE ALL ON SCHEMA vault FROM arie_mcp_readonly';
        EXECUTE 'REVOKE ALL ON ALL TABLES IN SCHEMA vault FROM arie_mcp_readonly';
    END IF;
END $$;

-- -------------------------------------------------------------- the schema --

CREATE SCHEMA IF NOT EXISTS mcp_diag;
REVOKE ALL ON SCHEMA mcp_diag FROM PUBLIC;
GRANT USAGE ON SCHEMA mcp_diag TO arie_mcp_readonly;

-- ---------------------------------------------------------------- the views --

-- get_system_health: is the schema current.
CREATE OR REPLACE VIEW mcp_diag.v_schema_migrations AS
SELECT filename, checksum, applied_at
FROM public.schema_migrations;

GRANT SELECT ON mcp_diag.v_schema_migrations TO arie_mcp_readonly;

-- get_system_health: queue depth by status. No PII — `jobs` carries no
-- person/company/lead-content columns of its own.
CREATE OR REPLACE VIEW mcp_diag.v_queue_depth AS
SELECT status, count(*) AS job_count
FROM public.jobs
GROUP BY status;

GRANT SELECT ON mcp_diag.v_queue_depth TO arie_mcp_readonly;

-- get_system_health: worker fleet liveness — the same table `GET
-- /healthz/worker` already reads via `arie.jobs.heartbeat.fleet_status`.
-- `hostname`/`pid` are infrastructure identifiers, not personal data.
CREATE OR REPLACE VIEW mcp_diag.v_worker_heartbeats AS
SELECT worker_instance_id, hostname, pid, started_at, last_seen_at
FROM public.worker_heartbeats;

GRANT SELECT ON mcp_diag.v_worker_heartbeats TO arie_mcp_readonly;

-- inspect_job: one job plus its parent lead's status/is_shadow/
-- organization_id ONLY — never persons/companies, so no name/email/domain
-- is reachable through this view under any join. `last_error` is truncated
-- to 500 chars in the view itself (defense in depth on top of the
-- application-layer cap in `src/arie_mcp/limits.py`) — it is expected to be
-- this codebase's own exception text, not vendor payload, but is capped
-- rather than assumed safe either way.
CREATE OR REPLACE VIEW mcp_diag.v_job_detail AS
SELECT
    j.job_id,
    j.lead_id,
    j.job_type,
    j.status,
    j.attempt_count,
    j.next_retry_at,
    j.locked_by,
    j.locked_at,
    left(j.last_error, 500) AS last_error,
    j.created_at,
    l.status           AS lead_status,
    l.is_shadow         AS lead_is_shadow,
    l.organization_id   AS lead_organization_id
FROM public.jobs j
LEFT JOIN public.leads l ON l.lead_id = j.lead_id;

GRANT SELECT ON mcp_diag.v_job_detail TO arie_mcp_readonly;

-- list_failed_jobs: same PII-free column set as v_job_detail, pre-filtered
-- to the two failure statuses so the tool layer never has to trust a
-- caller-supplied WHERE clause against a wider view.
CREATE OR REPLACE VIEW mcp_diag.v_failed_jobs AS
SELECT
    j.job_id,
    j.lead_id,
    j.job_type,
    j.status,
    j.attempt_count,
    j.next_retry_at,
    j.locked_by,
    j.locked_at,
    left(j.last_error, 500) AS last_error,
    j.created_at
FROM public.jobs j
WHERE j.status IN ('failed', 'dead_letter');

GRANT SELECT ON mcp_diag.v_failed_jobs TO arie_mcp_readonly;
