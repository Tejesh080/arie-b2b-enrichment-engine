-- =============================================================================
-- 0030_organization_billing.sql — Productization M6 Part 2/4: the billing
-- domain model. Stripe is the payment/subscription authority; this table is
-- ARIE's own durable *record* of what Stripe last told it, never written to
-- speculatively ahead of a confirmed Stripe event (see `arie.billing.service`).
--
-- One row per organization, created at provisioning time (self-service
-- `arie.provisioning.create_customer_organization`, or this migration's own
-- backfill for every pre-M6 organization) — never lazily materialized on
-- first billing read, so `arie.billing.repository.get_billing` can assert a
-- row exists rather than special-casing `None` at every call site the way
-- `arie.limits.get_limits` already does for `organizations` itself.
--
-- `plan` is an ARIE-internal value (`internal`/`starter`/`growth`/`pro`),
-- never a Stripe price id — see `arie.billing.plans` for the mapping and
-- `arie.config.StripeConfig.price_id_for_plan` for the one place a plan name
-- becomes a price id, server-side only. `status` mirrors Stripe's own
-- subscription status vocabulary verbatim (so a new Stripe status is a data
-- value, not a migration) plus one ARIE-only value, `none`, for "provisioned,
-- never checked out" — the safe starting state for a brand-new self-service
-- organization before Stripe has said anything at all.
--
-- `last_event_created_at` guards against Stripe's own documented out-of-order
-- webhook delivery (Part 26): `arie.billing.service.process_webhook_event`
-- only applies an incoming event's subscription-state fields if that event's
-- own `created` timestamp is newer than the last one already applied,
-- discarding a stale redelivery rather than letting it roll state backward.
-- =============================================================================

CREATE TABLE IF NOT EXISTS organization_billing (
    organization_id        UUID PRIMARY KEY REFERENCES organizations(organization_id) ON DELETE CASCADE,
    stripe_customer_id     TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    plan                   TEXT NOT NULL DEFAULT 'starter'
        CHECK (plan IN ('internal', 'starter', 'growth', 'pro')),
    status                 TEXT NOT NULL DEFAULT 'none'
        CHECK (status IN (
            'none', 'incomplete', 'incomplete_expired', 'trialing', 'active',
            'past_due', 'canceled', 'unpaid', 'paused'
        )),
    current_period_start   TIMESTAMPTZ,
    current_period_end     TIMESTAMPTZ,
    cancel_at_period_end   BOOLEAN NOT NULL DEFAULT false,
    canceled_at            TIMESTAMPTZ,
    last_event_created_at  TIMESTAMPTZ,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- `event_id` is Stripe's own event id (`evt_...`) — the primary key IS the
-- idempotency mechanism: a second delivery of the same event is a duplicate
-- INSERT, caught with `ON CONFLICT DO NOTHING` by `arie.billing.repository
-- .record_webhook_event`, never a second processing pass. `organization_id`
-- is nullable because a small class of events (a signature failure, or an
-- event whose customer cannot yet be resolved to an organization) is still
-- worth a durable row for operator debugging even though no tenant can be
-- attributed. No payload column: the full Stripe payload is never persisted
-- (it can carry more than this table needs to retain), only a hash for
-- dedup-adjacent debugging — see `arie.billing.repository`'s own docstring.
CREATE TABLE IF NOT EXISTS billing_webhook_events (
    event_id           TEXT PRIMARY KEY,
    event_type         TEXT NOT NULL,
    stripe_created_at  TIMESTAMPTZ NOT NULL,
    payload_hash        TEXT NOT NULL,
    organization_id    UUID REFERENCES organizations(organization_id),
    received_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at       TIMESTAMPTZ,
    processing_status  TEXT NOT NULL DEFAULT 'pending'
        CHECK (processing_status IN ('pending', 'processed', 'failed', 'ignored')),
    sanitized_error    TEXT
);

CREATE INDEX IF NOT EXISTS idx_billing_webhook_events_organization
    ON billing_webhook_events(organization_id) WHERE organization_id IS NOT NULL;

-- Backfill: every organization that exists before this migration gets a
-- billing row today, not lazily. The Legacy Organization is the one place a
-- fixed UUID is acceptable to hard-code — a one-time bootstrap fact, not
-- application logic reaching for it (see `arie.tenancy.LEGACY_ORGANIZATION_ID`'s
-- own docstring for the same distinction drawn by migration 0014). Every
-- other pre-M6 organization becomes `starter`/`none` — provisioned but never
-- checked out — the same safe floor a brand-new self-service signup gets,
-- rather than silently granting a paid plan's entitlements to an
-- organization that never subscribed to one.
INSERT INTO organization_billing (organization_id, plan, status)
SELECT
    organization_id,
    CASE WHEN organization_id = '00000000-0000-0000-0000-000000000001' THEN 'internal' ELSE 'starter' END,
    CASE WHEN organization_id = '00000000-0000-0000-0000-000000000001' THEN 'active' ELSE 'none' END
FROM organizations
ON CONFLICT (organization_id) DO NOTHING;
