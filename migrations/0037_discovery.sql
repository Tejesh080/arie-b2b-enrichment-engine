-- =============================================================================
-- 0037_discovery.sql — Product Pivot: market discovery.
--
-- Two tables. `discovery_runs` is the customer's "find opportunities" click —
-- one row per run, its own lifecycle, and the funnel counts
-- `arie.discovery.models.DiscoveryFunnel` reports back (stored as JSONB
-- rather than a dozen columns: the funnel's own shape belongs to the
-- application, not the schema, and every field in it is already an integer
-- or a modelled USD figure with no query that needs to filter on one).
--
-- `discovery_candidates` is the provenance trail the pivot brief explicitly
-- asks to stay queryable: every raw search result this run kept after
-- deduplication, its screening verdict, and — for a survivor — the
-- `leads` row it became. Nothing here duplicates `leads`, `companies`, or
-- `evidence`; a promoted candidate's score, evidence, and decision live
-- exactly where every other lead's already does.
-- =============================================================================

CREATE TABLE IF NOT EXISTS discovery_runs (
    run_id                       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id              UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    profile_version               INT,
    status                        TEXT NOT NULL DEFAULT 'draft'
                                      CHECK (status IN (
                                          'draft', 'planning', 'discovering', 'screening',
                                          'promoting', 'researching', 'complete', 'failed', 'cancelled'
                                      )),
    requested_opportunity_count  INT NOT NULL CHECK (requested_opportunity_count > 0),
    market                        TEXT,
    max_candidates                INT NOT NULL CHECK (max_candidates > 0),
    created_by_user_id           UUID,
    error_detail                  TEXT,
    funnel                        JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at                    TIMESTAMPTZ,
    completed_at                  TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_discovery_runs_org
    ON discovery_runs(organization_id, created_at DESC);

CREATE TABLE IF NOT EXISTS discovery_candidates (
    candidate_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id            UUID NOT NULL REFERENCES discovery_runs(run_id) ON DELETE CASCADE,
    organization_id   UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    company_name      TEXT NOT NULL,
    domain            TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    snippet           TEXT,
    source_provider   TEXT NOT NULL,
    search_query      TEXT NOT NULL,
    screening_class   TEXT CHECK (screening_class IN ('promising', 'possible', 'unlikely', 'insufficient_info')),
    screening_reason  TEXT,
    -- ON DELETE SET NULL, not CASCADE: a discovery candidate's provenance
    -- ("this is where the lead came from") should survive the lead itself
    -- being deleted, the same reasoning `lead_batches` applies to
    -- `lead_batch_rows.lead_id`.
    promoted_lead_id  UUID REFERENCES leads(lead_id) ON DELETE SET NULL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- One row per (run, domain) — the dedupe boundary this table enforces
    -- structurally rather than trusting every caller to have already applied
    -- `arie.discovery.dedupe` correctly.
    UNIQUE (run_id, domain)
);

CREATE INDEX IF NOT EXISTS idx_discovery_candidates_run
    ON discovery_candidates(run_id);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_org
    ON discovery_candidates(organization_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_discovery_candidates_lead
    ON discovery_candidates(promoted_lead_id) WHERE promoted_lead_id IS NOT NULL;

ALTER TABLE discovery_runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE discovery_candidates ENABLE ROW LEVEL SECURITY;

-- Any active member may read and start a discovery run — matching
-- `lead_recommendation_feedback`'s bar, not the owner/admin-only bar a
-- profile write requires. The application layer (`_require_jwt_session` on
-- the API routes) decides which authenticated sessions may write; RLS here
-- is defence-in-depth at the organization boundary like every other table.
DROP POLICY IF EXISTS discovery_runs_select ON discovery_runs;
CREATE POLICY discovery_runs_select ON discovery_runs FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS discovery_runs_insert ON discovery_runs;
CREATE POLICY discovery_runs_insert ON discovery_runs FOR INSERT
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS discovery_runs_update ON discovery_runs;
CREATE POLICY discovery_runs_update ON discovery_runs FOR UPDATE
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS discovery_candidates_select ON discovery_candidates;
CREATE POLICY discovery_candidates_select ON discovery_candidates FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS discovery_candidates_insert ON discovery_candidates;
CREATE POLICY discovery_candidates_insert ON discovery_candidates FOR INSERT
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS discovery_candidates_update ON discovery_candidates;
CREATE POLICY discovery_candidates_update ON discovery_candidates FOR UPDATE
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));
