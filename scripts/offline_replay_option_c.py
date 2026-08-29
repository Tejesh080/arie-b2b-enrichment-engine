"""Offline estimate of Option C (Abstract first, Hunter only when material)
against the validation-20 15-lead artifact.

**Makes no provider calls.** For each lead, rebuilds the Abstract-only
``ScoringResult`` from the real ``abstract_raw_call.fields`` already captured
in ``data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.json``,
then asks the real, unmodified
:func:`arie.live.person_relevance.person_evidence_is_decision_relevant`
whether Hunter's fields (``arie.providers.hunter_contract.HUNTER_PROVIDES_FIELDS``)
could have changed the recommendation. This is the exact function
``arie.jobs.handlers._option_c_stop_check`` calls in the real acquisition
loop — nothing here re-derives or approximates that decision.

The "would any recommendation differ" and "was a useful result skipped"
questions are answered against the **corrected** identity verdicts from
``scripts/offline_replay_validation_20.py`` (the full_name-fix replay), not
the original miscalibrated run — comparing Option C against a known-buggy
baseline would misattribute that bug's effect to a provider-order change.

Writes a derived JSON report next to the source artifact; never modifies
either source file.

    python scripts/offline_replay_option_c.py
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from arie.core.types import Evidence
from arie.evidence.ttl_policy import ttl_for_field
from arie.identity.validation import RequestedIdentity, ReturnedIdentity, validate_identity
from arie.live.person_relevance import person_evidence_is_decision_relevant
from arie.providers.hunter_contract import HUNTER_PROVIDES_FIELDS
from arie.providers.live_abstract import PROVIDER_NAME as ABSTRACT_PROVIDER_NAME
from arie.scoring.engine import ScoringResult, score_evidence

ARTIFACT = Path("data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.json")
IDENTITIES_FILE = Path("data/evaluation/runs/validation-20-2026-08-30/identities.json")
OUT_PATH = Path(
    "data/evaluation/runs/validation-20-2026-08-30/live-ah-b12155dfc3.option-c-replay.json"
)


def _full_names_by_email(identities_path: Path) -> dict[str, str]:
    payload = json.loads(identities_path.read_text(encoding="utf-8"))
    return {
        row["email"]: row["full_name"]
        for row in payload["identities"]
        if row.get("ground_truth_quality") in ("HIGH", "MEDIUM") and row.get("full_name")
    }


def _abstract_only_scoring(abstract_fields: dict[str, Any], now: datetime) -> ScoringResult:
    company_id = uuid.uuid4()
    evidence = [
        Evidence(
            entity_type="company",
            entity_id=company_id,
            field_name=field_name,
            value=value,
            source=ABSTRACT_PROVIDER_NAME,
            confidence=0.80,
            ttl_seconds=ttl_for_field(field_name),
            fetched_at=now,
        )
        for field_name, value in abstract_fields.items()
    ]
    return score_evidence(evidence, now)


def _corrected_person_verdict(lead: dict[str, Any], full_names: dict[str, str]) -> str | None:
    """The corrected verdict from the full_name-fix replay, recomputed here
    directly against the artifact (not read from a second file) so this
    script only depends on one input artifact plus identities.json."""
    hunter_call = lead.get("hunter_raw_call") or {}
    if hunter_call.get("status") != "success":
        return None
    matched = (hunter_call.get("raw") or {}).get("matched_identity")
    if not isinstance(matched, dict):
        return None
    identity = lead["identity"]
    validation = validate_identity(
        RequestedIdentity(
            email=identity["email"],
            company_domain=identity["domain"],
            full_name=full_names.get(identity["email"]),
        ),
        ReturnedIdentity(
            full_name=matched.get("full_name"),
            email=matched.get("email"),
            employer_domain=matched.get("employer_domain"),
            employer_name=matched.get("employer_name"),
        ),
    )
    return str(validation.verdict)


def _corrected_full_decision(
    lead: dict[str, Any], corrected_verdict: str | None, now: datetime
) -> str:
    """The recommendation from the real, corrected full Abstract+Hunter run:
    Abstract's fields always count; Hunter's fields count only when the
    corrected identity verdict is not MISMATCH — the same "keep the record,
    reject the score" rule ``arie.jobs.handlers._validate_person_match``
    applies live. This is what Option C is compared against, never the
    original miscalibrated run."""
    abstract_fields = (lead.get("abstract_raw_call") or {}).get("fields") or {}
    company_id = uuid.uuid4()
    evidence = [
        Evidence(
            entity_type="company",
            entity_id=company_id,
            field_name=field_name,
            value=value,
            source=ABSTRACT_PROVIDER_NAME,
            confidence=0.80,
            ttl_seconds=ttl_for_field(field_name),
            fetched_at=now,
        )
        for field_name, value in abstract_fields.items()
    ]
    if corrected_verdict != "MISMATCH":
        hunter_fields = (lead.get("hunter_raw_call") or {}).get("fields") or {}
        person_id = uuid.uuid4()
        evidence.extend(
            Evidence(
                entity_type="person",
                entity_id=person_id,
                field_name=field_name,
                value=value,
                source="hunter_combined_enrichment",
                confidence=0.75,
                ttl_seconds=ttl_for_field(field_name),
                fetched_at=now,
            )
            for field_name, value in hunter_fields.items()
        )
    return str(score_evidence(evidence, now).decision)


def main() -> int:
    if not ARTIFACT.exists() or not IDENTITIES_FILE.exists():
        print(f"source artifact or identities file not found: {ARTIFACT}, {IDENTITIES_FILE}")
        return 1

    full_names = _full_names_by_email(IDENTITIES_FILE)
    source = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    now = datetime.now(UTC)

    leads: list[dict[str, Any]] = []
    for lead in source["leads"]:
        identity = lead["identity"]
        abstract_fields = (lead.get("abstract_raw_call") or {}).get("fields") or {}
        abstract_scoring = _abstract_only_scoring(abstract_fields, now)

        relevance = person_evidence_is_decision_relevant(abstract_scoring, HUNTER_PROVIDES_FIELDS)
        corrected_verdict = _corrected_person_verdict(lead, full_names)
        corrected_full_decision = _corrected_full_decision(lead, corrected_verdict, now)
        called = lead["receipt"]["providers"]["called"]
        real_hunter_cost = float(
            next(
                (c["cost_usd"] for c in called if c["provider"] == "hunter_combined_enrichment"),
                0.0,
            )
        )
        # Option C always calls Abstract, in the same order, for the same
        # domains, as the real run did — so its Abstract spend (including
        # cache-suppressed repeats and the one rate-limited, unbilled error)
        # is identical to what was actually observed, never a flat per-lead
        # rate. Only the Hunter side is variable between the two scenarios.
        real_abstract_cost = float(
            next(
                (c["cost_usd"] for c in called if c["provider"] == "abstract_company_enrichment"),
                0.0,
            )
        )
        # Option C's own recommendation: if it skips Hunter, the decision is
        # whatever Abstract-only evidence already gives; if it calls Hunter,
        # it gets exactly the same real data and applies the same identity
        # validation as the full run, so the decision is identical to it.
        option_c_decision = (
            corrected_full_decision if relevance.should_call else str(abstract_scoring.decision)
        )

        leads.append(
            {
                "validation_id": identity.get("validation_id"),
                "email": identity["email"],
                "abstract_only_score": abstract_scoring.total_score,
                "abstract_only_decision": str(abstract_scoring.decision),
                "abstract_only_bounds": {
                    "lower": abstract_scoring.bounds.lower,
                    "upper": abstract_scoring.bounds.upper,
                },
                "option_c_would_call_hunter": relevance.should_call,
                "option_c_reason": relevance.reason,
                "option_c_best_case_score": relevance.best_case_score,
                "option_c_decision": option_c_decision,
                "real_full_run_corrected_decision": corrected_full_decision,
                "real_full_run_corrected_person_verdict": corrected_verdict,
                "real_hunter_cost_usd": real_hunter_cost,
                "real_abstract_cost_usd": real_abstract_cost,
            }
        )

    calls_made = sum(1 for lead in leads if lead["option_c_would_call_hunter"])
    calls_avoided = len(leads) - calls_made
    real_abstract_total = sum(lead["real_abstract_cost_usd"] for lead in leads)
    spend_saved = round(
        sum(
            lead["real_hunter_cost_usd"] for lead in leads if not lead["option_c_would_call_hunter"]
        ),
        5,
    )
    # Option C's Abstract spend is identical to the real run's (same calls,
    # same order, same cache suppression and the one rate-limited error) —
    # only the Hunter side differs, using each lead's real observed Hunter
    # cost rather than a flat rate (a genuine miss bills $0 under Hunter's
    # own bill-on-match rule; see arie.providers.live_hunter).
    option_c_spend = round(
        real_abstract_total
        + sum(lead["real_hunter_cost_usd"] for lead in leads if lead["option_c_would_call_hunter"]),
        5,
    )

    skipped_but_useful = [
        lead
        for lead in leads
        if not lead["option_c_would_call_hunter"]
        and lead["real_full_run_corrected_person_verdict"] in ("VERIFIED", "PROBABLE")
    ]
    # Would any *recommendation* differ from the corrected full run? By
    # construction, a skip only fires when Hunter's best-case fields
    # couldn't move the decision, and the real result is never better than
    # best case — so this should be empty. Checked explicitly, over every
    # lead (not just the skipped ones), rather than assumed.
    recommendation_would_differ = [
        lead
        for lead in leads
        if lead["option_c_decision"] != lead["real_full_run_corrected_decision"]
    ]

    summary = {
        "run_id": source.get("run_id"),
        "leads": leads,
        "metrics": {
            "n": len(leads),
            "hunter_calls_option_c_would_make": calls_made,
            "hunter_calls_option_c_would_avoid": calls_avoided,
            "modeled_spend_saved_usd": spend_saved,
            "option_c_total_modeled_spend_usd": option_c_spend,
            "real_full_run_total_modeled_spend_usd": round(
                real_abstract_total + sum(lead["real_hunter_cost_usd"] for lead in leads),
                5,
            ),
            "leads_skipped_with_a_verified_or_probable_hunter_result": [
                lead["validation_id"] for lead in skipped_but_useful
            ],
            "leads_where_option_c_recommendation_differs_from_the_full_run": [
                lead["validation_id"] for lead in recommendation_would_differ
            ],
        },
    }

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    print(f"{'id':<6} {'email':<32} {'call?':<7} {'verdict':<10} reason")
    for lead in leads:
        print(
            f"{lead['validation_id'] or '-':<6} {lead['email']:<32} "
            f"{'YES' if lead['option_c_would_call_hunter'] else 'no':<7} "
            f"{lead['real_full_run_corrected_person_verdict'] or '-':<10} "
            f"{lead['option_c_reason']}"
        )
    print()
    for key, value in summary["metrics"].items():
        print(f"{key}: {value}")
    if skipped_but_useful:
        print(
            "\nNOTE: leads skipped by Option C whose real Hunter result was VERIFIED/PROBABLE "
            f"(useful but not decision-relevant): {[lead['validation_id'] for lead in skipped_but_useful]}"
        )
    print(f"\nartifact written: {OUT_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
