"""Offline replay of the validation-20 15-lead artifact with the full_name fix.

**Makes no provider calls.** Everything here re-derives from the raw values
already captured in
``data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.json`` (the
real 2026-08-29/30 Abstract+Hunter run) plus the ``full_name`` every identity
already carries in ``identities.json`` — the one input
``scripts/live_experiment_abstract_hunter.py`` never threaded into
``LeadIngestCommand`` during that run (see
``tests/unit/test_validation_20_full_name_wiring.py``). Neither
``arie.identity.validation`` nor ``arie.scoring.engine`` is modified; this
script only supplies the missing input to the same, unmodified functions the
real pipeline calls.

For every lead where the corrected verdict is ``MISMATCH``, this also
recomputes the score: builds the same ``Evidence`` list the real
``compute_score`` job would have scored, minus the person-provider fields a
``MISMATCH`` clears (mirrors ``arie.jobs.handlers._validate_person_match``'s
"keep the record, reject the score" rule exactly), and re-scores it with
``arie.scoring.engine.score_evidence`` plus the same corpus-calibrated
``ConfidenceModel`` the real worker used (``arie.jobs.handlers.build_runtime``,
seed 42 — deterministic, so this reconstructs bit-for-bit the model that
scored the original run without needing the live database).

Writes a derived JSON report next to the source artifact; never modifies
either source file.

    python scripts/offline_replay_validation_20.py
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arie.confidence.model import ConfidenceModel
from arie.core.types import Evidence
from arie.evidence.ttl_policy import ttl_for_field
from arie.identity.validation import (
    MISMATCH,
    RequestedIdentity,
    ReturnedIdentity,
    validate_identity,
)
from arie.jobs.handlers import build_runtime
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.scoring.engine import score_evidence

ARTIFACT = Path("data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.json")
IDENTITIES_FILE = Path("data/evaluation/runs/validation-20-2026-08-30/identities.json")
OUT_PATH = Path(
    "data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.offline-replay.json"
)

# The two providers' fixed declared confidence for a SUCCESS result — see
# arie.providers.live_abstract._DECLARED_CONFIDENCE (0.80) and
# arie.providers.live_hunter._DECLARED_CONFIDENCE (0.75). Reproduced here
# rather than imported since both are module-private constants in adapter
# modules this script otherwise has no reason to import.
ABSTRACT_CONFIDENCE = 0.80


def _full_names_by_email(identities_path: Path) -> dict[str, str]:
    payload = json.loads(identities_path.read_text(encoding="utf-8"))
    return {
        row["email"]: row["full_name"]
        for row in payload["identities"]
        if row.get("ground_truth_quality") in ("HIGH", "MEDIUM") and row.get("full_name")
    }


def _evidence_from_known_fields(
    *, fields: dict[str, Any], source: str, confidence: float, entity_type: str, now: datetime
) -> list[Evidence]:
    return [
        Evidence(
            entity_type=entity_type,  # type: ignore[arg-type]
            entity_id=uuid.uuid4(),
            field_name=field_name,
            value=value,
            source=source,
            confidence=confidence,
            ttl_seconds=ttl_for_field(field_name),
            fetched_at=now,
        )
        for field_name, value in fields.items()
    ]


def _replay_one(
    lead: dict[str, Any], full_names: dict[str, str], now: datetime, model: ConfidenceModel
) -> dict[str, Any]:
    identity = lead["identity"]
    email = identity["email"]
    domain = identity["domain"]
    receipt = lead["receipt"]
    hunter_call = lead.get("hunter_raw_call") or {}
    hunter_raw = hunter_call.get("raw") or {}
    hunter_status = hunter_call.get("status")
    hunter_fields: dict[str, Any] = hunter_call.get("fields") or {}
    abstract_call = lead.get("abstract_raw_call") or {}
    abstract_fields: dict[str, Any] = abstract_call.get("fields") or {}

    matched = hunter_raw.get("matched_identity")
    corrected_verdict: str | None = None
    corrected_reasons: tuple[str, ...] = ()
    if hunter_status == "success" and isinstance(matched, dict):
        validation = validate_identity(
            RequestedIdentity(email=email, company_domain=domain, full_name=full_names.get(email)),
            ReturnedIdentity(
                full_name=matched.get("full_name"),
                email=matched.get("email"),
                employer_domain=matched.get("employer_domain"),
                employer_name=matched.get("employer_name"),
            ),
        )
        corrected_verdict = validation.verdict
        corrected_reasons = validation.reasons

    original_verdict = (
        ((lead.get("evidence_snapshot") or {}).get("identity_findings") or [{}])[0].get("verdict")
        if hunter_status == "success"
        else None
    )

    lost_person_evidence = corrected_verdict == MISMATCH and bool(hunter_fields)

    result: dict[str, Any] = {
        "validation_id": identity.get("validation_id"),
        "email": email,
        "requested_full_name": full_names.get(email),
        "hunter_matched_name": matched.get("full_name") if isinstance(matched, dict) else None,
        "hunter_matched_title": matched.get("title") if isinstance(matched, dict) else None,
        "original_verdict_no_full_name": original_verdict,
        "corrected_verdict": corrected_verdict,
        "corrected_reasons": list(corrected_reasons),
        "lost_person_evidence": lost_person_evidence,
        "original_score": receipt["score"]["value"] if receipt.get("score") else None,
        "original_bounds": receipt["score"]["bounds"] if receipt.get("score") else None,
        "original_confidence": receipt["score"]["confidence"] if receipt.get("score") else None,
    }

    if not lost_person_evidence:
        return result

    # Rebuild exactly the evidence set the real compute_score job scored:
    # Abstract's company fields (unaffected by identity validation) plus
    # Hunter's person fields — then drop the person fields, mirroring
    # arie.jobs.handlers._validate_person_match's MISMATCH branch
    # (`replace(result, fields={})`) applied to what was actually persisted.
    evidence = _evidence_from_known_fields(
        fields=abstract_fields,
        source=ABSTRACT_PROVIDER_NAME,
        confidence=ABSTRACT_CONFIDENCE,
        entity_type="company",
        now=now,
    )
    corrected_scoring = score_evidence(evidence, now)
    corrected_confidence = model.predict(corrected_scoring)

    result["corrected_score"] = corrected_scoring.total_score
    result["corrected_bounds"] = {
        "lower": corrected_scoring.bounds.lower,
        "upper": corrected_scoring.bounds.upper,
    }
    result["corrected_confidence"] = corrected_confidence
    result["dropped_fields"] = {k: hunter_fields[k] for k in hunter_fields}
    return result


def main() -> int:
    if not ARTIFACT.exists() or not IDENTITIES_FILE.exists():
        print(f"source artifact or identities file not found: {ARTIFACT}, {IDENTITIES_FILE}")
        return 1

    full_names = _full_names_by_email(IDENTITIES_FILE)
    source = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    now = datetime.now(UTC)

    print("building the corpus-calibrated confidence model (seed=42, deterministic)...")
    model = build_runtime().policy.model

    leads = [_replay_one(lead, full_names, now, model) for lead in source["leads"]]

    verified = sum(1 for lead in leads if lead["corrected_verdict"] == "VERIFIED")
    probable = sum(1 for lead in leads if lead["corrected_verdict"] == "PROBABLE")
    mismatch = sum(1 for lead in leads if lead["corrected_verdict"] == "MISMATCH")
    no_comparison = sum(1 for lead in leads if lead["corrected_verdict"] is None)
    lost_evidence = [lead for lead in leads if lead["lost_person_evidence"]]

    # The real, already-billed total Hunter spend across all 15 leads (14
    # billed calls, 1 free clean miss — see the receipts' own cost.total_cost_usd
    # minus each lead's Abstract share). Billing already happened at call
    # time and does not change with this fix; only which results are usable
    # changes.
    hunter_total_cost_usd = round(
        sum(
            float(call["cost_usd"])
            for lead in source["leads"]
            for call in lead["receipt"]["providers"]["called"]
            if call["provider"] == "hunter_combined_enrichment"
        ),
        5,
    )
    corrected_usable = verified + probable  # both keep their fields; only MISMATCH clears them
    corrected_cost_per_usable = (
        round(hunter_total_cost_usd / corrected_usable, 5) if corrected_usable else None
    )

    summary = {
        "run_id": source.get("run_id"),
        "leads": leads,
        "metrics": {
            "n": len(leads),
            "corrected_verified": verified,
            "corrected_probable": probable,
            "corrected_mismatch": mismatch,
            "corrected_no_comparison_possible": no_comparison,
            "leads_losing_person_evidence": [lead["validation_id"] for lead in lost_evidence],
            "hunter_total_modeled_cost_usd": hunter_total_cost_usd,
            "hunter_cost_per_verified_or_probable_usable_person_usd": corrected_cost_per_usable,
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"\n{'id':<6} {'email':<32} {'orig_verdict':<10} {'corrected':<10} {'lost_evid':<10}")
    for lead in leads:
        print(
            f"{lead['validation_id'] or '-':<6} {lead['email']:<32} "
            f"{lead['original_verdict_no_full_name'] or '-':<10} "
            f"{lead['corrected_verdict'] or '-':<10} "
            f"{'YES' if lead['lost_person_evidence'] else 'no':<10}"
        )
        if lead["lost_person_evidence"]:
            print(
                f"       score: {lead['original_score']} -> {lead['corrected_score']}  "
                f"bounds: {lead['original_bounds']} -> {lead['corrected_bounds']}  "
                f"confidence: {lead['original_confidence']:.3f} -> {lead['corrected_confidence']:.3f}"
            )

    print()
    for key, value in summary["metrics"].items():
        print(f"{key}: {value}")
    print(f"\nartifact written: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
