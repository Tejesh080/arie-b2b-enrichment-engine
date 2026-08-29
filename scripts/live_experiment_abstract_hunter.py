"""One controlled, real Abstract + Hunter experiment — Live V1 two-provider proof.

Runs a fixed, small identity list through the real ``compute_score`` pipeline
using ``evaluation_parallel`` strategy (no early stop — both providers are
attempted on every lead, which is what makes the Abstract-vs-Hunter company
comparison and the Hunter title-normalization check meaningful) with
``live_providers=[Abstract, Hunter]`` injected explicitly — Apollo is never
built, never imported into the call graph, never checked for a key. This is
the documented injection point ``arie.jobs.handlers.build_handlers`` already
exposes for tests and ``scripts/live_provider_smoke.py``; no core handler code
changes.

**Database.** Points at the local Compose Postgres
(``postgresql://arie:arie_local_dev@localhost:5432/arie``) by default, never
the deployed Supabase in ``.env``'s ``DATABASE_URL`` — this experiment's leads,
evidence, and cost-ledger rows have no business in the shared/production
database. Override with ``--db-url`` only for a deliberately different local
target.

**Spend discipline.** ``RecordingProvider`` wraps each real adapter and keeps
the full ``ProviderResult`` (including ``raw``) for every call *it itself
made* — the same single real HTTP call the pipeline makes, not a second one —
so the report below can show raw industry/title/company-preview/credits
without ever calling a provider twice for the same identity. A cache-served
answer (Step 11) never reaches ``fetch`` at all, which is how "no new call
recorded" proves cache reuse without spending anything to test it.

Never runs without ``--confirm-live-spend``.

    python scripts/live_experiment_abstract_hunter.py --confirm-live-spend
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

from arie.api.ingest import LeadIngestCommand, ingest_lead
from arie.api.main import build_state
from arie.api.receipt import build_receipt
from arie.config import HUNTER, LIVE_PROVIDER
from arie.core.types import Entity, EntityType, ProviderResult
from arie.jobs.handlers import build_handlers
from arie.jobs.worker import run_worker_cycle
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider
from arie.providers.live_hunter import HunterEnrichmentProvider

LOCAL_DB_URL = "postgresql://arie:arie_local_dev@localhost:5432/arie"

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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--confirm-live-spend",
        action="store_true",
        help="Required. Acknowledges this makes real Abstract + Hunter calls.",
    )
    parser.add_argument("--db-url", default=LOCAL_DB_URL, help="Postgres to run against.")
    parser.add_argument(
        "--run-id", default=None, help="Idempotency prefix; a fresh one is generated if omitted."
    )
    parser.add_argument(
        "--cache-test-index",
        type=int,
        default=0,
        help="Index into IDENTITIES to re-ingest as a second lead, proving cache reuse.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("data/evaluation/runs/abstract-hunter-live-1"),
        help="Directory for the raw JSON artifact (gitignored run data).",
    )
    args = parser.parse_args()

    if not LIVE_PROVIDER.configured or not HUNTER.configured:
        print("ABSTRACT_COMPANY_API_KEY / HUNTER_API_KEY not both set.", file=sys.stderr)
        return 1

    if not args.confirm_live_spend:
        print(
            "Refusing to run without --confirm-live-spend: this makes real Abstract + Hunter "
            "calls against the five identities in IDENTITIES.",
            file=sys.stderr,
        )
        return 1

    run_id = args.run_id or f"live-ah-{uuid.uuid4().hex[:10]}"
    print(f"run_id={run_id}  db={args.db_url}")

    state = build_state(args.db_url)
    abstract = RecordingProvider(AbstractCompanyEnrichmentProvider.build())
    hunter = RecordingProvider(HunterEnrichmentProvider.build())

    # evaluation_parallel: no early stop, both providers attempted on every
    # lead (see module docstring) — Apollo is simply absent from this list,
    # never built, never referenced.
    handlers = build_handlers(
        state.pool,
        provider_mode="live",
        live_providers=[abstract, hunter],
        live_strategy="evaluation_parallel",
    )

    try:
        lead_ids: list[UUID] = []
        for index, identity in enumerate(IDENTITIES):
            with state.pool.connection() as conn:
                command = LeadIngestCommand(
                    source="live-experiment-abstract-hunter",
                    email=identity["email"],
                    external_ref=f"{run_id}:{index}",
                    company_domain=identity["domain"],
                    company_name=identity["company_name"],
                    is_shadow=False,
                )
                result = ingest_lead(
                    conn, resolver=state.resolver, queue=state.queue, command=command
                )
                conn.commit()
            lead_ids.append(result.lead_id)
            print(f"ingested [{index}] {identity['email']} -> lead_id={result.lead_id}")

        processed = 0
        for _ in range(len(IDENTITIES) * 2):
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
        cache_identity = IDENTITIES[args.cache_test_index]
        abstract_calls_before = abstract.call_count(cache_identity["domain"])
        hunter_calls_before = hunter.call_count(cache_identity["email"])
        with state.pool.connection() as conn:
            command = LeadIngestCommand(
                source="live-experiment-abstract-hunter",
                email=cache_identity["email"],
                external_ref=f"{run_id}:cache-test",
                company_domain=cache_identity["domain"],
                company_name=cache_identity["company_name"],
                is_shadow=False,
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

        report: dict[str, Any] = {"run_id": run_id, "leads": []}
        with state.pool.connection() as conn:
            for identity, lead_id in zip(IDENTITIES, lead_ids, strict=True):
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
                        "hunter_raw_call": _result_to_dict(_first_call(hunter, identity["email"])),
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

        args.out.mkdir(parents=True, exist_ok=True)
        out_path = args.out / f"{run_id}.json"
        out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
        print(f"\nartifact written: {out_path}")
        return 0
    finally:
        abstract.close()
        hunter.close()
        state.pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
