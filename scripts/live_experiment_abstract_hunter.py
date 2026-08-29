"""One controlled, real Abstract + Hunter experiment — Live V1 two-provider proof.

Runs an identity list through the real ``compute_score`` pipeline using
``evaluation_parallel`` strategy (no early stop — both providers are attempted
on every lead, which is what makes the Abstract-vs-Hunter company comparison
and the Hunter title-normalization check meaningful) with
``live_providers=[Abstract, Hunter]`` injected explicitly — Apollo is never
built, never imported into the call graph, never checked for a key. This is
the documented injection point ``arie.jobs.handlers.build_handlers`` already
exposes for tests and ``scripts/live_provider_smoke.py``; no core handler code
changes.

**Two identity sources.** With no ``--identities-file``, runs the original
fixed five (``IDENTITIES`` below) against the shared local Compose database.
With ``--identities-file``, loads a Phase-1-style validation dataset JSON
(``{"identities": [{"email", "company_domain", "company", "expected_title",
"ground_truth_quality", ...}, ...]}``) and keeps only ``HIGH``/``MEDIUM`` rows
— a ``LOW``-quality row (e.g. an unverified carry-over) is excluded
automatically, never opt-in.

**Database.** The original five default to the shared local Compose Postgres
(``postgresql://arie:arie_local_dev@localhost:5432/arie``) — never the
deployed Supabase in ``.env``'s ``DATABASE_URL``. A ``--identities-file`` run
defaults instead to a *dedicated* sibling database on that same server
(``.../arie_live_validation``, see ``VALIDATION_DB_URL``) and refuses outright
to target the shared ``arie`` database: docker-compose's always-on ``api``/
``worker`` containers (``restart: unless-stopped``, polling via ``SKIP
LOCKED``) poll it continuously and would race this run — exactly the race a
prior run of this experiment hit. The dedicated database is created and fully
migrated automatically (idempotent, same technique as ``scripts/migrate.py``)
before anything else happens. Override either default with ``--db-url``.

**Spend discipline.** ``RecordingProvider`` wraps each real adapter and keeps
the full ``ProviderResult`` (including ``raw``) for every call *it itself
made* — the same single real HTTP call the pipeline makes, not a second one —
so the report below can show raw industry/title/company-preview/credits
without ever calling a provider twice for the same identity. A cache-served
answer (Step 11) never reaches ``fetch`` at all, which is how "no new call
recorded" proves cache reuse without spending anything to test it.

``--preflight`` validates identity count, providers, database, and the
approved cost ceiling, then exits — it never ingests a lead or calls a
provider, and does not require ``--confirm-live-spend``. Otherwise, never
runs without ``--confirm-live-spend``.

**Two strategies.** ``--strategy evaluation_parallel`` (the default) is the
proof above — no early stop, both providers on every lead. ``--strategy
option_c`` instead runs Abstract first and calls Hunter only when
``arie.jobs.handlers._option_c_stop_check`` judges its fields
decision-relevant (see ``arie.live.person_relevance``), via
``build_handlers``'s ``live_stop_check`` injection point — not a third named
``LiveStrategy``, so the receipt's own ``policy_name`` still reads
``live_optimized``; ``stopping.reason_code`` (``person_evidence_not_material``
for a skip) is the truthful signal for which rule actually ran. ``--select``
restricts a ``--identities-file`` run to specific ``validation_id``s (e.g.
``--select v02,v11,v01,v09``) — for a small, targeted live verification
against a handful of leads rather than the full set.

    python scripts/live_experiment_abstract_hunter.py --confirm-live-spend

    python scripts/live_experiment_abstract_hunter.py \\
        --identities-file data/evaluation/runs/validation-20-2026-08-30/identities.json \\
        --preflight

    python scripts/live_experiment_abstract_hunter.py \\
        --identities-file data/evaluation/runs/validation-20-2026-08-30/identities.json \\
        --select v02,v11,v01,v09 --strategy option_c --confirm-live-spend
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import psycopg
from psycopg import sql as psycopg_sql

from arie.api.ingest import LeadIngestCommand, ingest_lead
from arie.api.main import build_state
from arie.api.receipt import build_receipt
from arie.config import HUNTER, LIVE_PROVIDER
from arie.core.types import Entity, EntityType, ProviderResult
from arie.jobs.handlers import _option_c_stop_check, build_handlers
from arie.jobs.worker import run_worker_cycle
from arie.migrations import checksum_of, migration_files
from arie.providers.hunter_contract import HUNTER_PROVIDER_NAME
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider
from arie.providers.live_hunter import HunterEnrichmentProvider

LOCAL_DB_URL = "postgresql://arie:arie_local_dev@localhost:5432/arie"
"""The shared local Compose database — what the original five identities run
against, and what docker-compose's always-on api/worker containers poll."""

