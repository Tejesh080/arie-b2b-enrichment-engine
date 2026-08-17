-- =============================================================================
-- 0008_decision_receipts.sql — post-M1 Decision Receipt (P1)
--
-- Additive-only: one new table, nothing dropped or rewritten. Must stay
-- re-runnable against a database that already has it (ADR 0005).
--
-- Why this table exists at all: GET /leads/{lead_id}/receipt needs to answer
-- "why did ARIE stop spending money and make this decision", truthfully, for
-- leads decided arbitrarily long ago. Most of that answer already lives in
-- durable, lead-scoped tables (provider_calls, scores, human_reviews) and is
-- read live from them. A few facts are neither: they are computed once, at
-- decision time, by `arie.jobs.handlers.compute_score`, and then discarded —
--
--   * score bounds (lower/upper) — `arie.scoring.engine.ScoreBounds`
--   * the policy's own name and the confidence-calibration method in effect
--   * which field won each piece of evidence, from which source, and whether
--     sources disagreed — `arie.scoring.merge.FieldResolution`
--
-- The last one matters most: `evidence` (0001_init.sql) is keyed by
-- (entity_type, entity_id) — company/person — not by lead, and is shared and
-- mutated by every other lead at that company. Reading it "now" to explain an
-- old lead's decision would show today's cache state, not what was actually
-- known when the decision was made. This table is the frozen snapshot that
-- avoids that — written once, inside compute_score's own work transaction, so
-- it commits atomically with the `scores` row and the state-graph walk it sits
-- beside, and is never updated afterward.
--
-- One row per lead: compute_score's own guard (it only runs against a lead
-- still at LeadStatus.NEW) means it can complete at most once per lead, but
-- the unique index is kept anyway as the same belt-and-braces backstop
-- idx_human_reviews_one_pending_per_lead already is for its own invariant.
-- =============================================================================

CREATE TABLE IF NOT EXISTS decision_receipts (
    receipt_id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id                 UUID NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),

    decision                TEXT NOT NULL,     -- Decision enum: auto_route | reject | escalate_human
    autonomous              BOOLEAN NOT NULL,
    confidence              NUMERIC NOT NULL,
    tau                     NUMERIC NOT NULL,

    score_value             NUMERIC NOT NULL,
    score_lower             NUMERIC NOT NULL,
    score_upper             NUMERIC NOT NULL,

    stop_reason             TEXT NOT NULL,
    policy_name             TEXT NOT NULL,
    scorer_version          TEXT NOT NULL,
    confidence_calibration  TEXT NOT NULL,

    -- {"known": [{"field", "source", "confidence", "candidate_count", "contested"}, ...],
    --  "unknown": ["field_name", ...]} — arie.scoring.engine.ScoringResult's
    -- resolutions/signals at decision time, not a re-read of today's `evidence`.
    evidence_snapshot       JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_decision_receipts_lead ON decision_receipts(lead_id);
