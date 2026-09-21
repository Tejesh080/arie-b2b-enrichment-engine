"""Raw Hunter Combined Enrichment trace for jon@bannerbear.com (2026-09-22).

Calls the exact endpoint/config ARIE's adapter uses, then runs the SAME raw
response through ARIE's real, unmodified contract functions
(``arie.providers.hunter_contract``) step by step, to find exactly where
RAW HUNTER RESPONSE -> NORMALIZED RESULT -> ARIE SUCCESS/MISS diverges.

No code changed. No lead ingested. No score computed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx

from arie.config import HUNTER
from arie.providers.hunter_contract import (
    company_payload,
    extract_hunter_company,
    extract_hunter_person,
    normalize_hunter_person,
    normalized_identity,
    person_payload,
)

RUN_DIR = Path("data/evaluation/runs/realistic-pilot-2026-09-21")
EMAIL = "jon@bannerbear.com"


def main() -> int:
    api_key = os.environ["HUNTER_API_KEY"]

    print(f"Calling {HUNTER.base_url} (ARIE's configured Combined Enrichment endpoint) for {EMAIL} ...")
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            HUNTER.base_url,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            params={"email": EMAIL},
        )
    del api_key

    print("\n=== 1. Raw HTTP result ===")
    print(f"HTTP status: {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        print("Response was not valid JSON.")
        body = {}

    top_level_keys = list(body.keys()) if isinstance(body, dict) else []
    print(f"Top-level keys: {top_level_keys}")
    if "errors" in body:
        print(f"errors: {body['errors']}")
    if "meta" in body:
        print(f"meta: {body['meta']}")

    p_payload = person_payload(body) if isinstance(body, dict) else {}
    c_payload = company_payload(body) if isinstance(body, dict) else {}
    print(f"\nperson object present: {bool(p_payload)}")
    print(f"company object present: {bool(c_payload)}")

    if p_payload:
        employment = p_payload.get("employment")
        print(f"person.name: {p_payload.get('name')}")
        print(f"person.email: {p_payload.get('email')}")
        print(f"person.employment: {employment}")
        print(f"person top-level keys: {list(p_payload.keys())}")
    if c_payload:
        print(f"company.name: {c_payload.get('name')}")
        print(f"company.domain: {c_payload.get('domain')}")
        print(f"company.metrics: {c_payload.get('metrics')}")
        print(f"company.category: {c_payload.get('category')}")

    identity = normalized_identity(body) if isinstance(body, dict) else None
    print("\n=== 2. ARIE's normalized_identity() (display/audit only, never scored) ===")
    print(identity)

    print("\n=== 3. ARIE's extract_hunter_person() (raw Hunter vocabulary, pre-normalization) ===")
    extracted_person = extract_hunter_person(body) if isinstance(body, dict) else {}
    print(f"extracted fields: {extracted_person}")

    print("\n=== 4. ARIE's extract_hunter_company() (comparison only, never evidence) ===")
    extracted_company = extract_hunter_company(body) if isinstance(body, dict) else {}
    print(f"extracted fields: {extracted_company}")

    print("\n=== 5. ARIE's normalize_hunter_person() -> NormalizationReport ===")
    report = normalize_hunter_person(body) if isinstance(body, dict) else None
    if report is not None:
        print(f"report.fields (would become evidence): {report.fields}")
        print(f"report.has_usable_fields: {report.has_usable_fields}")
        print(f"report.audit(): {report.audit()}")

    print("\n=== 6. Where ARIE's adapter classification actually lands ===")
    if not p_payload and not c_payload:
        print("person_payload AND company_payload both empty -> adapter returns "
              "a FREE MISS before normalize_hunter_person is even called "
              "(live_hunter.py's own 'off-contract empty 200' branch).")
    elif report is not None and not report.has_usable_fields:
        print("person/company payload present, but normalize_hunter_person's "
              "has_usable_fields is False -> adapter returns a BILLED MISS "
              "(Hunter delivered a record, nothing mapped to the canonical vocabulary).")
    elif report is not None and report.has_usable_fields:
        print("has_usable_fields is True -> adapter WOULD return SUCCESS with "
              f"fields={report.fields}. If the live run showed a miss, the "
              "live payload differed from this one (see raw JSON saved).")

    out_path = RUN_DIR / "hunter_combined_raw_trace.json"
    out_path.write_text(
        json.dumps(
            {
                "http_status": resp.status_code,
                "top_level_keys": top_level_keys,
                "body": body,
                "person_payload_present": bool(p_payload),
                "company_payload_present": bool(c_payload),
                "extracted_person": extracted_person,
                "extracted_company": extracted_company,
                "normalized_report_fields": report.fields if report else None,
                "has_usable_fields": report.has_usable_fields if report else None,
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"\nfull raw body + trace written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
