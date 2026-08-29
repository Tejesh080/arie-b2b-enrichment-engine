-- =============================================================================
-- 0013_organization_id_columns.sql — nullable organization_id on every
-- tenant-owned table.
--
-- Nullable here, `NOT NULL` only after 0014's backfill has run — the same
-- zero-downtime shape every prior migration in this repo that added a column
-- to a non-empty table used (e.g. 0009's `leads.is_shadow`, except that one
-- had a safe default and this one cannot: which organization a pre-existing
-- row belongs to is a data decision, not a constant). Splitting the column
-- add from the NOT NULL/constraint tightening (0015) is what keeps this
-- migration re-runnable and safe against a database that already has rows.
--
-- `companies` is deliberately absent from this list — see 0012's own
-- docstring for the tenancy boundary this project settled on. `jobs` and
-- `lead_events` are also absent: neither is read by any customer-facing
-- endpoint independent of the `lead_id` it already carries, so there is no
-- exposure path yet, and adding the column now would be scope not asked for
-- in Productization M1.
-- =============================================================================

ALTER TABLE persons ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_persons_organization ON persons(organization_id);

ALTER TABLE evidence ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_evidence_organization ON evidence(organization_id);

ALTER TABLE leads ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_leads_organization ON leads(organization_id);

ALTER TABLE provider_calls ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_provider_calls_organization ON provider_calls(organization_id);

ALTER TABLE model_calls ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_model_calls_organization ON model_calls(organization_id);

ALTER TABLE scores ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_scores_organization ON scores(organization_id);

ALTER TABLE voi_decisions ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_voi_decisions_organization ON voi_decisions(organization_id);

ALTER TABLE human_reviews ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_human_reviews_organization ON human_reviews(organization_id);

ALTER TABLE decision_receipts ADD COLUMN IF NOT EXISTS organization_id UUID REFERENCES organizations(organization_id);
CREATE INDEX IF NOT EXISTS idx_decision_receipts_organization ON decision_receipts(organization_id);
