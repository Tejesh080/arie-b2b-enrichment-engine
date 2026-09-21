"""Raw Hunter Combined Enrichment trace for 3 more named contacts (2026-09-22).

Same method as ``scripts/hunter_combined_raw_trace.py`` (Jon Yongfook), run
for the 3 remaining Hunter-Email-Finder-verified contacts. No code changed.
No lead ingested. No score computed.
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

CONTACTS = [
    {"name": "Steve Schabel", "email": "sschabel@alexandriaindustries.com"},
    {"name": "Jessica Mah", "email": "jmah@indinero.com"},
    {"name": "Josh Spencer", "email": "josh@lastbookstorela.com"},
]


def trace_one(email: str) -> dict:
    api_key = os.environ["HUNTER_API_KEY"]
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(
            HUNTER.base_url,
            headers={"X-API-KEY": api_key, "Accept": "application/json"},
            params={"email": email},
        )
    del api_key

    try:
        body = resp.json()
    except ValueError:
        body = {}

    p_payload = person_payload(body) if isinstance(body, dict) else {}
    c_payload = company_payload(body) if isinstance(body, dict) else {}
    identity = normalized_identity(body) if isinstance(body, dict) else None
    extracted_person = extract_hunter_person(body) if isinstance(body, dict) else {}
    extracted_company = extract_hunter_company(body) if isinstance(body, dict) else {}
    report = normalize_hunter_person(body) if isinstance(body, dict) else None

    employment = p_payload.get("employment") if p_payload else None

    return {
        "email": email,
        "http_status": resp.status_code,
        "person_match": bool(p_payload),
        "company_match": bool(c_payload),
        "returned_name": (identity.full_name if identity else None),
        "returned_employer": (identity.employer_name if identity else None),
        "returned_domain": (identity.employer_domain if identity else None),
        "employment_raw": employment,
        "company_employee_count": extracted_company.get("employee_count"),
        "extracted_person_fields": extracted_person,
        "normalized_fields": report.fields if report else None,
        "has_usable_fields": report.has_usable_fields if report else None,
        "raw_body": body,
    }


def main() -> int:
    results = []
    for contact in CONTACTS:
        print(f"\n=== {contact['name']} ({contact['email']}) ===")
        r = trace_one(contact["email"])
        r["contact"] = contact["name"]
        results.append(r)
        print(f"HTTP {r['http_status']} | person_match={r['person_match']} company_match={r['company_match']}")
        print(f"returned_name={r['returned_name']!r} employer={r['returned_employer']!r} domain={r['returned_domain']!r}")
        print(f"employment_raw={r['employment_raw']}")
        print(f"company_employee_count={r['company_employee_count']!r}")
        print(f"extracted_person_fields={r['extracted_person_fields']}")
        print(f"normalized_fields={r['normalized_fields']} has_usable_fields={r['has_usable_fields']}")

    out_path = RUN_DIR / "hunter_combined_raw_trace_3contacts.json"
    out_path.write_text(json.dumps(results, indent=2, default=str), encoding="utf-8")
    print(f"\nfull report written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
