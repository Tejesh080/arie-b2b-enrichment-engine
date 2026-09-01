-- =============================================================================
-- 0033_organization_billing_bootstrap_trigger.sql — Productization M6.
--
-- `arie.billing.repository.get_billing` asserts every organization has
-- exactly one `organization_billing` row — the same "an authenticated
-- caller's own organization always exists" invariant `arie.limits.get_limits`
-- already relies on for `organizations` itself. Migration 0030's backfill
-- made that true for every organization that existed *then*; this trigger is
-- what keeps it true for every organization created *since*, regardless of
-- which code path does the inserting.
--
-- That matters beyond `arie.provisioning.create_customer_organization` (which
-- already writes its own billing row explicitly, and still works unchanged —
-- `ON CONFLICT DO NOTHING` below makes the two paths idempotent with each
-- other): several integration-test fixtures, and any future admin tooling,
-- insert directly into `organizations` without knowing anything about
-- billing. A trigger enforces the invariant at the one place it can never be
-- forgotten, rather than requiring every current and future caller to
-- remember it.
--
-- `plan='starter'`/`status='none'` — the same safe, unsubscribed-floor
-- starting state `arie.provisioning` already gives a fresh self-service
-- organization; see `arie.billing.plans.UNSUBSCRIBED`.
-- =============================================================================

CREATE OR REPLACE FUNCTION arie_bootstrap_organization_billing() RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO organization_billing (organization_id, plan, status)
    VALUES (NEW.organization_id, 'starter', 'none')
    ON CONFLICT (organization_id) DO NOTHING;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_organizations_bootstrap_billing ON organizations;
CREATE TRIGGER trg_organizations_bootstrap_billing
    AFTER INSERT ON organizations
    FOR EACH ROW EXECUTE FUNCTION arie_bootstrap_organization_billing();
