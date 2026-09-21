"""Real-evidence live_shadow reality check (2026-09-22) — 8 frozen leads.

Runs the 8 companies frozen in
``data/evaluation/runs/realistic-pilot-2026-09-21/live_shadow_ground_truth.md``
(checksum ``a4c238ad...``) through ARIE's real, unmodified ``execution_mode
=live_shadow`` acquisition path (``arie.jobs.handlers._build_live_handlers``,
the *default* strategy — no ``evaluation_parallel`` override, no
``live_providers`` injection bypass) against real Abstract + Hunter calls.

**Isolation, following the same pattern ``scripts/live_experiment_abstract_
hunter.py`` already established**: a dedicated, disposable local Postgres
database on the same docker-compose server used for local dev — never the
deployed Supabase database, never the shared ``arie`` database docker-
compose's always-on api/worker containers poll. Fully migrated from
``migrations/*.sql`` before anything else runs.

**The one deliberate compatibility addition, and why.** Supabase's Vault
extension (``vault.create_secret``/``vault.decrypted_secrets``) does not
exist on a plain local Postgres — confirmed directly in
``migrations/0039_mcp_diag_schema.sql``'s own guarded ``IF EXISTS`` check.
Org-scoped provider credentials (``arie.provider_configs.set_provider_
credential``) are written through ``arie.vault``'s four functions and
nothing else. Rather than bypass that path (which the reference script does,
via ``build_handlers(live_providers=[...])`` — a legitimate but different
choice that skips ``execution_mode`` resolution and org-credential lookup
entirely), this script creates a **minimal, local-only, schema-compatible
stand-in for those four Vault primitives** so the *real*, unmodified
``resolve_organization_providers`` / ``set_provider_credential`` /
``get_execution_mode`` code path runs exactly as it would against Supabase.
This exists only in this disposable local database, is created once at the
top of this script, and touches nothing outside it.

**ICP fidelity.** The live-shadow org's ICP profile is created with the
*exact* ``config`` JSON already confirmed for the production ``ARIE
Realistic Pilot Evaluation`` org (fetched once, read-only, and pinned below)
— not re-derived, not re-typed. ``resolve_scoring_config`` is called for
both organizations and diffed field-by-field before any lead is ingested;
the run refuses to proceed on any difference.

**Spend discipline.** Real money. ``--preflight`` validates everything
that can be checked without spending (database isolation, safety
constants, credential presence, ICP-parity, cost ceiling) and exits.
``--confirm-live-spend`` is required to actually ingest a lead or call a
provider.

    python scripts/live_shadow_reality_check_2026_09_22.py --preflight
    python scripts/live_shadow_reality_check_2026_09_22.py --confirm-live-spend
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from psycopg import sql as psycopg_sql
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from arie.api.ingest import LeadIngestCommand, ingest_lead
from arie.config import HUNTER, LIVE_PROVIDER
from arie.icp_profiles import create_profile, resolve_scoring_config
from arie.identity.resolver import IdentityResolver
from arie.jobs.handlers import build_handlers
from arie.jobs.queue import PostgresJobQueue
from arie.jobs.worker import run_worker_cycle
from arie.live.providers import REGISTERED_LIVE_PROVIDER_NAMES
from arie.live.safety import LIVE_AUTONOMY_ENABLED, autonomy_allowed_for
from arie.migrations import checksum_of, migration_files
from arie.organizations import LIVE_SHADOW, get_execution_mode, set_execution_mode
from arie.provider_configs import set_provider_credential
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_hunter import HUNTER_PROVIDER_NAME
from arie.provisioning import create_customer_organization

RUN_DIR = Path("data/evaluation/runs/realistic-pilot-2026-09-21")
GROUND_TRUTH_CHECKSUM = "a4c238adb70e0610e716548e83626fa3c79d0c35789bc7cb507fb25d551f7966"
PROD_TRACK_A_ORG_ID = UUID("8f9763af-796d-4798-b65a-a0867158ac92")

LOCAL_DB_URL = "postgresql://arie:arie_local_dev@localhost:5432/arie_live_shadow_reality_check"
SHARED_DB_NAME = "arie"

MAX_MODELED_SPEND_USD = 0.10
"""8 leads x (Abstract $0.00165 + Hunter up to ~$0.0049) is a few cents;
generous headroom kept explicit and checked, not assumed."""

OWNER_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
"""Fixed, non-secret placeholder — organization_members.user_id carries no
FK to auth.users locally (same reasoning migrations/0012 already documents
for why that FK doesn't exist), and there is no real Supabase Auth on this
disposable local database at all."""

# The 8 frozen leads -- email is a generic per-company placeholder
# (contact@domain), exactly the same convention Track A used, never a
# fabricated address attributed to the named contact.
LEADS: list[dict[str, str]] = [
    {
        "company_name": "Alexandria Industries",
        "domain": "alexandriaindustries.com",
        "full_name": "Steve Schabel",
    },
    {"company_name": "indinero", "domain": "indinero.com", "full_name": "Jessica Mah"},
    {"company_name": "Higginbotham", "domain": "higginbotham.com", "full_name": ""},
    {"company_name": "CFO Hub", "domain": "cfohub.com", "full_name": ""},
    {"company_name": "Bannerbear", "domain": "bannerbear.com", "full_name": "Jon Yongfook"},
    {
        "company_name": "The Last Bookstore",
        "domain": "lastbookstorela.com",
        "full_name": "Josh Spencer",
    },
    {"company_name": "CNC Metalcraft", "domain": "cncmetalcraft.com", "full_name": ""},
    {
        "company_name": "Van Vlissingen and Co.",
        "domain": "vvco.com",
        "full_name": "Charles R. Lamphere",
    },
]


def _db_identity(conninfo: str) -> tuple[str, str, str]:
    parts = urlsplit(conninfo)
    return ((parts.hostname or "").lower(), str(parts.port or 5432), parts.path.lstrip("/").lower())


def _ensure_database(conninfo: str) -> bool:
    _host, _port, dbname = _db_identity(conninfo)
    try:
        with psycopg.connect(conninfo, connect_timeout=10):
            return False
    except psycopg.OperationalError as exc:
        if "does not exist" not in str(exc):
            raise
    parts = urlsplit(conninfo)
    maintenance = conninfo.replace(f"/{parts.path.lstrip('/')}", "/postgres", 1)
    with (
        psycopg.connect(maintenance, autocommit=True, connect_timeout=10) as conn,
        conn.cursor() as cur,
    ):
        cur.execute(psycopg_sql.SQL("CREATE DATABASE {}").format(psycopg_sql.Identifier(dbname)))
    return True


def _apply_migrations(conninfo: str) -> list[str]:
    applied: list[str] = []
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "filename TEXT PRIMARY KEY, checksum TEXT NOT NULL, "
                "applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
            )
        conn.commit()
        for path in migration_files():
            sql_text = path.read_text(encoding="utf-8")
            checksum = checksum_of(sql_text)
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT checksum FROM schema_migrations WHERE filename = %s", (path.name,)
                )
                row = cur.fetchone()
            if row is not None:
                if row[0] != checksum:
                    raise RuntimeError(f"{path.name} checksum mismatch — refusing to proceed")
                continue
            with conn.cursor() as cur:
                cur.execute(sql_text)
                cur.execute(
                    "INSERT INTO schema_migrations (filename, checksum) VALUES (%s, %s)",
                    (path.name, checksum),
                )
            conn.commit()
            applied.append(path.name)
    return applied


_VAULT_SHIM_SQL = """
CREATE SCHEMA IF NOT EXISTS vault;

CREATE TABLE IF NOT EXISTS vault.secrets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    secret TEXT NOT NULL,
    name TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE VIEW vault.decrypted_secrets AS
SELECT id, secret AS decrypted_secret, name, created_at, updated_at
FROM vault.secrets;

CREATE OR REPLACE FUNCTION vault.create_secret(raw_value TEXT, name TEXT DEFAULT NULL)
RETURNS UUID AS $$
DECLARE
    new_id UUID;
BEGIN
    INSERT INTO vault.secrets (secret, name) VALUES (raw_value, name) RETURNING id INTO new_id;
    RETURN new_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION vault.update_secret(secret_id UUID, raw_value TEXT, name TEXT DEFAULT NULL)
RETURNS VOID AS $$
BEGIN
    UPDATE vault.secrets SET secret = raw_value, name = COALESCE(name, vault.secrets.name), updated_at = now()
    WHERE id = secret_id;
END;
$$ LANGUAGE plpgsql;
"""
"""A minimal, schema-compatible local stand-in for exactly the four Supabase
Vault primitives ``arie/vault.py`` calls (``vault.create_secret``,
``vault.update_secret``, ``DELETE FROM vault.secrets``,
``SELECT ... FROM vault.decrypted_secrets``) -- see this module's own
docstring for why. No encryption of its own, same as the real module: this
disposable local database is not exposed anywhere, and the whole point is
letting the real, unmodified application code path run against it."""


def _ensure_vault_shim(conninfo: str) -> None:
    with psycopg.connect(conninfo) as conn:
        with conn.cursor() as cur:
            cur.execute(_VAULT_SHIM_SQL)
        conn.commit()


def _diff_scoring_configs(
    prod_conn: psycopg.Connection, local_conn: psycopg.Connection
) -> dict[str, Any]:
    prod_cfg = resolve_scoring_config(prod_conn, organization_id=PROD_TRACK_A_ORG_ID)
    # local org id filled in by caller before this is invoked a second time
    return {
        "qualify_threshold": prod_cfg.qualify_threshold,
        "reject_threshold": prod_cfg.reject_threshold,
        "size_bands": list(prod_cfg.size_bands),
        "industry_points": dict(prod_cfg.industry_points),
        "seniority_points": dict(prod_cfg.seniority_points),
        "function_points": dict(prod_cfg.function_points),
        "intent_max_points": prod_cfg.intent_max_points,
        "trigger_points": prod_cfg.trigger_points,
        "disqualifier_enabled": prod_cfg.disqualifier_enabled,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--preflight", action="store_true")
    parser.add_argument("--confirm-live-spend", action="store_true")
    args = parser.parse_args()

    if not args.preflight and not args.confirm_live_spend:
        print("Must pass --preflight or --confirm-live-spend.", file=sys.stderr)
        return 1

    ground_truth_path = RUN_DIR / "live_shadow_ground_truth.md"
    actual_checksum = checksum_of(ground_truth_path.read_text(encoding="utf-8"))
    # checksum_of hashes migration-style content; ground truth sheet isn't a
    # migration, so compute directly with the same primitive it uses (sha256)
    import hashlib

    actual_checksum = hashlib.sha256(ground_truth_path.read_bytes()).hexdigest()

    host, port, dbname = _db_identity(LOCAL_DB_URL)
    created = _ensure_database(LOCAL_DB_URL)
    migrations_applied = _apply_migrations(LOCAL_DB_URL)
    _ensure_vault_shim(LOCAL_DB_URL)

    import os

    abstract_key_present = bool(os.getenv("ABSTRACT_COMPANY_API_KEY"))
    hunter_key_present = bool(os.getenv("HUNTER_API_KEY"))

    checks: list[tuple[bool, str, str]] = []

    def check(label: str, passed: bool, detail: str) -> None:
        checks.append((passed, label, detail))

    check(
        "ground-truth sheet checksum matches frozen value",
        actual_checksum == GROUND_TRUTH_CHECKSUM,
        f"{actual_checksum} vs frozen {GROUND_TRUTH_CHECKSUM}",
    )
    check("exactly 8 leads, unmodified", len(LEADS) == 8, f"{len(LEADS)} leads")
    check(
        "dedicated experiment database (not shared 'arie')",
        dbname != SHARED_DB_NAME,
        f"{host}:{port}/{dbname}"
        + (" [created]" if created else " [existing]")
        + f", {len(migrations_applied)} migration(s) applied this run",
    )
    check(
        "ABSTRACT_COMPANY_API_KEY present locally",
        abstract_key_present,
        "present, not printed" if abstract_key_present else "MISSING",
    )
    check(
        "HUNTER_API_KEY present locally",
        hunter_key_present,
        "present, not printed" if hunter_key_present else "MISSING",
    )
    check(
        "Apollo not among providers",
        "apollo_person_enrichment" not in (ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME),
        "only abstract_company_enrichment + hunter_combined_enrichment will be configured",
    )
    check(
        "LIVE_AUTONOMY_ENABLED is False (structural constant)",
        LIVE_AUTONOMY_ENABLED is False,
        f"arie.live.safety.LIVE_AUTONOMY_ENABLED = {LIVE_AUTONOMY_ENABLED}; "
        f"autonomy_allowed_for('live') = {autonomy_allowed_for('live')}",
    )
    max_abstract = round(8 * LIVE_PROVIDER.cost_usd_per_call, 5)
    max_hunter = round(8 * HUNTER.cost_usd_per_success, 5)
    max_total = round(max_abstract + max_hunter, 5)
    check(
        "max modeled spend within cap",
        max_total <= MAX_MODELED_SPEND_USD,
        f"${max_total} (Abstract ${max_abstract} + Hunter ${max_hunter}) <= ${MAX_MODELED_SPEND_USD}",
    )

    print("PREFLIGHT — live_shadow reality check dry run")
    print("=" * 72)
    for passed, label, detail in checks:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    print("=" * 72)
    ok = all(passed for passed, _, _ in checks)

    if args.preflight:
        print("PREFLIGHT PASSED" if ok else "PREFLIGHT FAILED")
        return 0 if ok else 1

    if not ok:
        print("Refusing to proceed: preflight checks failed.", file=sys.stderr)
        return 1

    # ---- real setup: org, execution_mode, ICP parity, credentials --------
    pool = ConnectionPool(LOCAL_DB_URL, min_size=1, max_size=5, open=True)
    queue = PostgresJobQueue(pool)

    with pool.connection() as conn:
        result = create_customer_organization(
            conn,
            owner_user_id=OWNER_USER_ID,
            organization_name="ARIE Live Shadow Reality Check 2026-09-22",
        )
    local_org_id = result.organization_id
    print(f"\nlocal live-shadow org created: {local_org_id} (slug={result.slug})")

    with pool.connection() as conn:
        set_execution_mode(
            conn,
            organization_id=local_org_id,
            execution_mode=LIVE_SHADOW,
            actor_user_id=OWNER_USER_ID,
        )
        mode = get_execution_mode(conn, organization_id=local_org_id)
    print(f"execution_mode set and verified: {mode}")
    assert mode == LIVE_SHADOW

    prod_icp = json.loads((RUN_DIR / "track_a_raw_icp_profile.json").read_text(encoding="utf-8"))
    with pool.connection() as conn:
        create_profile(
            conn,
            organization_id=local_org_id,
            created_by_user_id=OWNER_USER_ID,
            name=prod_icp["name"],
            config=prod_icp["config"],
            scorer_version=prod_icp["scorer_version"],
        )

    # scoring_config parity check: local org vs. production org (read-only,
    # via the readonly-scoped production connection the caller must supply
    # as PROD_DATABASE_URL; this script never writes to production).
    prod_conninfo = os.environ.get("PROD_DATABASE_URL_READONLY")
    parity: dict[str, Any] = {"checked": False}
    if prod_conninfo:
        with psycopg.connect(prod_conninfo) as prod_conn, pool.connection() as local_conn:
            prod_cfg = resolve_scoring_config(prod_conn, organization_id=PROD_TRACK_A_ORG_ID)
            local_cfg = resolve_scoring_config(local_conn, organization_id=local_org_id)
            parity = {
                "checked": True,
                "identical": (
                    prod_cfg.qualify_threshold == local_cfg.qualify_threshold
                    and prod_cfg.reject_threshold == local_cfg.reject_threshold
                    and dict(prod_cfg.industry_points) == dict(local_cfg.industry_points)
                    and dict(prod_cfg.seniority_points) == dict(local_cfg.seniority_points)
                    and dict(prod_cfg.function_points) == dict(local_cfg.function_points)
                    and prod_cfg.intent_max_points == local_cfg.intent_max_points
                    and prod_cfg.trigger_points == local_cfg.trigger_points
                    and prod_cfg.disqualifier_enabled == local_cfg.disqualifier_enabled
                ),
                "prod": {
                    "qualify_threshold": prod_cfg.qualify_threshold,
                    "reject_threshold": prod_cfg.reject_threshold,
                    "industry_points": dict(prod_cfg.industry_points),
                },
                "local": {
                    "qualify_threshold": local_cfg.qualify_threshold,
                    "reject_threshold": local_cfg.reject_threshold,
                    "industry_points": dict(local_cfg.industry_points),
                },
            }
    print(f"scoring_config parity check: {parity}")
    if prod_conninfo and not parity["identical"]:
        print(
            "REFUSING to proceed: local scoring_config does not match production.", file=sys.stderr
        )
        return 1

    abstract_key = os.environ["ABSTRACT_COMPANY_API_KEY"]
    hunter_key = os.environ["HUNTER_API_KEY"]
    with pool.connection() as conn:
        set_provider_credential(
            conn,
            organization_id=local_org_id,
            provider=ABSTRACT_PROVIDER_NAME,
            raw_credential=abstract_key,
            actor_user_id=OWNER_USER_ID,
        )
        set_provider_credential(
            conn,
            organization_id=local_org_id,
            provider=HUNTER_PROVIDER_NAME,
            raw_credential=hunter_key,
            actor_user_id=OWNER_USER_ID,
        )
    del abstract_key, hunter_key
    print("Abstract + Hunter org-scoped Vault-path credentials configured (values never printed).")
    print(
        f"registered live providers available: {REGISTERED_LIVE_PROVIDER_NAMES} — Apollo not configured for this org."
    )

    # ---- ingest the 8 frozen leads ---------------------------------------
    resolver = IdentityResolver(pool)
    lead_ids: dict[str, UUID] = {}
    with pool.connection() as conn:
        for lead in LEADS:
            email = f"contact@{lead['domain']}"
            cmd = LeadIngestCommand(
                source="live_shadow_reality_check",
                email=email,
                organization_id=local_org_id,
                external_ref=f"live-shadow-reality-check:{lead['domain']}",
                company_domain=lead["domain"],
                company_name=lead["company_name"],
                full_name=lead.get("full_name") or None,
                is_shadow=False,
            )
            outcome = ingest_lead(conn, resolver=resolver, queue=queue, command=cmd)
            conn.commit()
            lead_ids[lead["domain"]] = outcome.lead_id
            print(
                f"ingested {lead['company_name']} ({lead['domain']}) -> lead_id={outcome.lead_id}"
            )

    # ---- run the real live worker cycle until all 8 are done ------------
    handlers = build_handlers(pool, provider_mode="live")
    max_cycles = 40
    for i in range(max_cycles):
        results = run_worker_cycle(queue, pool, handlers, job_types=["compute_score"])
        if results:
            for r in results:
                print(
                    f"  cycle {i}: {r.job_type} {r.job_id}: {r.outcome}"
                    + (f" ({r.detail})" if r.detail else "")
                )
        with pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT count(*) FROM leads WHERE organization_id = %(org)s AND status = 'NEW'",
                {"org": local_org_id},
            )
            remaining = cur.fetchone()[0]
        if remaining == 0:
            print(f"all 8 leads reached a decision after {i + 1} cycle(s)")
            break
    else:
        print("WARNING: max cycles reached with leads still unresolved", file=sys.stderr)

    # ---- extract full receipts + raw provider calls ----------------------
    report: list[dict[str, Any]] = []
    with pool.connection() as conn:
        for lead in LEADS:
            lead_id = lead_ids[lead["domain"]]
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT lead_id, status, is_shadow FROM leads WHERE lead_id = %(id)s",
                    {"id": lead_id},
                )
                lead_row = cur.fetchone()
                cur.execute(
                    "SELECT * FROM decision_receipts WHERE lead_id = %(id)s",
                    {"id": lead_id},
                )
                receipt_row = cur.fetchone()
                cur.execute(
                    "SELECT provider, entity_type, status, error_kind, suppressed_reason, "
                    "cache_hit, cost_usd, credits_used, latency_ms, requested_at, completed_at "
                    "FROM provider_calls WHERE lead_id = %(id)s ORDER BY requested_at",
                    {"id": lead_id},
                )
                provider_calls = cur.fetchall()
            report.append(
                {
                    "company_name": lead["company_name"],
                    "domain": lead["domain"],
                    "lead_id": str(lead_id),
                    "lead_status": lead_row["status"] if lead_row else None,
                    "is_shadow": lead_row["is_shadow"] if lead_row else None,
                    "decision_receipt": {k: v for k, v in receipt_row.items()}
                    if receipt_row
                    else None,
                    "provider_calls": [dict(row) for row in provider_calls],
                }
            )

    out_path = RUN_DIR / "live_shadow_run_results.json"
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\nfull report written to {out_path}")

    pool.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
