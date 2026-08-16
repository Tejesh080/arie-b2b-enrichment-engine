-- =============================================================================
-- 0006_human_review_workflow.sql — M1 Step 11 (human approval path)
--
-- No new columns: `human_reviews` (0001_init.sql) already carries everything
-- the workflow needs — reviewer, original_decision, final_decision, notes,
-- responded_at. `responded_at IS NULL` is "pending"; setting it is the one
-- irreversible write a review ever gets (arie.approval.service.submit_decision
-- enforces that with a compare-and-swap UPDATE, not a constraint, since which
-- content is "the same decision" isn't something a CHECK constraint can express).
--
-- This index is the one piece of enforcement that *does* belong in the schema:
-- at most one open review per lead, ever, so "retrieve the pending review for
-- this lead" can never be ambiguous.
-- =============================================================================

-- A lead escalates to AWAITING_HUMAN exactly once before it's answered — the
-- DECISION -> AWAITING_HUMAN edge only fires from arie.statemachine, guarded by
-- leads.version, so nothing should ever be able to open a second pending review
-- for the same lead. This is the database-level backstop for that invariant
-- (belt-and-braces with the version check, not a substitute for it), and it
-- doubles as the ON CONFLICT arbiter for arie.approval.service.request_review's
-- idempotent insert — the same idiom idx_leads_source_ref and jobs.idempotency_key
-- already use: a partial unique index that also serves as the natural dedup key.
CREATE UNIQUE INDEX IF NOT EXISTS idx_human_reviews_one_pending_per_lead
    ON human_reviews(lead_id) WHERE responded_at IS NULL;
