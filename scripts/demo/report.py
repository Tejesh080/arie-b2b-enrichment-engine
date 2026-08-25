"""Renders a `DemoRun` into a self-contained HTML report and a JSON sidecar.

No CDN dependency, no build step — open the HTML file directly in a browser.
Every interpolated string is escaped; nothing here trusts a receipt field to
be safe to drop into markup as-is.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from scripts.demo.render import HumanReviewRendering, ProviderActivity, RenderedDecision
from scripts.demo.scenarios import IdempotencyResult


@dataclass(frozen=True)
class DemoRun:
    generated_at: str
    run_id: str
    scenario_a: RenderedDecision
    scenario_a_idempotency: IdempotencyResult
    scenario_b_before: RenderedDecision
    scenario_b_after: RenderedDecision
    scenario_d: tuple[RenderedDecision, RenderedDecision] | None


@dataclass(frozen=True)
class RunSummary:
    leads_demonstrated: int
    autonomous_decisions: int
    human_escalations: int
    human_overrides: int
    fresh_provider_calls: int
    cache_reuses: int
    total_spend_usd: str


def _demonstrated_leads(run: DemoRun) -> tuple[RenderedDecision, ...]:
    """The settled rendering for each lead the report shows a card for —
    `scenario_b_after` only, not `_before` too, since it's the same lead and
    counting it twice would double leads_demonstrated and total spend."""
    leads = [run.scenario_a, run.scenario_b_after]
    if run.scenario_d is not None:
        leads.extend(run.scenario_d)
    return tuple(leads)


def compute_summary(run: DemoRun) -> RunSummary:
    """Every figure here is a direct read or a plain sum over what the
    receipts in this run actually reported — nothing recomputed or estimated."""
    leads = _demonstrated_leads(run)
    total_spend = sum((Decimal(lead.total_cost_usd) for lead in leads), start=Decimal(0))
    return RunSummary(
        leads_demonstrated=len(leads),
        autonomous_decisions=sum(1 for lead in leads if lead.autonomous is True),
        human_escalations=sum(1 for lead in leads if lead.human_review is not None),
        human_overrides=sum(1 for lead in leads if lead.human_override),
        fresh_provider_calls=sum(lead.activity.fresh_count for lead in leads),
        cache_reuses=sum(lead.activity.cache_reused_count for lead in leads),
        total_spend_usd=str(total_spend),
    )


def render_json(run: DemoRun) -> str:
    """Public API outputs and demo metadata only — no credentials, nothing
    that didn't come from an ARIE HTTP response."""
    payload = {
        "generated_at": run.generated_at,
        "run_id": run.run_id,
        "summary": asdict(compute_summary(run)),
        "scenario_a": asdict(run.scenario_a),
        "scenario_a_idempotency": asdict(run.scenario_a_idempotency),
        "scenario_b_before": asdict(run.scenario_b_before),
        "scenario_b_after": asdict(run.scenario_b_after),
        "scenario_d": (
            [asdict(run.scenario_d[0]), asdict(run.scenario_d[1])]
            if run.scenario_d is not None
            else None
        ),
    }
    return json.dumps(payload, indent=2, default=str)


# --------------------------------------------------------------------- HTML --

