"""`scripts.demo.render` — receipt dict -> presentation mapping.

No database, no HTTP — every test feeds a plain dict shaped like a real
`GET /leads/{id}/receipt` response and asserts on the rendered result.
"""

from __future__ import annotations

from typing import Any

from scripts.demo.render import render_decision, split_provider_activity


def _provider_call(
    provider: str, *, cache_hit: bool, status: str = "success", cost_usd: str = "0.05"
) -> dict[str, Any]:
    return {
        "provider": provider,
        "status": status,
        "cost_usd": cost_usd,
        "latency_ms": 100,
        "cache_hit": cache_hit,
    }


def _autonomous_receipt() -> dict[str, Any]:
    return {
        "receipt_version": "1",
        "lead_id": "11111111-1111-1111-1111-111111111111",
        "status": "decided",
        "lead_status": "AUTO_ROUTED",
        "created_at": "2026-08-18T00:00:00Z",
        "decision": {
            "recommended_action": "auto_route",
            "autonomous": True,
            "final_status": "AUTO_ROUTED",
            "human_override": False,
        },
        "score": {
            "value": 73.6,
            "threshold_qualify": 65.0,
            "threshold_reject": 55.0,
            "bounds": {"lower": 0.0, "upper": 73.6},
            "confidence": 0.832,
            "tau": 0.804,
        },
        "stopping": {
            "reason_code": "confidence_reached",
            "explanation": "The calibrated confidence model judged this decision reliable.",
        },
        "versions": {
            "policy": "calibrated_bounds",
            "scorer": "icp-1.0.0",
            "confidence_calibration": "platt",
        },
        "cost": {
            "provider_cost_usd": "0.407",
            "model_cost_usd": "0",
            "total_cost_usd": "0.407",
            "budget_usd_cap": "1.5",
        },
        "evidence": {
            "cache_hits": 1,
            "provider_calls": 2,
            "items": [
                {
                    "field": "industry",
                    "source": "firmographics_premium",
                    "confidence": 0.88,
                    "contested": False,
                },
            ],
            "unknown_fields": ["disqualifying_flag"],
        },
        "providers": {
            "called": [
                _provider_call("inbound_payload", cache_hit=False, cost_usd="0.0"),
                _provider_call("firmographics_premium", cache_hit=False, cost_usd="0.09"),
                _provider_call("dns_web", cache_hit=True, cost_usd="0.0"),
            ],
            "not_called": ["deep_research"],
        },
        "human_review": None,
    }


def _pending_receipt() -> dict[str, Any]:
    return {
        "receipt_version": "1",
        "lead_id": "22222222-2222-2222-2222-222222222222",
        "status": "pending",
        "lead_status": "SCORING",
        "created_at": None,
        "decision": None,
        "score": None,
        "stopping": None,
        "versions": None,
        "cost": {
            "provider_cost_usd": "0",
            "model_cost_usd": "0",
            "total_cost_usd": "0",
            "budget_usd_cap": "1.5",
        },
        "evidence": {"cache_hits": 0, "provider_calls": 0, "items": [], "unknown_fields": []},
        "providers": {"called": [], "not_called": []},
        "human_review": None,
    }


def _escalated_receipt(*, reviewed: bool, override: bool) -> dict[str, Any]:
    receipt: dict[str, Any] = {
        "receipt_version": "1",
        "lead_id": "33333333-3333-3333-3333-333333333333",
        "status": "decided",
        "lead_status": "AUTO_ROUTED" if reviewed else "AWAITING_HUMAN",
        "created_at": "2026-08-18T00:00:00Z",
        "decision": {
            "recommended_action": "reject",
            "autonomous": False,
            "final_status": "AUTO_ROUTED" if reviewed else "AWAITING_HUMAN",
            "human_override": override,
        },
        "score": {
            "value": 51.0,
            "threshold_qualify": 65.0,
            "threshold_reject": 55.0,
            "bounds": {"lower": 0.0, "upper": 81.0},
            "confidence": 0.248,
            "tau": 0.804,
        },
        "stopping": {
            "reason_code": "all_providers_called",
            "explanation": "Every available data provider was called.",
        },
        "versions": {
            "policy": "calibrated_bounds",
            "scorer": "icp-1.0.0",
            "confidence_calibration": "platt",
        },
        "cost": {
            "provider_cost_usd": "0.34",
            "model_cost_usd": "0",
            "total_cost_usd": "0.34",
            "budget_usd_cap": "1.5",
        },
        "evidence": {"cache_hits": 0, "provider_calls": 8, "items": [], "unknown_fields": []},
        "providers": {"called": [], "not_called": []},
        "human_review": {
            "review_id": "44444444-4444-4444-4444-444444444444",
            "required": True,
            "reviewer": "arie-demo" if reviewed else None,
            "original_decision": "reject",
            "action": "approve" if reviewed else None,
            "final_decision": "auto_route" if reviewed else None,
            "responded_at": "2026-08-18T00:01:00Z" if reviewed else None,
        },
    }
    return receipt


