-- =============================================================================
-- 0021_lead_batches.sql — Productization M3: CSV bulk lead upload, batch
-- tracking.
--
-- Two tables, deliberately holding only what `leads`/`jobs` cannot already
-- answer (per the brief's own "do not duplicate information unnecessarily"
-- instruction):
--
--   * `lead_batches` — static facts fixed at upload time (filename, row
--     counts). No status/progress columns here at all: a batch's live
--     processing state (how many leads are still processing vs. qualified/
--     rejected/awaiting review/failed) is always computed by grouping
--     `leads.status` for `leads.batch_id = this batch` at read time
--     (`arie.batches.batch_progress`) — storing a second, independently
--     updated copy of that count would just be a second place for it to go
--     stale relative to the `leads`/`jobs` state that is already the source
--     of truth.
--   * `lead_batch_rows` — one row per uploaded CSV line, permanent audit
--     history of what was actually in the file and why each row was
--     accepted or rejected at validation time. This *is* worth storing
--     statically: validation happens once, synchronously, during the
--     upload request, and never changes afterward.
--
-- `leads.batch_id` (added below) is what makes the live-computed progress
-- query possible without a join through `lead_batch_rows` — a lead already
-- knows which batch created it, the same way it already knows its
-- `organization_id`.
-- =============================================================================

CREATE TABLE IF NOT EXISTS lead_batches (
    batch_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    filename           TEXT NOT NULL,
    total_rows         INT NOT NULL CHECK (total_rows >= 0),
    accepted_rows      INT NOT NULL CHECK (accepted_rows >= 0),
    rejected_rows      INT NOT NULL CHECK (rejected_rows >= 0),
    created_by_user_id UUID NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT lead_batches_row_counts_consistent CHECK (accepted_rows + rejected_rows = total_rows)
);

-- The batch-listing query (GET /batches), newest first.
CREATE INDEX IF NOT EXISTS idx_lead_batches_org
    ON lead_batches(organization_id, created_at DESC);

CREATE TABLE IF NOT EXISTS lead_batch_rows (
    batch_id          UUID NOT NULL REFERENCES lead_batches(batch_id) ON DELETE CASCADE,
    row_number        INT NOT NULL CHECK (row_number >= 1),
    organization_id   UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    raw_row           JSONB NOT NULL,
    validation_status TEXT NOT NULL CHECK (validation_status IN ('accepted', 'rejected')),
    validation_error  TEXT,
    lead_id           UUID REFERENCES leads(lead_id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (batch_id, row_number),
    CONSTRAINT lead_batch_rows_error_matches_status CHECK (
        (validation_status = 'rejected' AND validation_error IS NOT NULL)
        OR (validation_status = 'accepted' AND validation_error IS NULL)
    ),
    CONSTRAINT lead_batch_rows_lead_id_only_when_accepted CHECK (
        validation_status = 'accepted' OR lead_id IS NULL
    )
);

-- `organization_id` is redundant with `lead_batches.organization_id` (reached
-- via `batch_id`) but carried directly anyway, matching every other
-- tenant-owned table in this schema (`evidence`, `scores`, `provider_calls`,
-- ...) — a direct RLS policy needs no join, and a table whose isolation
-- depends on joining through a parent row is one migration away from a
-- forgotten join producing a silent cross-tenant leak.
CREATE INDEX IF NOT EXISTS idx_lead_batch_rows_org ON lead_batch_rows(organization_id);

-- The "which row produced this lead" reverse lookup a receipt page or a
-- future "find the batch a lead came from" feature would need.
CREATE INDEX IF NOT EXISTS idx_lead_batch_rows_lead
    ON lead_batch_rows(lead_id) WHERE lead_id IS NOT NULL;

ALTER TABLE leads ADD COLUMN IF NOT EXISTS batch_id UUID REFERENCES lead_batches(batch_id) ON DELETE SET NULL;

-- The batch-progress aggregation query (`GROUP BY status WHERE batch_id = ...`).
CREATE INDEX IF NOT EXISTS idx_leads_batch ON leads(batch_id) WHERE batch_id IS NOT NULL;

-- RLS as defense-in-depth, matching every other tenant-owned table. Any
-- active member may read or write both tables — CSV upload is a data-plane
-- action at the same permission tier as `POST /leads` itself (any JWT role
-- can already create leads one at a time; batching them changes nothing
-- about who may do it), unlike `organization_icp_profiles`, which is
-- deliberately owner/admin-only because it changes how *every* future lead
-- is scored, not just the leads a single request creates.
ALTER TABLE lead_batches ENABLE ROW LEVEL SECURITY;
ALTER TABLE lead_batch_rows ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS lead_batches_tenant_isolation ON lead_batches;
CREATE POLICY lead_batches_tenant_isolation ON lead_batches FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS lead_batch_rows_tenant_isolation ON lead_batch_rows;
CREATE POLICY lead_batch_rows_tenant_isolation ON lead_batch_rows FOR ALL
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));