SHARED_DB_NAME = "arie"

VALIDATION_DB_URL = "postgresql://arie:arie_local_dev@localhost:5432/arie_live_validation"
"""Dedicated sibling database, same server, for ``--identities-file`` runs —
distinct enough that the compose workers' ``SKIP LOCKED`` polling of
``arie``'s ``jobs`` table never sees a row inserted here."""

DEFAULT_EXPECTED_COUNT = 15
"""``--preflight``'s default expectation — the size of the approved
validation-20-2026-08-30 HIGH/MEDIUM set. Override with ``--expected-count``
for a different dataset."""

MAX_MODELED_SPEND_USD = 0.09825
MAX_HUNTER_CREDITS = 3.0
"""The approved Phase 4 cost ceiling for the 15-identity validation-20 set
(15 x $0.00165 Abstract + 15 x 0.2 Hunter credits at $0.0049/success), fixed
at the value approved before any provider call — not recomputed from whatever
``LIVE_PROVIDER``/``HUNTER`` happen to be configured to when this runs, so a
later rate change can't silently raise the spend a human already signed off
on without ``--preflight`` catching it."""

# Exactly the five identities specified for this run. Do not add, remove, or
# substitute — a MISS is a valid result, not a reason to try a different
# address (see the module docstring's spend-discipline note).
IDENTITIES: list[dict[str, str]] = [
    {
        "email": "jason@37signals.com",
        "domain": "37signals.com",
        "company_name": "37signals",
        "expected_title": "Founder & CEO",
    },
    {
        "email": "patrick@stripe.com",
        "domain": "stripe.com",
        "company_name": "Stripe",
        "expected_title": "Co-founder & CEO",
    },
    {
        "email": "tobi@shopify.com",
        "domain": "shopify.com",
        "company_name": "Shopify",
        "expected_title": "CEO",
    },
    {
        "email": "ahsan@marakor.com",
        "domain": "marakor.com",
        "company_name": "Marakor",
        "expected_title": "Founder & CEO",
    },
    {
        "email": "ahmed@orderii.co",
        "domain": "orderii.co",
        "company_name": "Orderii LLC",
        "expected_title": "Co-Founder & CTO",
    },
]


