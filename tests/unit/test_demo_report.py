"""`scripts.demo.report` — summary computation and HTML/JSON rendering.

Builds `RenderedDecision`/`IdempotencyResult` values directly (no receipt
dicts, no HTTP) since report.py operates one level above `render.py`.
"""

from __future__ import annotations

import json

from scripts.demo.render import (
    HumanReviewRendering,
    ProviderActivity,
    ProviderCall,
    RenderedDecision,
)
from scripts.demo.report import DemoRun, compute_summary, render_html, render_json
from scripts.demo.scenarios import IdempotencyResult


def _decision(
    *,
    label: str = "Test Lead — Test Co",
    autonomous: bool | None = True,
    human_override: bool = False,
    human_review: HumanReviewRendering | None = None,
    fresh: tuple[ProviderCall, ...] = (),
    cache_reused: tuple[ProviderCall, ...] = (),
    total_cost_usd: str = "0.10",
) -> RenderedDecision:
    return RenderedDecision(
        label=label,
        lead_id="11111111-1111-1111-1111-111111111111",
        status="decided",
        lead_status="AUTO_ROUTED",
        recommended_action="auto_route",
        final_status="AUTO_ROUTED",
        autonomous=autonomous,
        human_override=human_override,
        score_value=70.0,
        score_lower=60.0,
        score_upper=80.0,
        threshold_qualify=65.0,
        threshold_reject=55.0,
        confidence=0.9,
        tau=0.8,
        stop_reason="confidence_reached",
        stop_explanation="Confidence cleared the bar.",
        provider_cost_usd=total_cost_usd,
        model_cost_usd="0",
        total_cost_usd=total_cost_usd,
        evidence_known=("industry",),
        evidence_unknown=("disqualifying_flag",),
        activity=ProviderActivity(
            fresh=fresh, cache_reused=cache_reused, not_called=("deep_research",)
        ),
        policy_version="calibrated_bounds",
        scorer_version="icp-1.0.0",
        confidence_calibration="platt",
        human_review=human_review,
    )


def _idempotency(*, ok: bool = True) -> IdempotencyResult:
    lead_id = "11111111-1111-1111-1111-111111111111"
    return IdempotencyResult(
        source="arie-demo",
        external_ref="demo-abc123-autonomous",
        first={"lead_id": lead_id, "created": True, "job_created": True},
        second={
            "lead_id": lead_id,
            "created": not ok,
            "job_created": not ok,
        },
    )


def _run(**overrides) -> DemoRun:  # type: ignore[no-untyped-def]
    defaults = dict(
        generated_at="2026-08-18T00:00:00Z",
        run_id="abc123",
        scenario_a=_decision(fresh=(ProviderCall("inbound_payload", "success", "0.0", 0),)),
        scenario_a_idempotency=_idempotency(),
        scenario_b_before=_decision(autonomous=False),
        scenario_b_after=_decision(
            autonomous=False,
            human_override=True,
            human_review=HumanReviewRendering(
                review_id="44444444-4444-4444-4444-444444444444",
                required=True,
                reviewer="arie-demo",
                original_decision="reject",
                action="approve",
                final_decision="auto_route",
                responded_at="2026-08-18T00:01:00Z",
                is_override=True,
            ),
            cache_reused=(ProviderCall("dns_web", "success", "0.0", 100),),
        ),
        scenario_d=None,
    )
    defaults.update(overrides)
    return DemoRun(**defaults)  # type: ignore[arg-type]


# ------------------------------------------------------------------ summary --


def test_summary_counts_each_demonstrated_lead_once() -> None:
    """scenario_b_before and scenario_b_after are the same lead -- must not
    be double-counted."""
    summary = compute_summary(_run())
    assert summary.leads_demonstrated == 2  # scenario_a + scenario_b_after


def test_summary_counts_autonomous_and_escalated_separately() -> None:
    summary = compute_summary(_run())
    assert summary.autonomous_decisions == 1  # scenario_a only
    assert summary.human_escalations == 1  # scenario_b_after
    assert summary.human_overrides == 1


def test_summary_sums_actual_spend_across_demonstrated_leads() -> None:
    run = _run(
        scenario_a=_decision(total_cost_usd="0.407"),
        scenario_b_after=_decision(autonomous=False, total_cost_usd="0.340"),
    )
    summary = compute_summary(run)
    assert summary.total_spend_usd == "0.747"


def test_summary_includes_scenario_d_when_present() -> None:
    run = _run(
        scenario_d=(
            _decision(),
            _decision(cache_reused=(ProviderCall("dns_web", "success", "0.0", 0),)),
        )
    )
    summary = compute_summary(run)
    assert summary.leads_demonstrated == 4


# --------------------------------------------------------------------- JSON --


def test_json_report_round_trips_and_has_no_stray_objects() -> None:
    body = render_json(_run())
    parsed = json.loads(body)
    assert parsed["run_id"] == "abc123"
    assert parsed["summary"]["leads_demonstrated"] == 2
    assert parsed["scenario_d"] is None


# --------------------------------------------------------------------- HTML --


def test_html_report_contains_the_required_header() -> None:
    body = render_html(_run())
    assert "Adaptive Revenue Intelligence Engine" in body
    assert "ARIE decides when more enrichment is actually worth acquiring." in body


def test_html_report_escapes_a_malicious_label() -> None:
    """A company/person name is attacker-controllable in principle (it came
    from an inbound lead in production, even if this demo's own corpus is
    trusted) -- must never be interpolated into markup unescaped."""
    run = _run(scenario_a=_decision(label="<script>alert(1)</script>"))
    body = render_html(run)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_html_report_escapes_reviewer_and_action_fields() -> None:
    run = _run(
        scenario_b_after=_decision(
            autonomous=False,
            human_review=HumanReviewRendering(
                review_id="44444444-4444-4444-4444-444444444444",
                required=True,
                reviewer='"><img src=x onerror=alert(1)>',
                original_decision="reject",
                action="approve",
                final_decision="auto_route",
                responded_at="2026-08-18T00:01:00Z",
                is_override=True,
            ),
        )
    )
    body = render_html(run)
    assert "<img src=x onerror=alert(1)>" not in body


def test_html_report_shows_fresh_vs_cache_distinctly() -> None:
    body = render_html(_run())
    assert "inbound_payload" in body
    assert "dns_web" in body
    assert "Fresh provider calls" in body
    assert "Cache reuses" in body


def test_html_report_shows_idempotency_verdict() -> None:
    body = render_html(_run())
    assert "No duplicate lead or job created." in body


def test_html_report_flags_an_unexpected_duplicate() -> None:
    run = _run(scenario_a_idempotency=_idempotency(ok=False))
    body = render_html(run)
    assert "Unexpected: a duplicate was created." in body


def test_html_report_omits_scenario_d_when_absent() -> None:
    body = render_html(_run(scenario_d=None))
    # Only two decision cards (scenario_a, scenario_b_after) -- both share
    # the same label default in these fixtures, so count card containers.
    assert body.count('<div class="card">') + body.count('<div class="card idempotency">') == 3
