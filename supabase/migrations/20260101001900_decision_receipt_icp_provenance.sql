-- =============================================================================
-- 0020_decision_receipt_icp_provenance.sql — Productization M3: record which
-- organization ICP profile/version produced each Decision Receipt.
--
-- Additive-only, nullable columns — existing rows (every receipt written
-- before this milestone) simply have no ICP profile reference, which is
-- honest: they were produced before per-organization configuration existed
-- at all, by the same reference weights every organization's bootstrapped
-- v1 profile now names explicitly (migrations/0019). Nothing about an
-- existing receipt's meaning changes; this only adds a fact new receipts can
-- state that old ones cannot.
--
-- `icp_profile_id` is intentionally NOT a foreign key. Both
-- `decision_receipts` and `organization_icp_profiles` cascade-delete from
-- `organizations` independently (each via its own `organization_id` FK); an
-- `icp_profile_id -> organization_icp_profiles(profile_id)` FK with no
-- `ON DELETE` action would make an organization delete's ordering across
-- those two independent cascades load-bearing for no real benefit — nothing
-- in this product deletes an organization today, and the write is scoped to
-- one organization's own profile by application logic in
-- `arie.jobs.handlers` regardless.
-- =============================================================================

ALTER TABLE decision_receipts
    ADD COLUMN IF NOT EXISTS icp_profile_id      UUID,
    ADD COLUMN IF NOT EXISTS icp_profile_version INT;
