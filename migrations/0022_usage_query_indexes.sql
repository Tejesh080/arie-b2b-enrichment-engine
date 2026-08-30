-- =============================================================================
-- 0022_usage_query_indexes.sql — Productization M3, Part 7: indexes for the
-- new `GET /usage?from=&to=` aggregation.
--
-- No existing index covers "this organization's rows in a date range" for
-- any of these three tables — the closest, `idx_provider_calls_organization`
-- (0013) and `idx_leads_organization` (0013), are `organization_id`-only, and
-- `idx_provider_calls_quota` (0010) is provider-scoped, not tenant-scoped.
-- `arie.usage.get_usage_summary` is the first query shape that filters by
-- *both* organization and a timestamp range together, so it is the first to
-- need a composite index rather than a single-column one.
-- =============================================================================

CREATE INDEX IF NOT EXISTS idx_leads_org_created_at ON leads(organization_id, created_at);

CREATE INDEX IF NOT EXISTS idx_provider_calls_org_requested_at
    ON provider_calls(organization_id, requested_at);

CREATE INDEX IF NOT EXISTS idx_model_calls_org_created_at
    ON model_calls(organization_id, created_at);
