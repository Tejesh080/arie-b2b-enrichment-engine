-- =============================================================================
-- 0014_legacy_organization_backfill.sql — migrate every pre-existing row into
-- one well-known "Legacy Organization" safely.
--
-- The organization_id is a fixed, well-known UUID (not gen_random_uuid())
-- deliberately: it lets this migration be re-run safely (`ON CONFLICT DO
-- NOTHING` matches on the literal, not on a value only the first run would
-- know) and lets application code/tests refer to the legacy org by a constant
-- (`arie.tenancy.LEGACY_ORGANIZATION_ID`) without a lookup.
--
-- The backfill UPDATEs are idempotent by construction (`WHERE organization_id
-- IS NULL`) — the first run touches every pre-existing row, every run after
-- that touches zero, since 0013 already made new rows nullable and nothing
-- before 0015 (which adds the NOT NULL constraint) can write a NULL into a
-- brand-new row without the application already setting one.
-- =============================================================================

INSERT INTO organizations (organization_id, name, slug, status)
VALUES ('00000000-0000-0000-0000-000000000001', 'Legacy Organization', 'legacy', 'active')
ON CONFLICT (organization_id) DO NOTHING;

UPDATE persons SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE evidence SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE leads SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE provider_calls SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE model_calls SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE scores SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE voi_decisions SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE human_reviews SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
UPDATE decision_receipts SET organization_id = '00000000-0000-0000-0000-000000000001' WHERE organization_id IS NULL;