# ------------------------------------------------------- fresh vs cache --


def test_fresh_and_cache_activity_are_split_by_cache_hit_flag() -> None:
    activity = split_provider_activity(_autonomous_receipt())
    assert {c.provider for c in activity.fresh} == {"inbound_payload", "firmographics_premium"}
    assert {c.provider for c in activity.cache_reused} == {"dns_web"}
    assert activity.fresh_count == 2
    assert activity.cache_reused_count == 1


def test_a_cache_hit_is_never_present_in_fresh() -> None:
    activity = split_provider_activity(_autonomous_receipt())
    assert "dns_web" not in {c.provider for c in activity.fresh}


def test_not_called_is_reported_verbatim() -> None:
    activity = split_provider_activity(_autonomous_receipt())
    assert activity.not_called == ("deep_research",)


def test_no_provider_activity_renders_empty_not_missing() -> None:
    activity = split_provider_activity(_pending_receipt())
    assert activity.fresh == ()
    assert activity.cache_reused == ()
    assert activity.not_called == ()


# ------------------------------------------------------ autonomous decision --


def test_autonomous_decision_renders_every_documented_field() -> None:
    rendered = render_decision("Nadia Delacroix — Lumen500", _autonomous_receipt())

    assert rendered.recommended_action == "auto_route"
    assert rendered.autonomous is True
    assert rendered.final_status == "AUTO_ROUTED"
    assert rendered.human_override is False
    assert rendered.score_value == 73.6
    assert rendered.score_lower == 0.0
    assert rendered.score_upper == 73.6
    assert rendered.confidence == 0.832
    assert rendered.tau == 0.804
    assert rendered.stop_reason == "confidence_reached"
    assert rendered.total_cost_usd == "0.407"
    assert rendered.policy_version == "calibrated_bounds"
    assert rendered.human_review is None


def test_pending_receipt_renders_no_decision_fields() -> None:
    """A lead mid-pipeline must not be presented as if a decision exists."""
    rendered = render_decision("Someone — Somewhere", _pending_receipt())

    assert rendered.status == "pending"
    assert rendered.recommended_action is None
    assert rendered.score_value is None
    assert rendered.stop_reason is None
    assert rendered.human_review is None


# --------------------------------------------------- human review / override --


def test_pending_human_review_has_no_action_or_final_decision_yet() -> None:
    rendered = render_decision(
        "Nadia Haddad — Cobalt500", _escalated_receipt(reviewed=False, override=False)
    )

    assert rendered.recommended_action == "reject"
    assert rendered.autonomous is False
    assert rendered.human_override is False
    assert rendered.human_review is not None
    assert rendered.human_review.required is True
    assert rendered.human_review.action is None
    assert rendered.human_review.final_decision is None
    assert rendered.human_review.is_override is False


def test_human_override_preserves_the_original_recommendation() -> None:
    """The whole point of the receipt: recommendation and final outcome must
    both be visible, not one silently overwriting the other."""
    rendered = render_decision(
        "Nadia Haddad — Cobalt500", _escalated_receipt(reviewed=True, override=True)
    )

    assert rendered.recommended_action == "reject"  # frozen, never rewritten
    assert rendered.autonomous is False  # frozen
    assert rendered.final_status == "AUTO_ROUTED"  # live
    assert rendered.human_override is True
    assert rendered.human_review is not None
    assert rendered.human_review.original_decision == "reject"
    assert rendered.human_review.action == "approve"
    assert rendered.human_review.final_decision == "auto_route"
    assert rendered.human_review.is_override is True
    assert rendered.human_review.review_id == "44444444-4444-4444-4444-444444444444"


def test_a_reviewed_but_not_overridden_lead_reports_no_override() -> None:
    """Approving a reject would be an override; approving what was already
    going to be the same outcome should not be mislabeled as one."""
    receipt = _escalated_receipt(reviewed=True, override=False)
    receipt["human_review"]["final_decision"] = "reject"  # matches original_decision
    receipt["decision"]["human_override"] = False
    receipt["decision"]["final_status"] = "SYNCED"
    receipt["lead_status"] = "SYNCED"

    rendered = render_decision("Nadia Haddad — Cobalt500", receipt)

    assert rendered.human_override is False
    assert rendered.human_review is not None
    assert rendered.human_review.is_override is False
