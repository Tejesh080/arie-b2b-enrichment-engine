-- =============================================================================
-- 0042_lead_data_class.sql — where a lead came from, as a fact the row carries.
--
-- A production audit of organization 00000000-...-0001 found 194 leads, of
-- which exactly ONE was real customer data. The other 193 were development
-- exhaust: 74 integration-test rows, 69 frozen-eval-corpus identities, 29 load
-- test rows, 5 canaries, 7 RFC 2606 reserved-domain fixtures, and 9 whose
-- provenance could not be established. Every aggregate the product showed that
-- customer — dashboard counts, a 70-item review queue, $40.89 of "spend", and
-- an Ask ARIE answer that was 16/20 fixtures — was computed over that mixture.
--
-- The provenance was recoverable (harness tags in `leads.source`, exercise
-- prefixes in `external_ref`, reserved domains, corpus identity, and the fact
-- that 182 rows predate the organization's first member) but only by
-- re-deriving it with regexes at query time. That is not a thing to build a
-- product filter on: it drifts the moment a new harness appears, and it cannot
-- express "a human looked at this and decided".
--
-- **Shaped after `is_shadow` (0009), deliberately.** That column solved the
-- same problem for shadow-mode leads: a plain passthrough column on `leads`,
-- filtered by the *aggregate* business views, ignored by per-lead reads. The
-- same rule applies here and for the same reason — a quarantined lead must
-- keep its full audit trail and stay openable by id, or quarantine becomes a
-- soft delete.
--
-- `DEFAULT 'production'` makes this migration inert: every existing row and
-- every new ingest keeps today's behaviour until something deliberately
-- reclassifies it. Classification is a separate, reversible step.
--
-- The vocabulary is CHECK-constrained rather than left free-text. A typo'd
-- class would silently quarantine a real customer's leads, which is the one
-- failure mode this column must not have.
-- =============================================================================

ALTER TABLE leads
    ADD COLUMN IF NOT EXISTS data_class TEXT NOT NULL DEFAULT 'production';

-- Added separately from the column so re-running this migration against a
-- database that already has the column still installs the constraint.
ALTER TABLE leads
    DROP CONSTRAINT IF EXISTS leads_data_class_check;

ALTER TABLE leads
    ADD CONSTRAINT leads_data_class_check CHECK (data_class IN (
        'production',         -- a real customer's own lead
        'integration_test',   -- an automated test or deployment-validation run
        'load_test',          -- a throughput/concurrency exercise
        'canary',             -- a release canary
        'benchmark',          -- a frozen eval-corpus identity
        'synthetic_fixture',  -- a reserved-name placeholder (RFC 2606/6761)
        'quarantined',        -- withdrawn from the product by an operator
        'unknown'             -- provenance could not be established
    ));

-- Every product-facing read is "this organization's production leads, newest
-- first". A partial index keyed on exactly that predicate stays small — on the
-- audited organization it covers 1 row out of 194 — and keeps the fixtures out
-- of the index entirely rather than merely out of the results.
CREATE INDEX IF NOT EXISTS idx_leads_org_production
    ON leads (organization_id, created_at DESC)
    WHERE data_class = 'production';

COMMENT ON COLUMN leads.data_class IS
    'Provenance of this lead. Only ''production'' participates in user-facing '
    'aggregates (Ask ARIE, dashboards, review queue, usage, exports); every '
    'class stays readable by lead_id so its receipt and ledger survive. '
    'Server-assigned: ordinary ingest always writes ''production'', and no '
    'caller can assert it — see arie.api.ingest.';