@dataclass
class RecordingProvider:
    """Wraps a real adapter, keeping every ``ProviderResult`` it returns.

    Delegates every ``EnrichmentProvider`` property/method to ``inner`` so it
    is structurally indistinguishable to ``arie.jobs.handlers`` — the pipeline
    calls exactly the real adapter it would have called without this wrapper;
    this only *also* remembers what came back.
    """

    inner: Any
    calls: dict[str, list[ProviderResult]] = field(default_factory=dict)
    """Every ``fetch`` call this wrapper made, keyed by ``canonical_key`` — a
    list, not the last value, so a cache test can compare call *counts*
    before/after rather than mistake "still present from an earlier lead" for
    "called again"."""

    @property
    def name(self) -> str:
        return str(self.inner.name)

    @property
    def entity_type(self) -> EntityType:
        return self.inner.entity_type  # type: ignore[no-any-return]

    @property
    def provides_fields(self) -> tuple[str, ...]:
        return self.inner.provides_fields  # type: ignore[no-any-return]

    @property
    def base_cost_usd(self) -> float:
        return float(self.inner.base_cost_usd)

    @property
    def p50_latency_ms(self) -> int:
        return int(self.inner.p50_latency_ms)

    @property
    def p95_latency_ms(self) -> int:
        return int(self.inner.p95_latency_ms)

    def fetch(self, entity: Entity) -> ProviderResult:
        result: ProviderResult = self.inner.fetch(entity)
        self.calls.setdefault(entity.canonical_key, []).append(result)
        return result

    def call_count(self, canonical_key: str) -> int:
        return len(self.calls.get(canonical_key, []))

    def close(self) -> None:
        self.inner.close()


def _receipt_to_dict(receipt: Any) -> dict[str, Any] | None:
    return asdict(receipt) if receipt is not None else None


def _first_call(provider: RecordingProvider, canonical_key: str) -> ProviderResult | None:
    calls = provider.calls.get(canonical_key)
    return calls[0] if calls else None


def _result_to_dict(result: ProviderResult | None) -> dict[str, Any] | None:
    if result is None:
        return None
    return {
        "status": str(result.status),
        "fields": result.fields,
        "confidence": result.confidence,
        "cost_usd": result.cost_usd,
        "latency_ms": round(result.latency_ms, 1),
        "raw": result.raw,
    }


def _ingest_command(
    identity: dict[str, str], *, source: str, external_ref: str
) -> LeadIngestCommand:
    """Builds the ``LeadIngestCommand`` for one identity, carrying its
    ``full_name`` through when the identity dict has one.

    This is the fix for the gap the validation-20 real run exposed: without a
    requested ``full_name``, ``arie.identity.validation.validate_identity``
    (already correct — see ``tests/unit/test_identity_validation.py``) has
    nothing to compare a person-provider's ``matched_identity`` against, so a
    same-domain wrong-person match (Patrick Bosmans, not Patrick Collison, at
    ``patrick@stripe.com``) reads no worse than PROBABLE and its fields stay
    scoreable. ``identity.get("full_name")`` is ``""``/absent for the
    hardcoded five-identity ``IDENTITIES`` list (unchanged, no name on file
    there) and the real name for anything loaded via ``--identities-file`` —
    ``or None`` normalizes an empty string to ``None`` rather than passing a
    falsy-but-not-``None`` value through to identity resolution.
    """
    return LeadIngestCommand(
        source=source,
        email=identity["email"],
        external_ref=external_ref,
        company_domain=identity["domain"],
        company_name=identity["company_name"],
        full_name=identity.get("full_name") or None,
        is_shadow=False,
    )


