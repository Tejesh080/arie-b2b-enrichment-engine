"""Billing domain (Productization M6) — Stripe subscriptions, internal plan
definitions, and the one authoritative entitlement service. See
`arie.billing.plans` (entitlements), `arie.billing.service` (checkout/portal/
webhook orchestration), `arie.billing.repository`
(`organization_billing`/`billing_webhook_events` persistence), and
`arie.billing.stripe_gateway` (the only module that imports the Stripe SDK).
"""
