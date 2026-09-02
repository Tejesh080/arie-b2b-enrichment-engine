-- =============================================================================
-- 0035_profile_revision_proposals.sql — M7 Slice 3: targeting changes ARIE
-- suggests and a human decides on.
--
-- **Why this is a table and Slice 2's provenance was not.** A generated
-- targeting profile's provenance is immutable for exactly as long as the row
-- it describes, so it lives inside `organization_icp_profiles.config` and
-- needs no storage of its own. A proposal is the opposite: it has a lifecycle
-- (proposed -> accepted or rejected), it outlives the request that produced
-- it, a customer may come back to it days later, and it must name the exact
-- profile version it was reasoning about so that accepting a stale proposal
-- is detectable rather than silent. None of that fits inside another row's
-- JSON.
--
-- **A proposal changes nothing on its own.** There is no trigger here, no
-- foreign key that cascades into scoring, and no path from this table into
-- `organization_icp_profiles` except a human accepting it, which goes through
-- `arie.icp_profiles.create_profile` like every other profile version and
-- produces a new immutable row. Rejecting one writes only to this table.
--
-- `profile_id` is the version the proposal was computed against, NOT a version
-- it modifies. ON DELETE CASCADE because a proposal about a profile that no
-- longer exists is meaningless — though nothing in the application deletes a
-- profile, so this is a statement of intent more than a live code path.
--
-- `proposal` and `supporting_statistics` are validated JSON documents
-- (`arie.intelligence.proposals`), never arbitrary or executable — the same
-- rule `organization_icp_profiles.config` states for itself. The statistics
-- are the deterministic aggregates from `arie.intelligence.outcomes`, kept so
-- a customer reading a three-day-old proposal sees the numbers it was actually
-- based on rather than a recomputation against data that has since changed.
--
-- **What is deliberately not stored: the historical dataset itself.** A
-- customer's list of who they won and lost is theirs, it is not needed to act
-- on a proposal, and a table nothing ever deletes is the wrong place for it.
-- Only the aggregates survive.
--
-- `created_by_user_id`/`resolved_by_user_id` carry no foreign key to
-- `auth.users`, matching `organization_icp_profiles.created_by_user_id` and
-- every other user reference in this schema (see
-- `migrations/0012_organizations_and_members.sql`'s note).
-- =============================================================================

CREATE TABLE IF NOT EXISTS profile_revision_proposals (
    proposal_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id        UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    profile_id             UUID NOT NULL REFERENCES organization_icp_profiles(profile_id) ON DELETE CASCADE,
    profile_version        INT NOT NULL CHECK (profile_version >= 1),
    source                 TEXT NOT NULL CHECK (source IN ('historical_outcomes', 'user_feedback')),
    status                 TEXT NOT NULL DEFAULT 'proposed'
                               CHECK (status IN ('proposed', 'accepted', 'rejected')),
    summary                TEXT NOT NULL,
    proposal               JSONB NOT NULL,
    supporting_statistics  JSONB NOT NULL,
    evidence_strength      TEXT NOT NULL
                               CHECK (evidence_strength IN
                                      ('insufficient_data', 'weak', 'moderate', 'strong')),
    sample_size            INT NOT NULL CHECK (sample_size >= 0),
    created_by_user_id     UUID,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    resolved_by_user_id    UUID,
    resolved_at            TIMESTAMPTZ,
    resulting_profile_id   UUID REFERENCES organization_icp_profiles(profile_id) ON DELETE SET NULL,
    -- An unresolved proposal has no resolver and no resolution time; a resolved
    -- one has both. Enforced here rather than trusted to the application, so a
    -- proposal can never read as "accepted by nobody at no time".
    CONSTRAINT profile_revision_proposals_resolution_consistent CHECK (
        (status = 'proposed' AND resolved_at IS NULL AND resolved_by_user_id IS NULL)
        OR (status <> 'proposed' AND resolved_at IS NOT NULL)
    ),
    -- Only an accepted proposal can name a resulting profile version.
    CONSTRAINT profile_revision_proposals_result_only_when_accepted CHECK (
        resulting_profile_id IS NULL OR status = 'accepted'
    )
);

-- The listing every console screen makes: this organization's open proposals,
-- newest first. Partial on `status` because a resolved proposal is history and
-- is read by id, not by scanning.
CREATE INDEX IF NOT EXISTS idx_profile_revision_proposals_open
    ON profile_revision_proposals(organization_id, created_at DESC)
    WHERE status = 'proposed';

CREATE INDEX IF NOT EXISTS idx_profile_revision_proposals_org
    ON profile_revision_proposals(organization_id, created_at DESC);

-- RLS as defence in depth, matching `organization_icp_profiles` exactly and
-- for the same reasons. Any active member may read what ARIE has suggested —
-- seeing a suggestion is not a privileged act — but only owner/admin may
-- create, accept or reject one, because accepting is a profile write.
-- No DELETE policy is defined at all: proposals are permanent history, and
-- RLS's deny-if-no-policy-matches means even a non-bypassing role cannot
-- remove one.
ALTER TABLE profile_revision_proposals ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS profile_revision_proposals_select ON profile_revision_proposals;
CREATE POLICY profile_revision_proposals_select ON profile_revision_proposals FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS profile_revision_proposals_admin_insert ON profile_revision_proposals;
CREATE POLICY profile_revision_proposals_admin_insert ON profile_revision_proposals FOR INSERT
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));

DROP POLICY IF EXISTS profile_revision_proposals_admin_update ON profile_revision_proposals;
CREATE POLICY profile_revision_proposals_admin_update ON profile_revision_proposals FOR UPDATE
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));