def _load_identities_from_file(path: Path) -> tuple[list[dict[str, str]], int]:
    """Loads HIGH/MEDIUM identities from a Phase-1 validation dataset JSON.

    Returns ``(identities, excluded_count)``. A row whose ``ground_truth_quality``
    is not ``HIGH``/``MEDIUM`` (e.g. the ``LOW`` carry-over flagged in
    validation-20-2026-08-30) is dropped automatically — it is in that file
    specifically because it failed this project's own verification bar, so
    loading it silently would undo Phase 2's point.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    eligible: list[dict[str, str]] = []
    excluded = 0
    for row in payload["identities"]:
        if row.get("ground_truth_quality") not in ("HIGH", "MEDIUM"):
            excluded += 1
            continue
        eligible.append(
            {
                "email": row["email"],
                "domain": row["company_domain"],
                "company_name": row["company"],
                "expected_title": row["expected_title"],
                "full_name": row.get("full_name", ""),
                "validation_id": row.get("validation_id", ""),
                "ground_truth_quality": row["ground_truth_quality"],
            }
        )
    return eligible, excluded


def _select_identities(identities: list[dict[str, str]], select: str) -> list[dict[str, str]]:
    """Restricts ``identities`` to the comma-separated ``validation_id``s in
    ``select``, in the order given (not the file's own order) — so a
    verification run can list "the two calls, then the two skips" and have
    the printed/ingested order match.

    Raises ``ValueError`` naming any id not present, rather than silently
    ingesting fewer leads than asked for — the same "never silently drop"
    discipline ``--identities-file``'s own LOW-quality exclusion documents
    its departure from, deliberately, one line up.
    """
    wanted = [vid.strip() for vid in select.split(",") if vid.strip()]
    by_id = {identity.get("validation_id", ""): identity for identity in identities}
    missing = [vid for vid in wanted if vid not in by_id]
    if missing:
        raise ValueError(
            f"--select named validation_id(s) not present among eligible identities: "
            f"{missing}. Available: {sorted(by_id)}."
        )
    return [by_id[vid] for vid in wanted]


def _db_identity(conninfo: str) -> tuple[str, str, str]:
    """(host, port, dbname) — what makes two connection strings the same database."""
    parts = urlsplit(conninfo)
    return ((parts.hostname or "").lower(), str(parts.port or 5432), parts.path.lstrip("/").lower())


def _ensure_database(conninfo: str) -> bool:
    """Create the target Postgres database if it doesn't exist yet. Returns
    True if it was created.

    Connects to the server's ``postgres`` maintenance database to issue
    ``CREATE DATABASE`` — the same technique ``scripts/test_db.py``'s
    ``designate`` step uses to stand up a disposable sibling database on the
    same Compose Postgres server.
    """
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
    """Apply pending ``migrations/*.sql`` against ``conninfo``. Returns the
    filenames applied.

    Deliberately a small duplicate of ``scripts/migrate.py``'s ``migrate()``
    rather than an import of it: per ``arie.migrations``'s own module
    docstring, ``scripts/`` only resolves on ``sys.path`` when the process was
    launched as a module from the repo root, which this script's own
    documented ``python scripts/live_experiment_abstract_hunter.py``
    invocation is not. ``arie.migrations`` (an installed package) is safe to
    import from anywhere, so the primitives come from there.
    """
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
                    raise RuntimeError(
                        f"{path.name} was already applied with a different checksum. A "
                        "migration must never be edited after it has run anywhere — add a "
                        "new migration instead."
                    )
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


def _run_preflight(
    *,
    identities: list[dict[str, str]],
    excluded_count: int,
    expected_count: int,
    db_url: str,
    abstract_name: str,
    hunter_name: str,
    migrations_applied: list[str],
) -> bool:
    """Prints the dry-run report and returns whether every check passed.

    Never ingests a lead, never opens a worker cycle, never calls a provider
    — everything it checks is either static (which providers are wired in),
    already-done-by-this-point (database created + migrated), or arithmetic
    (cost ceiling from the configured per-call rates times the identity
    count).
    """
    n = len(identities)
    max_abstract_usd = round(n * LIVE_PROVIDER.cost_usd_per_call, 5)
    max_hunter_usd = round(n * HUNTER.cost_usd_per_success, 5)
    max_credits = round(n * HUNTER.credits_per_success, 2)
    max_total_usd = round(max_abstract_usd + max_hunter_usd, 5)
    host, port, dbname = _db_identity(db_url)

    ok = True
    results: list[tuple[bool, str, str]] = []

    def check(label: str, passed: bool, detail: str) -> None:
        nonlocal ok
        ok = ok and passed
        results.append((passed, label, detail))

    check(
        "eligible identities loaded",
        n == expected_count,
        f"{n} loaded (excluded {excluded_count} LOW-quality) — expected {expected_count}",
    )
    check(
        "providers: Abstract + Hunter only",
        {abstract_name, hunter_name} == {ABSTRACT_PROVIDER_NAME, HUNTER_PROVIDER_NAME},
        f"live_providers = [{abstract_name}, {hunter_name}]",
    )
    check(
        "Apollo disabled",
        True,
        "never imported, never built, absent from live_providers by construction",
    )
    check(
        "dedicated experiment database",
        dbname != SHARED_DB_NAME,
        f"{host}:{port}/{dbname}"
        + (
            " — REFUSED: this is the shared database docker-compose's api/worker "
            "containers poll continuously"
            if dbname == SHARED_DB_NAME
            else f" — schema migrated ({len(migrations_applied)} migration(s) applied this run)"
        ),
    )
    check(
        "live autonomy disabled",
        True,
        "this script has no outbound action executor — every outcome lands only in "
        "decision_receipts/human_reviews on the dedicated database, never an autonomous "
        "production decision",
    )
    check(
        "maximum modeled spend within approved cap",
        max_total_usd <= MAX_MODELED_SPEND_USD,
        f"${max_total_usd} (Abstract ${max_abstract_usd} + Hunter ${max_hunter_usd}) "
        f"<= ${MAX_MODELED_SPEND_USD}",
    )
    check(
        "maximum Hunter credits within approved cap",
        max_credits <= MAX_HUNTER_CREDITS,
        f"{max_credits} <= {MAX_HUNTER_CREDITS}",
    )

    print("PREFLIGHT — validation experiment dry run")
    print("=" * 72)
    for passed, label, detail in results:
        print(f"[{'PASS' if passed else 'FAIL'}] {label}: {detail}")
    print("=" * 72)
    print(
        "PREFLIGHT PASSED — safe to re-run with --confirm-live-spend"
        if ok
        else "PREFLIGHT FAILED — do not proceed"
    )
    return ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--confirm-live-spend",
        action="store_true",
        help="Required outside --preflight. Acknowledges this makes real Abstract + Hunter calls.",
    )
    parser.add_argument(
        "--identities-file",
        type=Path,
        default=None,
        help=(
            "Phase-1 validation dataset JSON to load identities from "
            "(e.g. data/evaluation/runs/validation-20-2026-08-30/identities.json). "
            "LOW-quality rows are excluded automatically. Omit to use the original "
            "hardcoded five against the shared local database."
        ),
    )
    parser.add_argument(
        "--select",
        default=None,
        help=(
            "Comma-separated validation_ids to restrict a --identities-file run to "
            "(e.g. v02,v11,v01,v09) — for a small, targeted live run against specific "
            "leads rather than the full set. Order is preserved; unknown ids are a "
            "hard error, never silently dropped."
        ),
    )
    parser.add_argument(
        "--strategy",
        choices=("evaluation_parallel", "option_c"),
        default="evaluation_parallel",
        help=(
            "evaluation_parallel (default): both providers on every lead, no early "
            "stop. option_c: Abstract first, Hunter only when "
            "arie.jobs.handlers._option_c_stop_check judges it decision-relevant — "
            "injected via build_handlers' live_stop_check, not a third named "
            "LiveStrategy (see the module docstring)."
        ),
    )
    parser.add_argument(
        "--preflight",
        action="store_true",
        help=(
            "Validate identity count, providers, database, and the approved cost "
            "ceiling, then exit — never ingests a lead or calls a provider. Does not "
            "require --confirm-live-spend."
        ),
    )
    parser.add_argument(
        "--expected-count",
        type=int,
        default=DEFAULT_EXPECTED_COUNT,
        help=f"--preflight's expected identity count (default {DEFAULT_EXPECTED_COUNT}).",
    )
    parser.add_argument(
        "--db-url",
        default=None,
        help=(
            "Postgres to run against. Defaults to the shared local Compose database "
            f"({LOCAL_DB_URL}) for the original five identities, or to a dedicated "
            f"database ({VALIDATION_DB_URL}) when --identities-file is given."
        ),
    )
    parser.add_argument(
        "--run-id", default=None, help="Idempotency prefix; a fresh one is generated if omitted."
    )
    parser.add_argument(
        "--cache-test-index",
        type=int,
        default=0,
        help="Index into the identity list to re-ingest as a second lead, proving cache reuse.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Directory for the raw JSON artifact (gitignored run data). Defaults next "
            "to --identities-file, or to data/evaluation/runs/abstract-hunter-live-1 "
            "for the original five."
        ),
    )
    args = parser.parse_args()

    if not LIVE_PROVIDER.configured or not HUNTER.configured:
        print("ABSTRACT_COMPANY_API_KEY / HUNTER_API_KEY not both set.", file=sys.stderr)
        return 1

    if args.identities_file is not None:
        identities, excluded_count = _load_identities_from_file(args.identities_file)
    else:
        identities, excluded_count = IDENTITIES, 0

    if args.select is not None:
        if args.identities_file is None:
            print("--select requires --identities-file.", file=sys.stderr)
            return 1
        try:
            identities = _select_identities(identities, args.select)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    db_url: str = args.db_url or (VALIDATION_DB_URL if args.identities_file else LOCAL_DB_URL)
    out_dir: Path = args.out or (
        args.identities_file.parent
        if args.identities_file
        else Path("data/evaluation/runs/abstract-hunter-live-1")
    )

    if args.identities_file is not None and _db_identity(db_url)[2] == SHARED_DB_NAME:
        print(
            f"Refusing to run this {len(identities)}-identity validation experiment against "
            f"the shared '{SHARED_DB_NAME}' database — docker-compose's always-on api/worker "
            "containers poll it continuously and would race this run. Pass --db-url for a "
            f"dedicated database (default: {VALIDATION_DB_URL}).",
            file=sys.stderr,
        )
        return 1

    if not args.preflight and not args.confirm_live_spend:
        print(
            "Refusing to run without --confirm-live-spend: this makes real Abstract + Hunter "
            f"calls against {len(identities)} identities.",
            file=sys.stderr,
        )
        return 1

    created = _ensure_database(db_url)
    if created:
        print(f"created database: {_db_identity(db_url)[2]!r}")
    migrations_applied = _apply_migrations(db_url)
    print(
        f"migrations applied: {len(migrations_applied)} ({', '.join(migrations_applied)})"
        if migrations_applied
        else "migrations: schema already up to date"
    )

    run_id = args.run_id or f"live-ah-{uuid.uuid4().hex[:10]}"
    print(f"run_id={run_id}  db={db_url}  identities={len(identities)}")

    abstract = RecordingProvider(AbstractCompanyEnrichmentProvider.build())
    hunter = RecordingProvider(HunterEnrichmentProvider.build())
    try:
        if args.preflight:
            passed = _run_preflight(
                identities=identities,
                excluded_count=excluded_count,
                expected_count=args.expected_count,
                db_url=db_url,
                abstract_name=abstract.name,
                hunter_name=hunter.name,
                migrations_applied=migrations_applied,
            )
            return 0 if passed else 1

        state = build_state(db_url)
        try:
            # evaluation_parallel: no early stop, both providers attempted on every
            # lead (see module docstring) — Apollo is simply absent from this list,
            # never built, never referenced. option_c: the default "optimized"
            # strategy resolution, with Hunter gated by _option_c_stop_check via
            # live_stop_check instead of evaluation_parallel's always-call-both.
            handlers = (
                build_handlers(
                    state.pool,
                    provider_mode="live",
                    live_providers=[abstract, hunter],
                    live_stop_check=_option_c_stop_check,
                )
                if args.strategy == "option_c"
                else build_handlers(
                    state.pool,
                    provider_mode="live",
                    live_providers=[abstract, hunter],
                    live_strategy="evaluation_parallel",
                )
            )

            lead_ids: list[UUID] = []
            for index, identity in enumerate(identities):
                with state.pool.connection() as conn:
                    command = _ingest_command(
                        identity,
                        source="live-experiment-abstract-hunter",
                        external_ref=f"{run_id}:{index}",
                    )
                    result = ingest_lead(
                        conn, resolver=state.resolver, queue=state.queue, command=command
                    )
                    conn.commit()
                lead_ids.append(result.lead_id)
                print(f"ingested [{index}] {identity['email']} -> lead_id={result.lead_id}")

            processed = 0
            for _ in range(len(identities) * 2):
                cycle = run_worker_cycle(
                    state.queue, state.pool, handlers, batch_size=1, job_types=["compute_score"]
                )
                if not cycle:
                    break
                for outcome in cycle:
                    print(
                        f"processed job {outcome.job_id} type={outcome.job_type} -> {outcome.outcome}"
                    )
                processed += len(cycle)
            print(f"jobs processed: {processed}")

            cache_lead_id: UUID | None = None
            cache_identity = identities[args.cache_test_index]
            abstract_calls_before = abstract.call_count(cache_identity["domain"])
            hunter_calls_before = hunter.call_count(cache_identity["email"])
            with state.pool.connection() as conn:
                command = _ingest_command(
                    cache_identity,
                    source="live-experiment-abstract-hunter",
                    external_ref=f"{run_id}:cache-test",
                )
                cache_result = ingest_lead(
                    conn, resolver=state.resolver, queue=state.queue, command=command
                )
                conn.commit()
            cache_lead_id = cache_result.lead_id
            print(f"cache-test ingested {cache_identity['email']} -> lead_id={cache_lead_id}")
            for _ in range(3):
                cycle = run_worker_cycle(
                    state.queue, state.pool, handlers, batch_size=1, job_types=["compute_score"]
                )
                if not cycle:
                    break

            report: dict[str, Any] = {
                "run_id": run_id,
                "identities_file": str(args.identities_file) if args.identities_file else None,
                "excluded_low_quality_count": excluded_count,
                "strategy": args.strategy,
                "selected_validation_ids": (
                    [identity.get("validation_id") for identity in identities]
                    if args.select
                    else None
                ),
                "leads": [],
            }
            with state.pool.connection() as conn:
                for identity, lead_id in zip(identities, lead_ids, strict=True):
                    receipt = build_receipt(conn, state.ledger, lead_id)
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT evidence_snapshot FROM decision_receipts WHERE lead_id = %(lead_id)s",
                            {"lead_id": lead_id},
                        )
                        row = cur.fetchone()
                        evidence_snapshot = row[0] if row else None
                    report["leads"].append(
                        {
                            "identity": identity,
                            "lead_id": str(lead_id),
                            "receipt": _receipt_to_dict(receipt),
                            "evidence_snapshot": evidence_snapshot,
                            "abstract_raw_call": _result_to_dict(
                                _first_call(abstract, identity["domain"])
                            ),
                            "hunter_raw_call": _result_to_dict(
                                _first_call(hunter, identity["email"])
                            ),
                        }
                    )

                cache_receipt = (
                    build_receipt(conn, state.ledger, cache_lead_id) if cache_lead_id else None
                )
                report["cache_test"] = {
                    "identity": cache_identity,
                    "lead_id": str(cache_lead_id) if cache_lead_id else None,
                    "receipt": _receipt_to_dict(cache_receipt),
                    "abstract_calls_before": abstract_calls_before,
                    "abstract_calls_after": abstract.call_count(cache_identity["domain"]),
                    "hunter_calls_before": hunter_calls_before,
                    "hunter_calls_after": hunter.call_count(cache_identity["email"]),
                }

            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{run_id}.json"
            out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
            print(f"\nartifact written: {out_path}")
            return 0
        finally:
            state.pool.close()
    finally:
        abstract.close()
        hunter.close()


if __name__ == "__main__":
    raise SystemExit(main())
