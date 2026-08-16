-- =============================================================================
-- 0003_identity_resolution.sql — support for the name-only fallback match
--
-- Domain stays the primary identity key for companies: `canonical_domain` is
-- already UNIQUE (0001_init.sql) and every domain-bearing lead resolves through
-- it exactly, with no ambiguity. This migration exists for the *degraded* path
-- — a lead that arrives with a company name but no domain or work email to
-- derive one from — which arie.identity.resolver falls back to matching by
-- normalized name among domain-less companies only.
--
-- `normalized_name` is deliberately NOT unique. Two genuinely different real
-- companies can share a normalized name ("Acme" is not rare), so enforcing
-- uniqueness here would silently merge unrelated accounts. It is a best-effort
-- lookup column, not a key — how often that best effort actually succeeds is
-- exactly what Step 7's ambiguous-identity measurement reports.
--
-- It is also not a GENERATED column: 0001_init.sql's evidence.expires_at
-- already hit Postgres's IMMUTABLE-only restriction on generated expressions,
-- and name normalization (legal-suffix stripping, punctuation folding) is
-- app-side logic, not a trivial SQL expression. arie.identity.resolver is
-- solely responsible for keeping normalized_name in sync with name.
-- =============================================================================

ALTER TABLE companies ADD COLUMN IF NOT EXISTS normalized_name TEXT;

CREATE INDEX IF NOT EXISTS idx_companies_normalized_name
    ON companies(normalized_name) WHERE normalized_name IS NOT NULL;