_CSS = """
:root {
  --bg: #0b0d12; --panel: #141822; --panel-2: #1b2130; --border: #262d3d;
  --text: #e8ecf5; --text-dim: #9aa3b8; --accent: #5b8cff; --good: #35c78a;
  --warn: #e8b34d; --bad: #ef6161;
  font-family: -apple-system, "Segoe UI", Inter, Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
body {
  background: var(--bg); color: var(--text); margin: 0; padding: 2.5rem 1.5rem 4rem;
  line-height: 1.5;
}
.wrap { max-width: 980px; margin: 0 auto; }
header.hero { margin-bottom: 2.5rem; }
header.hero h1 { font-size: 1.9rem; margin: 0 0 .35rem; letter-spacing: -0.01em; }
header.hero p.tagline { color: var(--text-dim); font-size: 1.05rem; margin: 0 0 .5rem; }
header.hero p.meta { color: var(--text-dim); font-size: .8rem; margin: 0; }
h2.section-title {
  font-size: .78rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--text-dim); margin: 2.5rem 0 .9rem; font-weight: 600;
}
.stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: .75rem; }
.stat {
  background: var(--panel); border: 1px solid var(--border); border-radius: 10px;
  padding: .9rem 1rem;
}
.stat .value { font-size: 1.5rem; font-weight: 650; }
.stat .label { color: var(--text-dim); font-size: .75rem; margin-top: .15rem; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.4rem 1.5rem; margin-bottom: 1.1rem;
}
.card-head { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
.card-head h3 { margin: 0; font-size: 1.15rem; }
.badge {
  display: inline-block; padding: .18rem .6rem; border-radius: 999px;
  font-size: .72rem; font-weight: 650; letter-spacing: .02em; text-transform: uppercase;
}
.badge.autonomous { background: rgba(53,199,138,.15); color: var(--good); }
.badge.human { background: rgba(232,179,77,.15); color: var(--warn); }
.badge.override { background: rgba(91,140,255,.15); color: var(--accent); }
.grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: .5rem 1.5rem; margin-top: 1rem; }
.kv .k { color: var(--text-dim); font-size: .74rem; text-transform: uppercase; letter-spacing: .04em; }
.kv .v { font-size: .98rem; margin-top: .1rem; }
.why { background: var(--panel-2); border-radius: 8px; padding: .8rem 1rem; margin-top: 1rem; font-size: .92rem; color: var(--text-dim); }
.flow { display: flex; align-items: center; gap: .6rem; margin: 1rem 0; flex-wrap: wrap; font-size: .92rem; }
.flow .step { background: var(--panel-2); border: 1px solid var(--border); border-radius: 8px; padding: .5rem .8rem; }
.flow .arrow { color: var(--text-dim); }
.activity { margin-top: 1rem; }
.activity .row { display: flex; gap: .5rem; align-items: baseline; margin-bottom: .3rem; flex-wrap: wrap; }
.activity .row .tag {
  font-size: .74rem; text-transform: uppercase; letter-spacing: .04em; color: var(--text-dim);
  min-width: 9rem;
}
.pill {
  display: inline-block; background: var(--panel-2); border: 1px solid var(--border);
  border-radius: 6px; padding: .1rem .5rem; font-size: .78rem; margin: .1rem .25rem .1rem 0;
}
.pill.fresh { border-color: rgba(53,199,138,.4); }
.pill.cache { border-color: rgba(91,140,255,.4); }
.pill.skip { opacity: .6; }
.idempotency .flow .step.ok { border-color: rgba(53,199,138,.5); }
footer { color: var(--text-dim); font-size: .78rem; margin-top: 3rem; border-top: 1px solid var(--border); padding-top: 1rem; }
"""


def _esc(value: Any) -> str:
    return html.escape(str(value)) if value is not None else "—"


def _fmt_pct(value: float | None) -> str:
    return f"{value * 100:.1f}%" if value is not None else "—"


def _fmt_num(value: float | None) -> str:
    return f"{value:.2f}" if value is not None else "—"


def _activity_html(activity: ProviderActivity) -> str:
    fresh = (
        "".join(f'<span class="pill fresh">{_esc(c.provider)}</span>' for c in activity.fresh)
        or '<span class="pill">none</span>'
    )
    cache = (
        "".join(
            f'<span class="pill cache">{_esc(c.provider)}</span>' for c in activity.cache_reused
        )
        or '<span class="pill">none</span>'
    )
    skipped = (
        "".join(f'<span class="pill skip">{_esc(p)}</span>' for p in activity.not_called)
        or '<span class="pill">none</span>'
    )
    return f"""
<div class="activity">
  <div class="row"><span class="tag">Fresh provider calls ({activity.fresh_count})</span>{fresh}</div>
  <div class="row"><span class="tag">Cache reuses ({activity.cache_reused_count})</span>{cache}</div>
  <div class="row"><span class="tag">Not called this run</span>{skipped}</div>
</div>
"""


def _human_flow_html(hr: HumanReviewRendering | None, recommended_action: str | None) -> str:
    if hr is None or hr.final_decision is None:
        return ""
    override_badge = '<span class="badge override">Human override</span>' if hr.is_override else ""
    return f"""
<div class="flow">
  <div class="step">Recommendation: {_esc(recommended_action)}</div>
  <span class="arrow">&rarr;</span>
  <div class="step">Human ({_esc(hr.reviewer)}): {_esc(hr.action)}</div>
  <span class="arrow">&rarr;</span>
  <div class="step">Final: {_esc(hr.final_decision)}</div>
  {override_badge}
</div>
"""


