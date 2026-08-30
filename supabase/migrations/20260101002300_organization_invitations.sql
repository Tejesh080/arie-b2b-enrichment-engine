-- =============================================================================
-- 0024_organization_invitations.sql — Productization M4 Part 2: minimal
-- organization invitation/membership workflow, plus the append-only audit
-- log Part 2's own actions (and Part 3-7's provider/Vault actions, later)
-- write to. Combined into one migration rather than two, per the brief's own
-- "minimize count if sensible" — both are small, both ship in the same
-- backend slice, and audit events need to exist before the first invitation
-- action can log one.
--
-- **Identity stays entirely Supabase Auth's** — this migration invents no
-- account/password machinery. `organization_invitations` only records *who
-- was invited, to which role, by whom, and whether that offer is still
-- open* — a real account, and the JWT that proves it, always comes from
-- Supabase Auth the same way every existing session does. Acceptance
-- (`arie.invitations.accept_invitation`) cross-checks the invited email
-- against the *verified* email on a real Supabase-issued JWT; it never
-- creates, sets, or even sees a password.
--
-- **Why a custom token instead of routing through Supabase's own invite
-- link:** Supabase's `auth.admin.generateLink`/`inviteUserByEmail` mint a
-- Supabase-hosted email flow, but the *organization/role* an invite grants
-- is entirely this application's own concept, unknown to Supabase Auth —
-- there is nothing in a Supabase invite link for this schema to hang
-- `organization_id`/`role` off, and abusing `user_metadata` for that would
-- let anything holding a valid session forge its own membership by editing
-- metadata it can write. A separate, ARIE-owned token addressed at exactly
-- one (organization, email, role) tuple is the smaller, safer piece to own.
--
-- **Token storage mirrors `organization_api_keys` exactly** — a
-- cryptographically random `secrets.token_urlsafe(32)` token, SHA-256
-- hashed (no salt/slow-KDF: correct for 256 bits of real randomness, wrong
-- for a low-entropy password), shown to the caller exactly once at creation
-- (`POST /organization/invitations`'s response), never persisted or logged
-- in raw form. `token_hash` alone is the row's real identity for
-- acceptance — the same lookup-by-hash pattern `arie.apikeys.verify_api_key`
-- already uses.
-- =============================================================================

CREATE TABLE IF NOT EXISTS organization_invitations (
    invitation_id      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    organization_id    UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    email_normalized   TEXT NOT NULL,
    role               TEXT NOT NULL CHECK (role IN ('owner', 'admin', 'analyst_reviewer')),
    token_hash         TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL DEFAULT 'pending'
                           CHECK (status IN ('pending', 'accepted', 'revoked', 'expired')),
    invited_by_user_id UUID NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ NOT NULL,
    accepted_at        TIMESTAMPTZ,
    revoked_at         TIMESTAMPTZ,
    -- Exactly one of "still open" (neither happened) or "resolved" (exactly
    -- one happened) — never both an acceptance and a revocation.
    CONSTRAINT organization_invitations_not_both_resolved
        CHECK (accepted_at IS NULL OR revoked_at IS NULL)
);

-- The application-level duplicate-invite guard (`arie.invitations
-- .create_invitation`'s own pre-check) is real correctness, but only this
-- partial unique index closes the concurrent-request race: two admins
-- inviting the same address in the same instant can't both land a pending
-- row. Partial on `status = 'pending'` so a resolved (accepted/revoked/
-- expired) invitation never blocks a fresh re-invite to the same address.
CREATE UNIQUE INDEX IF NOT EXISTS idx_organization_invitations_pending_unique
    ON organization_invitations(organization_id, email_normalized) WHERE status = 'pending';

-- The invitation-listing query (GET /organization/invitations), newest first.
CREATE INDEX IF NOT EXISTS idx_organization_invitations_org
    ON organization_invitations(organization_id, created_at DESC);

-- RLS as defense-in-depth, matching every other tenant-owned table — the
-- API's own pooled connection is service-role and bypasses this entirely;
-- see migrations/0016_row_level_security.sql's own docstring. A single
-- owner/admin-only `FOR ALL` policy, the same shape
-- `organization_api_keys_admin_only` uses: unlike ICP configuration,
-- pending-invitation email addresses are not something every member should
-- see by default, so reads are gated the same as writes.
ALTER TABLE organization_invitations ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_invitations_admin_only ON organization_invitations;
CREATE POLICY organization_invitations_admin_only ON organization_invitations FOR ALL
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']))
    WITH CHECK (arie_has_role(organization_id, ARRAY['owner', 'admin']));

-- =============================================================================
-- organization_audit_events — append-only log of organization-management
-- actions (member/invitation/provider-credential changes). Modelled directly
-- on `lead_events` (migrations/0001_init.sql): `BIGSERIAL` id, free-text
-- `event_type` (no CHECK enumerating every value — this log is meant to grow
-- new event types without a migration), a generic `JSONB payload` for
-- event-specific detail, single `created_at`. Diverges from `lead_events` in
-- carrying `organization_id` directly rather than reaching it through a
-- parent row — this table's rows are about the organization itself, not
-- about a lead that already carries tenancy (see
-- migrations/0013_organization_id_columns.sql's docstring on that
-- distinction) — and in carrying `actor_user_id`: every event this
-- milestone logs has a real human behind it (no system/automated actor
-- exists yet), so it is `NOT NULL`, unlike the nullable
-- `created_by_user_id` columns used where a migration's own bootstrap rows
-- have none.
--
-- `payload` must never carry a secret or credential value — see
-- `arie.audit`'s own docstring for the allowed-shape contract each event
-- type follows. Permanent history: no UPDATE/DELETE policy, matching
-- `organization_icp_profiles`'s versions.
-- =============================================================================

CREATE TABLE IF NOT EXISTS organization_audit_events (
    event_id        BIGSERIAL PRIMARY KEY,
    organization_id UUID NOT NULL REFERENCES organizations(organization_id) ON DELETE CASCADE,
    actor_user_id   UUID NOT NULL,
    event_type      TEXT NOT NULL,
    payload         JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- The audit-log listing query, newest first.
CREATE INDEX IF NOT EXISTS idx_organization_audit_events_org
    ON organization_audit_events(organization_id, created_at DESC);

ALTER TABLE organization_audit_events ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS organization_audit_events_admin_select ON organization_audit_events;
CREATE POLICY organization_audit_events_admin_select ON organization_audit_events FOR SELECT
    USING (arie_has_role(organization_id, ARRAY['owner', 'admin']));
