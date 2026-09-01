-- =============================================================================
-- 0034_llm_usage_and_budgets.sql — M7 Slice 1: the LLM usage ledger and the
-- organization-level ceilings that gate it.
--
-- **No new usage table.** `model_calls` (migrations/0001, 0005, 0013, 0015)
-- already is the LLM ledger: one row per billed model call, organization-
-- scoped and NOT NULL since 0015, RLS-isolated since 0016, idempotent on
-- `idempotency_key` since 0005, priced in NUMERIC, indexed by
-- (organization_id, created_at) since 0022, and rolled into `v_lead_cost`.
-- A second `llm_usage_ledger` table would have duplicated every one of those
-- properties and split "what did models cost us" across two places — which is
-- exactly the question the ledger exists to answer in one place. This
-- migration adds the three columns M7 needs and nothing else.
--
-- `provider` — which vendor served the call, so the LLMProvider abstraction
-- (`arie.llm.provider`) is observable in the ledger rather than only in code.
-- Nullable: every existing row predates the abstraction and was DeepSeek, but
-- backfilling a guess would state as recorded fact something nobody recorded.
-- NULL here means "written before providers were distinguished", and
-- `arie.llm.service` populates it on every row it writes from now on.
--
-- `batch_id` — M7 budgets LLM spend per CSV batch (`max_llm_calls_per_batch`,
-- `max_llm_cost_usd_per_batch` below), which is unanswerable without it.
-- ON DELETE SET NULL, matching `model_calls.lead_id`'s existing behaviour and
-- for the same reason `arie.ledger.store`'s docstring gives: money already
-- spent is not undone by deleting the thing it was spent on, so the cost row
-- outlives its subject rather than cascading away with it.
--
-- `actual_cost_usd` — the vendor's own billed figure, and only ever that.
-- Mirrors `provider_calls.actual_cost_usd` (0028) exactly, including the rule
-- that matters most: it is NULL unless a vendor response explicitly stated a
-- charge, and it is never computed, estimated, or copied from `cost_usd`.
-- NULL means "not reported", not "free". `cost_usd` remains the modelled
-- figure derived from `arie.ledger.pricing`'s hand-recorded list prices, and
-- the two columns exist separately so no view can quietly present a modelled
-- number as a billed one.
--
-- The organization ceilings follow 0023/0026's precedent — structured columns
-- on `organizations`, not a new table — with real NOT NULL defaults so every
-- existing organization is covered by a ceiling immediately rather than
-- reading as unlimited until someone configures it. They are usage ceilings,
-- not a billing plan: M6's entitlement system is untouched, and nothing here
-- knows what an organization pays.
-- =============================================================================

ALTER TABLE model_calls
    ADD COLUMN IF NOT EXISTS provider        TEXT,
    ADD COLUMN IF NOT EXISTS batch_id        UUID REFERENCES lead_batches(batch_id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS actual_cost_usd NUMERIC(12, 6);

-- The per-batch budget guard's own query: "how many calls, and how much
-- modelled spend, has this batch already incurred". Without this it is a
-- sequential scan of every model call the deployment has ever made, run once
-- per candidate LLM call.
CREATE INDEX IF NOT EXISTS idx_model_calls_batch
    ON model_calls(batch_id, created_at DESC)
    WHERE batch_id IS NOT NULL;

-- The monthly guard reads (organization_id, created_at) — already indexed by
-- `idx_model_calls_org_created_at` (0022) — but also filters on `purpose` to
-- separate M7's intelligence spend from M1's signal extraction. Adding
-- `purpose` to a covering index rather than a second one keeps the write cost
-- of this hot table where it was.
CREATE INDEX IF NOT EXISTS idx_model_calls_org_purpose_created_at
    ON model_calls(organization_id, purpose, created_at DESC);

ALTER TABLE organizations
    ADD COLUMN IF NOT EXISTS max_llm_calls_per_batch INT NOT NULL DEFAULT 500
        CHECK (max_llm_calls_per_batch >= 0),
    ADD COLUMN IF NOT EXISTS max_llm_cost_usd_per_batch NUMERIC(12, 4) NOT NULL DEFAULT 2.0000
        CHECK (max_llm_cost_usd_per_batch >= 0),
    ADD COLUMN IF NOT EXISTS max_llm_cost_usd_per_month NUMERIC(12, 4) NOT NULL DEFAULT 25.0000
        CHECK (max_llm_cost_usd_per_month >= 0),
    ADD COLUMN IF NOT EXISTS preferred_llm_model TEXT;

-- `preferred_llm_model` is nullable with no default and no CHECK: the set of
-- valid models is `arie.ledger.pricing.MODEL_PRICES`, which changes when a
-- price is recorded rather than when the schema changes, and a database CHECK
-- listing model names would go stale silently the first time one was added.
-- `arie.llm.factory.build_llm_provider` validates it against that table and
-- falls back to the configured default rather than failing an organization's
-- work over a model that was withdrawn. NULL means "use the deployment
-- default" (`LLM_MODEL`), which is what every organization means today.

-- No RLS changes. `model_calls` and `organizations` both already have tenant
-- isolation policies (migrations/0016), and adding columns to a table does not
-- alter which rows a policy admits — the new columns are covered by the
-- existing `model_calls_tenant_isolation` and `org_select` policies with no
-- further statement. Stated explicitly so a reviewer does not have to infer
-- the absence was considered.
