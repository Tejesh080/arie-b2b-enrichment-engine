-- =============================================================================
-- 0044_model_call_cost_basis.sql — what a recorded model cost actually *is*.
--
-- Migration 0043 drew the line for `provider_calls`; `model_calls` had the same
-- problem one level down. Its `cost_usd` is computed from
-- `arie.ledger.pricing.MODEL_PRICES` — published per-million-token *list*
-- rates, transcribed from vendor pricing pages (Bedrock's read from the AWS
-- Price List API, still list prices). Neither DeepSeek nor Bedrock returns a
-- per-call dollar figure; both return token counts. So every row in this table
-- is a list-price estimate, and none of it is a vendor-reported charge — but
-- nothing on the row said so, and `arie.usage` folded all of it into one total.
--
-- The vocabulary is `provider_calls`' own, deliberately: the same four values
-- mean the same four things, so a reader does not have to learn two schemes.
--
--   * `modelled_list_price`   — a real model call, priced from published rates.
--                               This is every real DeepSeek and Bedrock call.
--   * `simulated_catalogue`   — no real call happened. `fake-llm` is priced at
--                               $0 and exists so the test suite can exercise
--                               the ledger without a vendor.
--   * `vendor_billed`         — reserved. The day a provider reports its own
--                               charge, that is what this becomes, and
--                               `actual_cost_usd` carries the figure.
--   * `modelled_credit_equivalent` — reserved, for a credit-metered model API.
--
-- **`actual_cost_usd` stays NULL and that is the correct value.** A list price
-- is not a bill. Writing the computed figure there would invent marginal money
-- that nobody was charged, which is precisely the error this pair of
-- migrations exists to stop. A free-tier or credit-funded real call may record
-- 0.0 — "we know it cost nothing" — but only when that is actually known.
-- =============================================================================

ALTER TABLE model_calls
    ADD COLUMN IF NOT EXISTS cost_basis TEXT;

-- Every existing row is a real vendor call priced from a list rate, except the
-- test double. The split is by model name because that is the only evidence
-- these rows carry, and it is sufficient: `fake-llm` is the one entry in
-- `MODEL_PRICES` priced at zero, and it is named in `arie.ledger.pricing` as a
-- deliberate exception rather than a hole in the rule.
UPDATE model_calls
   SET cost_basis = CASE
           WHEN model = 'fake-llm' THEN 'simulated_catalogue'
           ELSE 'modelled_list_price'
       END
 WHERE cost_basis IS NULL;

ALTER TABLE model_calls
    DROP CONSTRAINT IF EXISTS model_calls_cost_basis_check;

ALTER TABLE model_calls
    ADD CONSTRAINT model_calls_cost_basis_check CHECK (
        cost_basis IS NULL OR cost_basis IN (
            'simulated_catalogue',
            'modelled_credit_equivalent',
            'modelled_list_price',
            'vendor_billed'
        )
    );

COMMENT ON COLUMN model_calls.cost_basis IS
    'What cost_usd is, using the same vocabulary as provider_calls.cost_basis. '
    'Every real DeepSeek/Bedrock call is ''modelled_list_price'': the vendors '
    'report token counts, not dollars, so the figure is an estimate of what '
    'the call was worth and must never be reported as billed money.';

COMMENT ON COLUMN model_calls.actual_cost_usd IS
    'Money actually billed, only when a vendor states it. NULL is correct for '
    'every row today. 0.0 means "known to have cost nothing" (a free-tier or '
    'credit-funded real call), which is a different claim from NULL.';
