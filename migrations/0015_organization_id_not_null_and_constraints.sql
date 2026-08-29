-- =============================================================================
-- 0015_organization_id_not_null_and_constraints.sql — close the tenancy
-- boundary: no row may exist without an owning organization, and the
-- uniqueness rules that used to be global are now per-tenant.
--
-- Safe to run only after 0014's backfill — `SET NOT NULL` fails loudly against
-- any remaining NULL, which is exactly the guard that stops this migration
-- from silently locking in a partially-backfilled database. Re-running this
-- file is safe: every statement is already idempotent (`SET NOT NULL` a
-- second time, `DROP ... IF EXISTS`, `CREATE ... IF NOT EXISTS`).
-- =============================================================================

ALTER TABLE persons ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE evidence ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE leads ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE provider_calls ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE model_calls ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE scores ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE voi_decisions ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE human_reviews ALTER COLUMN organization_id SET NOT NULL;
ALTER TABLE decision_receipts ALTER COLUMN organization_id SET NOT NULL;

-- persons: two organizations independently ingesting the same email must not
-- collide into one row (0001_init.sql's global UNIQUE) or silently overwrite
-- each other's name/title on ON CONFLICT — see arie.identity.resolver.
ALTER TABLE persons DROP CONSTRAINT IF EXISTS persons_canonical_email_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_org_email ON persons(organization_id, canonical_email);

-- leads: two organizations' upstream CRMs can legitimately reuse the same
-- (source, external_ref) pair — the old global partial unique index would
-- have let one organization's redelivery match a completely different
-- organization's lead.
DROP INDEX IF EXISTS idx_leads_source_ref;
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_org_source_ref
    ON leads(organization_id, source, external_ref) WHERE external_ref IS NOT NULL;

-- evidence: the cache lookup (arie.evidence.store) must never be able to
-- answer one organization's question from another organization's paid-for
-- evidence, including company-entity rows — see 0012's tenancy-boundary note.
DROP INDEX IF EXISTS idx_evidence_lookup;
CREATE INDEX IF NOT EXISTS idx_evidence_lookup
    ON evidence(organization_id, entity_type, entity_id, field_name, expires_at DESC);

-- provider_calls.idempotency_key and model_calls.idempotency_key stay
-- globally unique, deliberately not composited with organization_id: every
-- key in use today is derived from a job_id (`f"job:{job_id}:..."`), and a
-- job belongs to exactly one lead, which belongs to exactly one organization
-- permanently — so no cross-tenant collision is reachable, and compositing
-- the constraint would be churn with no isolation benefit.
