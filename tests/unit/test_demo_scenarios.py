"""`scripts.demo.scenarios` — orchestration logic, against a fake client.

`_FakeClient` implements exactly the methods `ArieClient` exposes that the
scenarios call, backed by an in-memory dict — enough to exercise the real
wiring (which id comes from where, what gets POSTed, that an override is
detected from the receipt alone) without a running stack. HTTP-transport-level
behavior (timeouts, malformed responses) is `test_demo_client.py`'s job, not
this file's.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from scripts.demo.corpus import select_demo_corpus
from scripts.demo.scenarios import (
    IdempotencyResult,
    run_scenario_a,
    run_scenario_b,
    run_scenario_d,
)

from arie.evalgen.schema import EvalLead

# ------------------------------------------------------------- IdempotencyResult --


def test_idempotency_result_true_only_when_the_second_call_created_nothing() -> None:
    result = IdempotencyResult(
        source="arie-demo",
        external_ref="demo-x-autonomous",
        first={"lead_id": "l1", "created": True, "job_created": True},
        second={"lead_id": "l1", "created": False, "job_created": False},
    )
    assert result.is_idempotent is True


def test_idempotency_result_false_if_a_second_lead_was_created() -> None:
    result = IdempotencyResult(
        source="arie-demo",
        external_ref="demo-x-autonomous",
        first={"lead_id": "l1", "created": True, "job_created": True},
        second={"lead_id": "l2", "created": True, "job_created": True},
    )
    assert result.is_idempotent is False


def test_idempotency_result_false_if_only_a_second_job_was_created() -> None:
    """A redelivered webhook after a dead-lettered job legitimately gets
    created=False but job_created=True (0007's requeue path) — still not the
    "nothing happened twice" story this scenario demonstrates."""
    result = IdempotencyResult(
        source="arie-demo",
        external_ref="demo-x-autonomous",
        first={"lead_id": "l1", "created": True, "job_created": True},
        second={"lead_id": "l1", "created": False, "job_created": True},
    )
    assert result.is_idempotent is False


# ------------------------------------------------------------------- fake client --


@dataclass
class _FakeClient:
    """A hand-rolled `ArieClient` stand-in. `post_lead` uses `external_ref` as
    the lead id directly, so tests can pre-populate `receipts` before calling
    a scenario — deterministic without a database."""

    receipts: dict[str, dict[str, Any]]
    reviews: dict[str, dict[str, Any]] = field(default_factory=dict)
    posted: list[dict[str, Any]] = field(default_factory=list)
    submitted: list[dict[str, Any]] = field(default_factory=list)
    _created_keys: set[tuple[str, str]] = field(default_factory=set)

    def post_lead(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.posted.append(payload)
        key = (payload["source"], payload["external_ref"])
        lead_id = payload["external_ref"]
        if key in self._created_keys:
            return {"lead_id": lead_id, "created": False, "job_created": False}
        self._created_keys.add(key)
        return {"lead_id": lead_id, "created": True, "job_created": True}

    def wait_for_decision(
        self, lead_id: str, *, timeout_s: float, poll_interval_s: float = 1.0
    ) -> dict[str, Any]:
        return self.receipts[lead_id]

    def get_receipt(self, lead_id: str) -> dict[str, Any]:
        return self.receipts[lead_id]

    def get_review(self, review_id: str) -> dict[str, Any]:
        return self.reviews[review_id]

    def submit_review_decision(self, review_id: str, **kwargs: Any) -> dict[str, Any]:
        self.submitted.append({"review_id": review_id, **kwargs})
        for receipt in self.receipts.values():
            hr = receipt.get("human_review")
            if hr is not None and hr["review_id"] == review_id:
                hr["action"] = kwargs["action"]
                hr["final_decision"] = "auto_route"
                hr["reviewer"] = kwargs["reviewer"]
                hr["responded_at"] = "2026-08-18T00:01:00Z"
                receipt["decision"]["human_override"] = True
                receipt["decision"]["final_status"] = "AUTO_ROUTED"
                receipt["lead_status"] = "AUTO_ROUTED"
        return {"final_decision": "auto_route"}


def _minimal_receipt(
    lead_id: str,
    *,
    cache_hits: int = 0,
    autonomous: bool,
    review_id: str | None = None,
) -> dict[str, Any]:
    human_review = (
        None
        if review_id is None
        else {
            "review_id": review_id,
            "required": True,
            "reviewer": None,
            "original_decision": "reject",
            "action": None,
            "final_decision": None,
            "responded_at": None,
        }
    )
    return {
        "receipt_version": "1",
        "lead_id": lead_id,
        "status": "decided",
        "lead_status": "AUTO_ROUTED" if autonomous else "AWAITING_HUMAN",
        "created_at": "2026-08-18T00:00:00Z",
        "decision": {
            "recommended_action": "auto_route" if autonomous else "reject",
            "autonomous": autonomous,
            "final_status": "AUTO_ROUTED" if autonomous else "AWAITING_HUMAN",
            "human_override": False,
        },
        "score": {
            "value": 70.0,
            "threshold_qualify": 65.0,
            "threshold_reject": 55.0,
            "bounds": {"lower": 60.0, "upper": 80.0},
            "confidence": 0.9 if autonomous else 0.2,
            "tau": 0.8,
        },
        "stopping": {"reason_code": "confidence_reached", "explanation": "..."},
        "versions": {
            "policy": "calibrated_bounds",
            "scorer": "icp-1.0.0",
            "confidence_calibration": "platt",
        },
        "cost": {
            "provider_cost_usd": "0.1",
            "model_cost_usd": "0",
            "total_cost_usd": "0.1",
            "budget_usd_cap": "1.5",
        },
        "evidence": {
            "cache_hits": cache_hits,
            "provider_calls": 1,
            "items": [],
            "unknown_fields": [],
        },
        "providers": {
            "called": [
                {
                    "provider": "internal_crm",
                    "status": "success",
                    "cost_usd": "0.0",
                    "latency_ms": 10,
                    "cache_hit": i < cache_hits,
                }
                for i in range(max(cache_hits, 1))
            ],
            "not_called": [],
        },
        "human_review": human_review,
    }


# --------------------------------------------------------------- scenario a/c --


def test_run_scenario_a_reuses_the_same_external_ref_for_the_idempotency_proof(
    leads: list[EvalLead],
) -> None:
    corpus = select_demo_corpus(leads=leads)
    run_id = "runid1"
    external_ref = f"demo-{run_id}-autonomous"
    client = _FakeClient(receipts={external_ref: _minimal_receipt(external_ref, autonomous=True)})

    rendered, idempotency = run_scenario_a(client, corpus, run_id=run_id, decision_timeout_s=1.0)

    assert len(client.posted) == 2
    assert client.posted[0] == client.posted[1], "identical body both times"
    assert idempotency.is_idempotent is True
    assert rendered.autonomous is True


# ----------------------------------------------------------------- scenario b --


def test_run_scenario_b_approves_using_only_ids_the_receipt_exposed(
    leads: list[EvalLead],
) -> None:
    corpus = select_demo_corpus(leads=leads)
    run_id = "runid2"
    lead_id = f"demo-{run_id}-escalating"
    review_id = "review-abc"
    receipt = _minimal_receipt(lead_id, autonomous=False, review_id=review_id)
    client = _FakeClient(receipts={lead_id: receipt}, reviews={review_id: {"lead_version": 6}})

    before, after = run_scenario_b(client, corpus, run_id=run_id, decision_timeout_s=1.0)

    assert before.human_override is False
    assert client.submitted == [
        {
            "review_id": review_id,
            "action": "approve",
            "reviewer": "arie-demo",
            "expected_lead_version": 6,
        }
    ]
    assert after.final_status == "AUTO_ROUTED"
    assert after.human_override is True
    assert after.recommended_action == before.recommended_action, "never rewritten"


def test_run_scenario_b_never_queries_postgres_for_the_review_id(leads: list[EvalLead]) -> None:
    """The review id must come from the receipt's own `human_review.review_id`
    — this test fails if that wiring ever gets bypassed for a direct lookup."""
    corpus = select_demo_corpus(leads=leads)
    run_id = "runid3"
    lead_id = f"demo-{run_id}-escalating"
    review_id = "review-xyz"
    receipt = _minimal_receipt(lead_id, autonomous=False, review_id=review_id)
    client = _FakeClient(receipts={lead_id: receipt}, reviews={review_id: {"lead_version": 1}})

    run_scenario_b(client, corpus, run_id=run_id, decision_timeout_s=1.0)

    assert client.submitted[0]["review_id"] == review_id


# ----------------------------------------------------------------- scenario d --


def test_run_scenario_d_returns_the_pair_when_cache_reuse_is_observed(
    leads: list[EvalLead],
) -> None:
    corpus = select_demo_corpus(leads=leads)
    run_id = "runid4"
    ref1, ref2 = f"demo-{run_id}-samecompany-1", f"demo-{run_id}-samecompany-2"
    client = _FakeClient(
        receipts={
            ref1: _minimal_receipt(ref1, autonomous=True, cache_hits=0),
            ref2: _minimal_receipt(ref2, autonomous=True, cache_hits=1),
        }
    )

    result = run_scenario_d(client, corpus, run_id=run_id, decision_timeout_s=1.0)

    assert result is not None
    _lead1, lead2 = result
    assert lead2.activity.cache_reused_count == 1


def test_run_scenario_d_omits_rather_than_fabricates_when_no_reuse_is_observed(
    leads: list[EvalLead],
) -> None:
    corpus = select_demo_corpus(leads=leads)
    run_id = "runid5"
    ref1, ref2 = f"demo-{run_id}-samecompany-1", f"demo-{run_id}-samecompany-2"
    client = _FakeClient(
        receipts={
            ref1: _minimal_receipt(ref1, autonomous=True, cache_hits=0),
            ref2: _minimal_receipt(ref2, autonomous=True, cache_hits=0),
        }
    )

    assert run_scenario_d(client, corpus, run_id=run_id, decision_timeout_s=1.0) is None