def _decision_card_html(rendered: RenderedDecision) -> str:
    badge = (
        '<span class="badge autonomous">Autonomous</span>'
        if rendered.autonomous
        else '<span class="badge human">Human review</span>'
    )
    evidence_known = ", ".join(rendered.evidence_known) or "none"
    evidence_unknown = ", ".join(rendered.evidence_unknown) or "none"
    human_flow = _human_flow_html(rendered.human_review, rendered.recommended_action)

    return f"""
<div class="card">
  <div class="card-head">
    <h3>{_esc(rendered.label)}</h3>
    {badge}
  </div>
  {human_flow}
  <div class="grid-2">
    <div class="kv"><div class="k">Recommended action</div><div class="v">{_esc(rendered.recommended_action)}</div></div>
    <div class="kv"><div class="k">Final status</div><div class="v">{_esc(rendered.final_status)}</div></div>
    <div class="kv"><div class="k">Score</div><div class="v">{_fmt_num(rendered.score_value)} (bounds {_fmt_num(rendered.score_lower)}&ndash;{_fmt_num(rendered.score_upper)})</div></div>
    <div class="kv"><div class="k">Qualify / reject thresholds</div><div class="v">{_fmt_num(rendered.threshold_qualify)} / {_fmt_num(rendered.threshold_reject)}</div></div>
    <div class="kv"><div class="k">Calibrated confidence</div><div class="v">{_fmt_pct(rendered.confidence)}</div></div>
    <div class="kv"><div class="k">Autonomy threshold (&tau;)</div><div class="v">{_fmt_pct(rendered.tau)}</div></div>
    <div class="kv"><div class="k">Actual provider spend</div><div class="v">${_esc(rendered.provider_cost_usd)}</div></div>
    <div class="kv"><div class="k">Actual model spend</div><div class="v">${_esc(rendered.model_cost_usd)}</div></div>
  </div>
  <div class="why"><strong>Why processing stopped ({_esc(rendered.stop_reason)}):</strong> {_esc(rendered.stop_explanation)}</div>
  {_activity_html(rendered.activity)}
  <div class="grid-2" style="margin-top:.75rem;">
    <div class="kv"><div class="k">Evidence known</div><div class="v">{_esc(evidence_known)}</div></div>
    <div class="kv"><div class="k">Evidence unknown</div><div class="v">{_esc(evidence_unknown)}</div></div>
  </div>
  <div class="grid-2" style="margin-top:.75rem;">
    <div class="kv"><div class="k">Policy</div><div class="v">{_esc(rendered.policy_version)}</div></div>
    <div class="kv"><div class="k">Scorer</div><div class="v">{_esc(rendered.scorer_version)}</div></div>
    <div class="kv"><div class="k">Confidence calibration</div><div class="v">{_esc(rendered.confidence_calibration)}</div></div>
  </div>
</div>
"""


def _idempotency_html(result: IdempotencyResult) -> str:
    ok = result.is_idempotent
    verdict = "No duplicate lead or job created." if ok else "Unexpected: a duplicate was created."
    verdict_class = "ok" if ok else ""
    return f"""
<div class="card idempotency">
  <div class="card-head"><h3>Redelivering the same request</h3></div>
  <div class="flow">
    <div class="step">1st POST /leads &mdash; created={_esc(result.first.get("created"))}, job_created={_esc(result.first.get("job_created"))}</div>
    <span class="arrow">&rarr;</span>
    <div class="step {verdict_class}">2nd POST /leads (identical body) &mdash; created={_esc(result.second.get("created"))}, job_created={_esc(result.second.get("job_created"))}</div>
  </div>
  <div class="why">{_esc(verdict)}</div>
</div>
"""


def _summary_html(summary: RunSummary) -> str:
    tiles = [
        ("Leads demonstrated", summary.leads_demonstrated),
        ("Autonomous decisions", summary.autonomous_decisions),
        ("Human escalations", summary.human_escalations),
        ("Human overrides", summary.human_overrides),
        ("Fresh provider calls", summary.fresh_provider_calls),
        ("Cache reuses", summary.cache_reuses),
        ("Actual total spend", f"${summary.total_spend_usd}"),
    ]
    stats = "".join(
        f'<div class="stat"><div class="value">{_esc(v)}</div><div class="label">{_esc(k)}</div></div>'
        for k, v in tiles
    )
    return f'<div class="stats">{stats}</div>'


def render_html(run: DemoRun) -> str:
    summary = compute_summary(run)
    cards = [_decision_card_html(run.scenario_a)]
    cards.append(_decision_card_html(run.scenario_b_after))
    if run.scenario_d is not None:
        cards.append(_decision_card_html(run.scenario_d[0]))
        cards.append(_decision_card_html(run.scenario_d[1]))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARIE — Decision Receipt Demo</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>Adaptive Revenue Intelligence Engine</h1>
    <p class="tagline">ARIE decides when more enrichment is actually worth acquiring.</p>
    <p class="meta">Generated {_esc(run.generated_at)} &middot; run {_esc(run.run_id)} &middot; every figure below is read from a live Decision Receipt, not reconstructed or estimated.</p>
  </header>

  <h2 class="section-title">This run, at a glance</h2>
  {_summary_html(summary)}

  <h2 class="section-title">Decisions</h2>
  {"".join(cards)}

  <h2 class="section-title">Reliability</h2>
  {_idempotency_html(run.scenario_a_idempotency)}

  <footer>
    Generated by <code>scripts/demo.ps1</code> from ARIE's public HTTP API only &mdash;
    <code>POST /leads</code>, <code>GET /leads/{{id}}/receipt</code>,
    <code>GET /reviews/{{id}}</code>, <code>POST /reviews/{{id}}/decision</code>.
    No Postgres queries, no internal state. Cost-quality benchmark results are
    reported separately in <code>docs/benchmark.md</code>.
  </footer>
</div>
</body>
</html>
"""
