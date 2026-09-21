"""Hunter named-contact diagnostic (2026-09-22) — provider coverage only.

Does NOT touch ARIE's scoring, ICP, database, or job pipeline. Two separate,
clearly-labeled checks:

1. **ARIE's existing Hunter adapter** (`HunterEnrichmentProvider.fetch`,
   Combined Enrichment by email) — the only lookup that adapter supports
   today, confirmed by reading its source before this script was written.
   Run only for the one contact with a genuinely, confidently publicly
   disclosed email (Jon Yongfook — see
   ``hunter_diagnostic_frozen_inputs.md``, frozen before this script ran).

2. **Hunter's own Email Finder endpoint**, called directly (name + domain,
   Hunter's own documented input for that endpoint) for all 5 contacts —
   independent of ARIE's adapter, to separate "does Hunter have coverage at
   all" from "does ARIE's adapter call the right endpoint."

Prints results only; writes one JSON artifact. No lead is ingested, no
worker cycle runs, no score is computed.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

from arie.core.types import Entity
from arie.providers.live_hunter import HunterEnrichmentProvider

RUN_DIR = Path("data/evaluation/runs/realistic-pilot-2026-09-21")

CONTACTS: list[dict[str, str]] = [
    {"name": "Steve Schabel", "first_name": "Steve", "last_name": "Schabel",
     "company": "Alexandria Industries", "domain": "alexandriaindustries.com", "known_email": ""},
    {"name": "Jessica Mah", "first_name": "Jessica", "last_name": "Mah",
     "company": "indinero", "domain": "indinero.com", "known_email": ""},
    {"name": "Jon Yongfook", "first_name": "Jon", "last_name": "Yongfook",
     "company": "Bannerbear", "domain": "bannerbear.com", "known_email": "jon@bannerbear.com"},
    {"name": "Josh Spencer", "first_name": "Josh", "last_name": "Spencer",
     "company": "The Last Bookstore", "domain": "lastbookstorela.com", "known_email": ""},
    {"name": "Charles Lamphere", "first_name": "Charles", "last_name": "Lamphere",
     "company": "Van Vlissingen and Co.", "domain": "vvco.com", "known_email": ""},
]

EMAIL_FINDER_URL = "https://api.hunter.io/v2/email-finder"


def run_arie_adapter_check(contact: dict[str, str]) -> dict[str, Any]:
    """Contact #3 only -- ARIE's real, unmodified Hunter adapter."""
    provider = HunterEnrichmentProvider.build()
    entity = Entity(entity_type="person", entity_id=uuid.uuid4(), canonical_key=contact["known_email"])
    result = provider.fetch(entity)
    provider.close()
    return {
        "contact": contact["name"],
        "email_used": contact["known_email"],
        "status": str(result.status),
        "fields": result.fields,
        "confidence": result.confidence,
        "cost_usd": result.cost_usd,
        "latency_ms": round(result.latency_ms, 1),
        "raw": result.raw,
    }


def run_email_finder(contact: dict[str, str]) -> dict[str, Any]:
    """Direct call to Hunter's own Email Finder endpoint -- name + domain,
    never a guessed email. Independent of ARIE's adapter code entirely."""
    api_key = os.environ["HUNTER_API_KEY"]
    started = time.monotonic()
    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(
                EMAIL_FINDER_URL,
                headers={"X-API-KEY": api_key},
                params={
                    "domain": contact["domain"],
                    "first_name": contact["first_name"],
                    "last_name": contact["last_name"],
                },
            )
    except httpx.HTTPError as exc:
        return {"contact": contact["name"], "domain": contact["domain"], "error": type(exc).__name__}
    latency_ms = (time.monotonic() - started) * 1000

    out: dict[str, Any] = {
        "contact": contact["name"],
        "domain": contact["domain"],
        "http_status": resp.status_code,
        "latency_ms": round(latency_ms, 1),
    }
    try:
        body = resp.json()
    except ValueError:
        out["parse_error"] = True
        return out

    if resp.status_code == 200:
        data = body.get("data", {})
        out["found_email"] = data.get("email")
        out["score"] = data.get("score")
        out["verification_status"] = (data.get("verification") or {}).get("status")
        out["found_first_name"] = data.get("first_name")
        out["found_last_name"] = data.get("last_name")
        out["found_position"] = data.get("position")
        out["found_company"] = data.get("company")
    else:
        out["errors"] = body.get("errors")
    return out


def main() -> int:
    print("=== Part 1: ARIE's existing Hunter adapter (Combined Enrichment by email) ===")
    adapter_results = []
    for contact in CONTACTS:
        if not contact["known_email"]:
            print(f"{contact['name']}: SKIPPED -- no confidently-known public email (see frozen input sheet)")
            adapter_results.append({
                "contact": contact["name"], "email_used": None,
                "status": "NOT_ATTEMPTED", "reason": "no confidently-known public email",
            })
            continue
        result = run_arie_adapter_check(contact)
        print(f"{contact['name']}: status={result['status']} confidence={result['confidence']} "
              f"cost=${result['cost_usd']} fields={result['fields']}")
        adapter_results.append(result)

    print("\n=== Part 2: Hunter Email Finder, direct (name + domain, all 5) ===")
    finder_results = []
    for contact in CONTACTS:
        result = run_email_finder(contact)
        print(f"{contact['name']} @ {contact['domain']}: http={result.get('http_status')} "
              f"found_email={result.get('found_email')} score={result.get('score')} "
              f"verification={result.get('verification_status')} errors={result.get('errors')}")
        finder_results.append(result)

    out_path = RUN_DIR / "hunter_named_contact_diagnostic.json"
    out_path.write_text(
        json.dumps({"arie_adapter": adapter_results, "email_finder": finder_results}, indent=2, default=str),
        encoding="utf-8",
    )
    print(f"\nfull report written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
