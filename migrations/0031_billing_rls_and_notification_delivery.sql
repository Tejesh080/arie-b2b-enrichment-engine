-- =============================================================================
-- 0031_billing_rls_and_notification_delivery.sql — Productization M6.
--
-- Part A: RLS for `organization_billing`, matching migration 0016's own
-- pattern (defense-in-depth only — the API's pooled connection is a
-- service-role/superuser connection and bypasses RLS entirely; this is for
-- any access path that isn't that connection). Read-only: owner/admin may
-- SELECT their own organization's billing row (Part 37 — "viewing billing:
-- owner/admin by default"); there is deliberately no write policy, the same
-- as every tenant table here except `organization_members` — every real
-- write to this table happens through `arie.billing.repository`'s
-- service-role connection (a Stripe webhook, or a Checkout/Portal session
-- helper), never a direct client mutation. `billing_webhook_events` gets RLS
-- enabled with **no** policy at all — a table with RLS on and zero policies
-- denies every row to every non-bypassing role, which is correct here: no
-- customer-facing path should ever see raw webhook bookkeeping.
--
-- Part B: notification delivery tracking, additive to two Productization M4
-- tables. `organization_invitations` gains an email-delivery status distinct
-- from the invitation's own lifecycle status (`pending`/`accepted`/`revoked`/
-- `expired`) — an invitation can be created successfully even if the email
-- attempt that followed it failed (Part 14's "invitation may exist even if
-- email delivery fails" requirement), so this cannot reuse that column.
-- `human_review_notifications` is a pure dedup marker (Part 16) — one row per
-- review that has ever been notified, inserted with `ON CONFLICT DO NOTHING
-- RETURNING`, so two workers racing to notify the same escalation send at
-- most one email between them.
-- =============================================================================

ALTER TABLE organization_billing ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_billing_select ON organization_billing;
CREATE POLICY organization_billing_select ON organization_billing FOR SELECT
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']));

ALTER TABLE billing_webhook_events ENABLE ROW LEVEL SECURITY;

ALTER TABLE organization_invitations
    ADD COLUMN IF NOT EXISTS email_status TEXT NOT NULL DEFAULT 'pending'
        CHECK (email_status IN ('pending', 'sent', 'failed')),
    ADD COLUMN IF NOT EXISTS email_error TEXT,
    ADD COLUMN IF NOT EXISTS email_sent_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS human_review_notifications (
    review_id    UUID PRIMARY KEY REFERENCES human_reviews(review_id) ON DELETE CASCADE,
    notified_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
