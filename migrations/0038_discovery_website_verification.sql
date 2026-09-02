-- =============================================================================
-- 0038_discovery_website_verification.sql — Opportunity Activation Part 26.
--
-- Three columns on `discovery_candidates`, nothing else. Website verification
-- runs *before* a candidate has a `leads`/`companies` row — see
-- `arie.discovery.website_verification`'s module docstring — so its result
-- has nowhere else to live; the existing `evidence` table is keyed by an
-- entity identity that does not exist yet at this stage. Buyer identification
-- needed no equivalent migration: it runs after promotion, when a real
-- person_id exists, and its facts live in the existing `evidence` table
-- (see `arie.discovery.buyer_search.BUYER_EVIDENCE_FIELDS`).
-- =============================================================================

ALTER TABLE discovery_candidates
    ADD COLUMN IF NOT EXISTS verification_status TEXT
        CHECK (verification_status IN ('verified', 'rejected', 'unavailable', 'skipped')),
    ADD COLUMN IF NOT EXISTS verified_facts JSONB,
    ADD COLUMN IF NOT EXISTS website_verified_at TIMESTAMPTZ;
