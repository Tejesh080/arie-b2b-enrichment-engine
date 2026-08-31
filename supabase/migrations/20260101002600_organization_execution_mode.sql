-- =============================================================================
-- 0027_organization_execution_mode.sql — Productization M5 Part 14: the
-- organization-level switch that gates real provider execution, separate
-- from (and stricter than) the process-wide PROVIDER_MODE env var.
--
-- `PROVIDER_MODE=live` (arie.config.RuntimeConfig) decides whether a worker
-- process runs the live acquisition code path at all. `execution_mode` below
-- decides, per organization, what that code path is allowed to DO once it's
-- running: acquire zero real evidence ('simulated' — the only value that
-- exists before this migration and the default after it), acquire real
-- evidence but never let it become operative ('live_shadow'), or acquire
-- real evidence and route every non-shadow outcome through mandatory human
-- review ('live_human_only'). No value here ever enables autonomous action —
-- `arie.live.safety.LIVE_AUTONOMY_ENABLED` stays a separate, hardcoded
-- `False` this migration does not touch.
--
-- Defaults to 'simulated' for every existing organization — this migration
-- adds no backfill step; `ADD COLUMN ... NOT NULL DEFAULT` applies the
-- default to every existing row atomically, so no organization becomes live
-- by migration. Moving off 'simulated' is a deliberate, audited, owner/admin
-- -only action (`arie.organizations.set_execution_mode`).
-- =============================================================================

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS execution_mode TEXT NOT NULL DEFAULT 'simulated'
        CHECK (execution_mode IN ('simulated', 'live_shadow', 'live_human_only'));
