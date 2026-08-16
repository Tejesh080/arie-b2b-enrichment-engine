-- =============================================================================
-- 0001_init.sql — core schema
--
-- Design notes worth reading before changing anything here:
--
--  * `lead_events` is append-only and is the source of truth. `leads.status` is
--    a materialised convenience; it can always be rebuilt by replaying events.
--    This is what gives us durability without a workflow engine.
--
--  * `evidence` IS the cache. There is no separate cache subsystem to keep in
--    sync — a cache hit is just a row whose expires_at is still in the future.
--
--  * `voi_decisions` stores rejected candidates too, not just chosen ones. The
--    rejected rows are the audit trail explaining why enrichment stopped.
-- =============================================================================

-- ---------------------------------------------------------------- identity --

CREATE TABLE IF NOT EXISTS companies (
    company_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    canonical_domain TEXT UNIQUE,
    name             TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS persons (
    person_id       UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id      UUID REFERENCES companies(company_id),
    canonical_email TEXT UNIQUE,
    full_name       TEXT,
    title           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_persons_company ON persons(company_id);

-- --------------------------------------------------------------- lifecycle --

CREATE TABLE IF NOT EXISTS leads (
    lead_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    person_id      UUID REFERENCES persons(person_id),
    company_id     UUID REFERENCES companies(company_id),
    source         TEXT NOT NULL,
    external_ref   TEXT,
    status         TEXT NOT NULL DEFAULT 'NEW',
    version        INT  NOT NULL DEFAULT 1,   -- optimistic concurrency guard
    budget_usd_cap NUMERIC NOT NULL DEFAULT 0.50,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_leads_status ON leads(status);
-- Prevents the same upstream record being ingested twice.
CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_source_ref
    ON leads(source, external_ref) WHERE external_ref IS NOT NULL;

CREATE TABLE IF NOT EXISTS lead_events (
    event_id   BIGSERIAL PRIMARY KEY,
    lead_id    UUID NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    event_type TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_lead_events_lead ON lead_events(lead_id, event_id);

-- ---------------------------------------------------------------- evidence --

CREATE TABLE IF NOT EXISTS evidence (
    evidence_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    entity_type        TEXT NOT NULL CHECK (entity_type IN ('company', 'person')),
    entity_id          UUID NOT NULL,
    field_name         TEXT NOT NULL,
    value              JSONB NOT NULL,
    signal_description TEXT,
    source             TEXT NOT NULL,
    confidence         NUMERIC NOT NULL CHECK (confidence BETWEEN 0 AND 1),
    effect_on_score    NUMERIC,
    ttl_seconds        INT NOT NULL CHECK (ttl_seconds > 0),
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,
    raw_ref            TEXT
);

-- expires_at can't be a GENERATED column: `timestamptz + interval` is a STABLE
-- operator, not IMMUTABLE, regardless of the interval being pure seconds —
-- Postgres rejects it outright ("generation expression is not immutable").
-- Discovered running this migration against a live database for the first
-- time; a trigger gets the identical guarantee — expires_at can never drift
-- out of sync with fetched_at + ttl_seconds — without that restriction.
CREATE OR REPLACE FUNCTION evidence_set_expires_at() RETURNS trigger AS $$
BEGIN
    NEW.expires_at := NEW.fetched_at + make_interval(secs => NEW.ttl_seconds);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS evidence_expires_at_trigger ON evidence;
CREATE TRIGGER evidence_expires_at_trigger
    BEFORE INSERT OR UPDATE ON evidence
    FOR EACH ROW EXECUTE FUNCTION evidence_set_expires_at();

-- The lookup that powers every cache hit.
CREATE INDEX IF NOT EXISTS idx_evidence_lookup
    ON evidence(entity_type, entity_id, field_name, expires_at DESC);

-- ------------------------------------------------------------ external I/O --

CREATE TABLE IF NOT EXISTS provider_calls (
    call_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id          UUID REFERENCES leads(lead_id) ON DELETE SET NULL,
    provider         TEXT NOT NULL,
    entity_type      TEXT NOT NULL,
    entity_id        UUID NOT NULL,
    idempotency_key  TEXT UNIQUE NOT NULL,
    requested_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at     TIMESTAMPTZ,
    latency_ms       INT,
    cost_usd         NUMERIC,
    status           TEXT,
    cache_hit        BOOLEAN NOT NULL DEFAULT false,
    raw_response_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_provider_calls_lead ON provider_calls(lead_id);

CREATE TABLE IF NOT EXISTS model_calls (
    call_id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id           UUID REFERENCES leads(lead_id) ON DELETE SET NULL,
    model             TEXT NOT NULL,
    tier              TEXT NOT NULL CHECK (tier IN ('cheap', 'strong')),
    purpose           TEXT NOT NULL,
    escalated_from    TEXT,
    prompt_tokens     INT,
    completion_tokens INT,
    cost_usd          NUMERIC,
    latency_ms        INT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ------------------------------------------------------ decisions & audit --

CREATE TABLE IF NOT EXISTS voi_decisions (
    decision_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id            UUID NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    step_number        INT NOT NULL,
    candidate_provider TEXT NOT NULL,
    p_flips_decision   NUMERIC NOT NULL,
    business_value     NUMERIC NOT NULL,
    expected_cost      NUMERIC NOT NULL,
    latency_penalty    NUMERIC NOT NULL,
    net_evoi           NUMERIC NOT NULL,
    chosen             BOOLEAN NOT NULL,
    confidence_before  NUMERIC,
    confidence_after   NUMERIC,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_voi_lead ON voi_decisions(lead_id, step_number);

CREATE TABLE IF NOT EXISTS scores (
    score_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id             UUID NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    total_score         NUMERIC NOT NULL,
    decision_confidence NUMERIC NOT NULL,
    component_breakdown JSONB NOT NULL,
    model_version       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS human_reviews (
    review_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id           UUID NOT NULL REFERENCES leads(lead_id) ON DELETE CASCADE,
    requested_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewer          TEXT,
    original_decision TEXT,
    final_decision    TEXT,
    notes             TEXT,
    responded_at      TIMESTAMPTZ
);

-- -------------------------------------------------------------- job queue --

CREATE TABLE IF NOT EXISTS jobs (
    job_id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    lead_id         UUID REFERENCES leads(lead_id) ON DELETE CASCADE,
    job_type        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','processing','done','failed','dead_letter')),
    attempt_count   INT NOT NULL DEFAULT 0,
    next_retry_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    locked_by       TEXT,
    locked_at       TIMESTAMPTZ,
    last_error      TEXT,
    idempotency_key TEXT UNIQUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Partial index: workers only ever poll for pending work that is due.
CREATE INDEX IF NOT EXISTS idx_jobs_claimable
    ON jobs(next_retry_at) WHERE status = 'pending';

-- ------------------------------------------------------- evaluation store --

CREATE TABLE IF NOT EXISTS eval_leads (
    eval_lead_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payload           JSONB NOT NULL,
    oracle_decision   TEXT NOT NULL,
    oracle_confidence NUMERIC,
    difficulty_band   TEXT NOT NULL CHECK (difficulty_band IN ('easy','medium','hard')),
    value_tier        TEXT NOT NULL,
    split             TEXT NOT NULL CHECK (split IN ('calibration','test')),
    generator_version TEXT NOT NULL,
    seed              BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS eval_runs (
    run_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    strategy    TEXT NOT NULL,
    config      JSONB NOT NULL,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS eval_results (
    result_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_id        UUID NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    eval_lead_id  UUID NOT NULL REFERENCES eval_leads(eval_lead_id),
    decision      TEXT NOT NULL,
    confidence    NUMERIC,
    cost_usd      NUMERIC NOT NULL,
    calls_made    INT NOT NULL,
    cache_hits    INT NOT NULL DEFAULT 0,
    latency_ms    NUMERIC NOT NULL,
    matched_oracle BOOLEAN NOT NULL,
    stop_reason   TEXT
);

CREATE INDEX IF NOT EXISTS idx_eval_results_run ON eval_results(run_id);
