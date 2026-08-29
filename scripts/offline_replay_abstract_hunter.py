"""Offline replay of the abstract-hunter-live-1 five-lead artifact.

**Makes no provider calls.** Everything here re-derives from the raw values
already captured in ``data/evaluation/runs/abstract-hunter-live-1/live-ah-2.json``
(the immutable record of the 2026-08-29 experiment) through the corrected
logic added on 2026-08-30:

* :func:`arie.identity.validation.validate_identity` — was the person Hunter
  matched actually the person the lead was about, not just a real person at
  the right company?
* the K/M-suffixed employee-count parser fix
  (:func:`arie.normalization.taxonomy.normalize_employee_count`) — re-parses
  Hunter's raw company-preview strings ("10K-50K") that the old parser left
  UNKNOWN.
* :func:`arie.live.evaluation.classify_numeric_agreement` — the new
  tolerance-based company-size comparison.

**Ground truth for identity validation is intentionally minimal.** The
original five-lead spec gave an expected employer/title for all five but an
expected *name* for none; this script does not invent one. The one exception
is ``patrick@stripe.com``, where the operator explicitly stated "expected
person: Patrick Collison" in the stabilization request that asked for this
replay — that fact is recorded here, sourced to that message, not inferred
from outside knowledge about who is well-known. The other four run with no
expected name, exactly what a production lead with no CRM-supplied name would
have — which is why their verdicts cap at PROBABLE rather than VERIFIED (see
``arie.identity.validation``'s module docstring for why a single corroborating
signal can never rule out a same-domain wrong-person match on its own).

Writes a derived report next to the source artifact; never modifies it.

    python scripts/offline_replay_abstract_hunter.py
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arie.identity.validation import RequestedIdentity, ReturnedIdentity, validate_identity
from arie.live.evaluation import classify_numeric_agreement
from arie.normalization.taxonomy import normalize_employee_count

SOURCE_ARTIFACT = Path("data/evaluation/runs/abstract-hunter-live-1/live-ah-2.json")
OUT_PATH = Path("data/evaluation/runs/abstract-hunter-live-1/live-ah-2.offline-replay.json")

# Explicitly and only what the operator stated, sourced to where — never
# outside general knowledge injected by this script. See the module docstring.
EXPECTED_FULL_NAMES: dict[str, str] = {
    "patrick@stripe.com": "Patrick Collison",  # 2026-08-30 stabilization request, section 2
}


def _company_preview_raw(hunter_raw: dict[str, Any], field: str) -> str | None:
    preview = hunter_raw.get("company_preview") or {}
    for entry in (*preview.get("mapped", []), *preview.get("unmapped", [])):
        if entry.get("field") == field:
            raw = entry.get("raw")
            return str(raw) if raw is not None else None
    return None


def _company_preview_industry_canonical(hunter_raw: dict[str, Any]) -> str | None:
    preview = hunter_raw.get("company_preview") or {}
    for entry in preview.get("mapped", []):
        if entry.get("field") == "industry":
            value = entry.get("canonical")
            return str(value) if value is not None else None
    return None


def _replay_one(lead: dict[str, Any]) -> dict[str, Any]:
    identity = lead["identity"]
    email = identity["email"]
    domain = identity["domain"]
    abstract_raw = lead.get("abstract_raw_call") or {}
    hunter_raw = (lead.get("hunter_raw_call") or {}).get("raw") or {}
    hunter_status = (lead.get("hunter_raw_call") or {}).get("status")

    matched = hunter_raw.get("matched_identity")
    identity_result: dict[str, Any] | None = None
    person_evidence_usable = False
    if hunter_status == "success" and isinstance(matched, dict):
        validation = validate_identity(
            RequestedIdentity(
                email=email, company_domain=domain, full_name=EXPECTED_FULL_NAMES.get(email)
            ),
            ReturnedIdentity(
                full_name=matched.get("full_name"),
                email=matched.get("email"),
                employer_domain=matched.get("employer_domain"),
                employer_name=matched.get("employer_name"),
            ),
        )
        identity_result = {"verdict": validation.verdict, "reasons": list(validation.reasons)}
        person_evidence_usable = validation.verdict != "MISMATCH"

    hunter_employee_raw = _company_preview_raw(hunter_raw, "employee_count")
    _parsed = (
        normalize_employee_count(hunter_employee_raw) if hunter_employee_raw is not None else None
    )
    hunter_employee_corrected: int | None = _parsed if isinstance(_parsed, int) else None

    abstract_employee = (abstract_raw.get("fields") or {}).get("employee_count")
    abstract_industry = (abstract_raw.get("fields") or {}).get("industry")
    hunter_industry_canonical = _company_preview_industry_canonical(hunter_raw)

    employee_count_agreement = classify_numeric_agreement(
        {"abstract": abstract_employee, "hunter": hunter_employee_corrected}
    )
    industry_agreement = (
        "agree"
        if abstract_industry is not None
        and hunter_industry_canonical is not None
        and abstract_industry == hunter_industry_canonical
        else (
            "conflict"
            if abstract_industry is not None and hunter_industry_canonical is not None
            else "unknown"
        )
    )

    return {
        "email": email,
        "domain": domain,
        "expected_title": identity.get("expected_title"),
        "hunter_status": hunter_status,
        "hunter_matched_name": matched.get("full_name") if isinstance(matched, dict) else None,
        "hunter_matched_title": matched.get("title") if isinstance(matched, dict) else None,
        "identity_validation": identity_result,
        "person_evidence_usable_for_scoring": person_evidence_usable,
        "company_comparison": {
            "industry": {
                "abstract": abstract_industry,
                "hunter": hunter_industry_canonical,
                "agreement": industry_agreement,
            },
            "employee_count": {
                "abstract": abstract_employee,
                "hunter_raw": hunter_employee_raw,
                "hunter_corrected": hunter_employee_corrected,
                "would_have_been_unknown_pre_fix": bool(
                    hunter_employee_raw
                    and hunter_employee_corrected is not None
                    and any(unit in hunter_employee_raw.lower() for unit in ("k", "m"))
                ),
                "agreement": employee_count_agreement,
            },
        },
    }


def main() -> int:
    if not SOURCE_ARTIFACT.exists():
        print(f"source artifact not found: {SOURCE_ARTIFACT}")
        return 1

    source = json.loads(SOURCE_ARTIFACT.read_text(encoding="utf-8"))
    leads = [_replay_one(lead) for lead in source["leads"]]

    verified = sum(
        1 for lead in leads if (lead["identity_validation"] or {}).get("verdict") == "VERIFIED"
    )
    probable = sum(
        1 for lead in leads if (lead["identity_validation"] or {}).get("verdict") == "PROBABLE"
    )
    mismatch = sum(
        1 for lead in leads if (lead["identity_validation"] or {}).get("verdict") == "MISMATCH"
    )
    unverifiable_or_none = sum(
        1
        for lead in leads
        if lead["identity_validation"] is None
        or lead["identity_validation"]["verdict"] == "UNVERIFIABLE"
    )
    hunter_matches = sum(1 for lead in leads if lead["hunter_status"] == "success")
    usable_person_evidence = sum(1 for lead in leads if lead["person_evidence_usable_for_scoring"])

    summary = {
        "run_id": source.get("run_id"),
        "leads": leads,
        "metrics": {
            "n": len(leads),
            "hunter_provider_match_rate": round(hunter_matches / len(leads), 4),
            "hunter_verified_person_rate": round(verified / len(leads), 4),
            "hunter_probable_person_rate": round(probable / len(leads), 4),
            "hunter_identity_mismatch_rate": round(mismatch / len(leads), 4),
            "hunter_unverifiable_or_uncalled_rate": round(unverifiable_or_none / len(leads), 4),
            "hunter_usable_person_evidence_rate": round(usable_person_evidence / len(leads), 4),
            "note": (
                "verified/probable/mismatch/unverifiable are computed only over the 5 "
                "leads Hunter matched; hunter_provider_match_rate and the two rate "
                "denominators below are over all 5 leads."
            ),
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(
        f"{'email':<22} {'hunter':<9} {'identity':<12} {'usable':<7} {'industry':<10} {'employee_count'}"
    )
    for lead in leads:
        verdict = (lead["identity_validation"] or {}).get("verdict", "n/a")
        emp = lead["company_comparison"]["employee_count"]
        ind = lead["company_comparison"]["industry"]
        print(
            f"{lead['email']:<22} {lead['hunter_status'] or '-':<9} {verdict:<12} "
            f"{'yes' if lead['person_evidence_usable_for_scoring'] else 'no':<7} "
            f"{ind['agreement']:<10} "
            f"{emp['abstract']}/{emp['hunter_corrected']} -> {emp['agreement']}"
        )
    print()
    for key, value in summary["metrics"].items():
        print(f"{key}: {value}")
    print(f"\nartifact written: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
