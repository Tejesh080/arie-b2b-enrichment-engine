-- =============================================================================
-- 0036_lead_recommendation_feedback.sql — M7 Slice 4: what a customer thought
-- of ARIE's customer-facing recommendation for one lead.
--
-- **An observation, never a mutation.** Submitting feedback here changes
-- nothing about the lead it is about: no score, no profile, no decision, no
-- provider call. It is a durable record for later aggregation (a future
-- slice's `profile_revision_proposals` source `'user_feedback'`, already a
-- valid CHECK value there since 0035) — this table is that record's home,
-- not a trigger for anything.
--
-- **One active row per (lead, user).** A person can change their mind about
-- a lead; `ON CONFLICT` on the unique pair below replaces the earlier verdict
-- rather than appending a second thumbs-up next to a first, which is what
-- "duplicate click is idempotent" and "changing feedback" both need.
--
-- **The recommendation context is captured, not re-derived later.**
-- `recommendation_priority`/`recommendation_next_action`/`profile_version`/
-- `score_snapshot` freeze what the customer was actually reacting to, the same
-- reasoning `decision_receipts` already applies to the machine decision: a
-- lead's live state moves on, and "43% of CONTACT_FIRST recommendations under
-- profile v4 got a thumbs-down" has to be answerable without the lead's
-- current priority silently standing in for what it was when the person
-- clicked.
--
-- `user_id` carries no foreign key to `auth.users`, matching every other user
-- reference in this schema (see `migrations/0012_organizations_and_members.
-- sql`'s note).
-- =============================================================================

CREATE TABLE IF NOT EXISTS lead_recommendation_feedback (
    feedback_id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id             UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    lead_id                     UUID NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    user_id                     UUID NOT NULL,
    profile_version             INT,
    recommendation_priority     TEXT NOT NULL
                                    CHECK (recommendation_priority IN
                                           ('contact_first', 'worth_pursuing', 'review', 'skip')),
    recommendation_next_action  TEXT NOT NULL,
    score_snapshot              NUMERIC,
    sentiment                   TEXT NOT NULL CHECK (sentiment IN ('positive', 'negative')),
    reason                      TEXT
                                    CHECK (reason IS NULL OR reason IN (
                                        'good_match', 'bad_match', 'wrong_person',
                                        'company_too_small', 'company_too_large',
                                        'wrong_industry', 'not_decision_maker',
                                        'already_customer', 'not_interested', 'other'
                                    )),
    note                        TEXT,
    created_by_user_id          UUID NOT NULL,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (lead_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_lead_recommendation_feedback_org
    ON lead_recommendation_feedback(organization_id, created_at DESC);

-- Aggregation by priority/profile-version (Part J's foundation) reads this
-- shape directly rather than scanning the whole table.
CREATE INDEX IF NOT EXISTS idx_lead_recommendation_feedback_org_priority
    ON lead_recommendation_feedback(organization_id, recommendation_priority, profile_version);

ALTER TABLE lead_recommendation_feedback ENABLE ROW LEVEL SECURITY;

-- Any active member may read and write feedback — giving and seeing an
-- opinion about a lead is not a privileged act, unlike a profile write
-- (`organization_icp_profiles`) or accepting a proposal, both owner/admin-only.
-- The application layer, not RLS, is what scopes an update to the caller's
-- own row (`user_id = %(user_id)s` in `arie.feedback`'s UPDATE), matching
-- this table's own "one row per (lead, user)" model; RLS here is
-- defence-in-depth at the organization boundary, same as every other table
-- in this schema.
DROP POLICY IF EXISTS lead_recommendation_feedback_select ON lead_recommendation_feedback;
CREATE POLICY lead_recommendation_feedback_select ON lead_recommendation_feedback FOR SELECT
    USING (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS lead_recommendation_feedback_insert ON lead_recommendation_feedback;
CREATE POLICY lead_recommendation_feedback_insert ON lead_recommendation_feedback FOR INSERT
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));

DROP POLICY IF EXISTS lead_recommendation_feedback_update ON lead_recommendation_feedback;
CREATE POLICY lead_recommendation_feedback_update ON lead_recommendation_feedback FOR UPDATE
    USING (organization_id IN (SELECT arie_current_organization_ids()))
    WITH CHECK (organization_id IN (SELECT arie_current_organization_ids()));
