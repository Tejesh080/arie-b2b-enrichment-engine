"""Post-M1 P5 — one controlled, real call to the live enrichment provider.

Two independent things this script can do, gated separately so a plain
invocation never makes more than one real call:

1. Call ``AbstractCompanyEnrichmentProvider`` directly for one domain and print
   the normalized result (status, fields, cost, latency) — proves the adapter
   works against the real API. This is the default behaviour.
1b. With ``--person-email``: also call ``ApolloPersonEnrichmentProvider`` once
   for that email. A separate flag, a separate vendor, a separate credit — an
   operator smoke-testing the company adapter must not silently spend an Apollo
   credit too. Refuses up front if ``APOLLO_API_KEY`` is unset, before the
   Abstract call has been made.
2. If ``--api-base-url`` is also given: POST a lead to a *running* ARIE API
   (assumed already started with ``PROVIDER_MODE=live`` and the same
   ``ABSTRACT_COMPANY_API_KEY``), wait for its Decision Receipt, and print it —
   proves the adapter is wired into the real pipeline (evidence, ledger,
   state machine, receipt). This makes a *second*, independent real call —
   the script says so before doing it.

Never runs without ``--confirm-live-spend``: Abstract's free tier is 100
requests/month, and this script's whole point is to spend one (or two) of
them deliberately, not by accident.

    python scripts/live_provider_smoke.py --domain github.com --confirm-live-spend
    python scripts/live_provider_smoke.py --domain github.com --confirm-live-spend \\
        --person-email someone@github.com
    python scripts/live_provider_smoke.py --domain github.com --confirm-live-spend \\
        --api-base-url http://localhost:8000 --shadow
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from typing import Any

import httpx

from arie.config import APOLLO_PERSON, LIVE_PROVIDER
from arie.core.types import Entity
from arie.providers.live_abstract import AbstractCompanyEnrichmentProvider
from arie.providers.live_apollo import ApolloPersonEnrichmentProvider


def _print_json(label: str, payload: dict[str, Any]) -> None:
    print(f"\n--- {label} ---")
    print(json.dumps(payload, indent=2, default=str))


def _call_adapter_once(domain: str) -> dict[str, Any]:
    provider = AbstractCompanyEnrichmentProvider.build()
    try:
        entity = Entity(entity_type="company", entity_id=uuid.uuid4(), canonical_key=domain)
        result = provider.fetch(entity)
    finally:
        provider.close()

    # `raw` never contains the api_key (arie.providers.live_abstract's own
    # module docstring states why), but this is still the one place a
    # deliberately-narrow print is worth being explicit about: never dump
    # `provider.config` or anything else that could carry the key.
    return {
        "provider": provider.name,
        "domain": domain,
        "status": str(result.status),
        "fields": result.fields,
        "confidence": result.confidence,
        "cost_usd": result.cost_usd,
        "latency_ms": round(result.latency_ms, 1),
        "raw": result.raw,
    }


def _call_person_adapter_once(email: str) -> dict[str, Any]:
    """One real Apollo People Enrichment call, for one email.

    Consumes at most one Apollo credit (a demographics match); a no-match
    response consumes none. ``reveal_personal_emails``/``reveal_phone_number``
    are hard-coded off in the adapter, so this can never trip the eight-credit
    phone-reveal charge.

    Prints the normalized fields, the raw->canonical mapping audit, and who
    Apollo matched — the last of those because the failure mode person
    enrichment has and company enrichment does not is *matching the wrong
    human*, and that is not visible from the canonical values alone.
    """
    provider = ApolloPersonEnrichmentProvider.build()
    try:
        entity = Entity(entity_type="person", entity_id=uuid.uuid4(), canonical_key=email)
        result = provider.fetch(entity)
    finally:
        provider.close()

    # `raw` never contains the api_key — it travels as a header, and no error
    # path interpolates an httpx exception's str(). Still: never print
    # `provider.config`, which holds the key itself.
    return {
        "provider": provider.name,
        "email": email,
        "status": str(result.status),
        "fields": result.fields,
        "confidence": result.confidence,
        "cost_usd": result.cost_usd,
        "cost_basis": result.raw.get("cost_basis"),
        "credits_consumed": result.raw.get("credits_consumed", 0),
        "latency_ms": round(result.latency_ms, 1),
        "raw": result.raw,
    }


def _run_through_api(
    *, base_url: str, domain: str, shadow: bool, timeout_s: float
) -> dict[str, Any]:
    email = f"live-smoke-{uuid.uuid4().hex[:8]}@{domain}"
    external_ref = f"live-smoke-{uuid.uuid4().hex[:12]}"
    payload = {
        "source": "live-provider-smoke",
        "email": email,
        "external_ref": external_ref,
        "company_domain": domain,
        "mode": "shadow" if shadow else "normal",
    }

    with httpx.Client(base_url=base_url, timeout=10.0) as client:
        response = client.post("/leads", json=payload)
        response.raise_for_status()
        ingested = response.json()
        lead_id = ingested["lead_id"]

        deadline = time.monotonic() + timeout_s
        receipt = client.get(f"/leads/{lead_id}/receipt").json()
        while receipt.get("status") == "pending":
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"lead {lead_id} did not reach a decision within {timeout_s:.0f}s "
                    f"(last lead_status={receipt.get('lead_status')!r}) — is a worker with "
                    "PROVIDER_MODE=live running against this API?"
                )
            time.sleep(1.0)
            receipt = client.get(f"/leads/{lead_id}/receipt").json()

    return {"ingest": ingested, "receipt": receipt}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--domain", default="github.com", help="Company domain to enrich (default: github.com)"
    )
    parser.add_argument(
        "--confirm-live-spend",
        action="store_true",
        help="Required. Acknowledges this call may spend real provider quota/money.",
    )
    parser.add_argument(
        "--person-email",
        default=None,
        help="If set, ALSO make one real Apollo People Enrichment call for this email "
        "(consumes at most one Apollo credit; a no-match consumes none). Requires "
        "APOLLO_API_KEY. Use a public business identity, not a private individual.",
    )
    parser.add_argument(
        "--api-base-url",
        default=None,
        help="If set, also POST a lead through this running ARIE API (PROVIDER_MODE=live) "
        "and print its Decision Receipt. Makes a SECOND real provider call.",
    )
    parser.add_argument(
        "--shadow",
        action="store_true",
        help="With --api-base-url: ingest the lead in shadow mode instead of normal mode.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=60.0,
        help="With --api-base-url: how long to wait for the receipt to decide (default: 60s).",
    )
    args = parser.parse_args()

    if not LIVE_PROVIDER.configured:
        print(
            "ABSTRACT_COMPANY_API_KEY is not set — see .env.example. Implementation is complete; "
            "live verification is blocked pending that key.",
            file=sys.stderr,
        )
        return 1

    # Checked before anything is spent, not after the Abstract call has already
    # gone out: the point of this gate is that an operator who asked for a
    # person smoke and cannot get one finds out for free.
    if args.person_email and not APOLLO_PERSON.configured:
        print(
            "APOLLO_API_KEY is not set — see .env.example. The Apollo adapter and its whole "
            "fixture suite are complete and green; only this one real call is blocked. "
            "Set APOLLO_API_KEY in .env and re-run to make it.",
            file=sys.stderr,
        )
        return 1

    if not args.confirm_live_spend:
        print(
            "Refusing to run without --confirm-live-spend: this makes a real call against "
            "Abstract API's Company Enrichment endpoint, which is billed (or consumes free-tier "
            "quota — 100 requests/month with no card). Pass --confirm-live-spend to proceed.",
            file=sys.stderr,
        )
        return 1

    print(f"Calling the real adapter once for domain={args.domain!r}...")
    adapter_result = _call_adapter_once(args.domain)
    _print_json("Direct adapter call", adapter_result)

    if args.person_email:
        print()
        print(f"Calling the real Apollo adapter once for email={args.person_email!r}...")
        _print_json("Direct person adapter call", _call_person_adapter_once(args.person_email))

    if args.api_base_url:
        print(
            f"\nAlso driving this domain through the running API at {args.api_base_url} "
            f"(mode={'shadow' if args.shadow else 'normal'}) — this is a SECOND real call "
            "if the lead isn't already fully cached."
        )
        try:
            api_result = _run_through_api(
                base_url=args.api_base_url,
                domain=args.domain,
                shadow=args.shadow,
                timeout_s=args.timeout_s,
            )
        except (httpx.HTTPError, TimeoutError) as exc:
            print(f"Through-the-API run failed: {exc}", file=sys.stderr)
            return 1
        _print_json("Ingestion response", api_result["ingest"])
        _print_json("Decision Receipt", api_result["receipt"])

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
