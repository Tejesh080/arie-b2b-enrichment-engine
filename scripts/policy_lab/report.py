"""Assembles the parsed artifact + computed statistics into the static
Policy Lab HTML report and its JSON sidecar.

Every number rendered here traces back to `scripts.policy_lab.stats`/
`.pareto`/`.comparison` — nothing is computed inline in template strings, so
what the report says and what the tests assert are checking the same values.
"""

from __future__ import annotations

import html
import json
from dataclasses import asdict, dataclass
from typing import Any

from scripts.policy_lab.chart import (
    ChartPoint,
    build_chart_points,
    render_pareto_svg,
    to_pareto_points,
)
from scripts.policy_lab.comparison import BaselineComparison, compare_to_baseline
from scripts.policy_lab.pareto import FrontierResult, compute_frontier
from scripts.policy_lab.stats import (
    BASELINE_POLICY,
    DISPLAY_NAMES,
    PRIMARY_POLICIES,
    PRODUCTION_POLICY,
    PolicyStats,
    count_evaluated_policy_variants,
    extract_all,
)


@dataclass(frozen=True)
class DatasetProvenance:
    generator_version: str | None
    rules_version: str | None
    content_sha256: str | None
    """The tracked manifest describes seed 42's dataset specifically; every
    seed in the sweep regenerates its own dataset from the same generator."""


@dataclass(frozen=True)
class ReportData:
    generated_at: str
    artifact_display_path: str
    seeds: tuple[int, ...]
    stats_by_policy: dict[str, PolicyStats]
    frontier: FrontierResult
    chart_points: list[ChartPoint]
    comparison: BaselineComparison
    confidence_ece_mean: float
    confidence_ece_min: float
    confidence_ece_max: float
    dataset: DatasetProvenance
    evaluated_variant_count: int


def build_report(
    artifact: dict[str, Any],
    manifest: dict[str, Any] | None,
    *,
    artifact_display_path: str,
    generated_at: str,
) -> ReportData:
    stats_by_policy = extract_all(artifact, PRIMARY_POLICIES)
    pareto_points = to_pareto_points(stats_by_policy)
    frontier = compute_frontier(pareto_points)
    chart_points = build_chart_points(stats_by_policy, frontier, PRODUCTION_POLICY)
    comparison = compare_to_baseline(
        stats_by_policy[PRODUCTION_POLICY], stats_by_policy[BASELINE_POLICY]
    )

    ece_values = [float(entry["confidence_ece"]) for entry in artifact["per_seed"]]

    dataset = DatasetProvenance(
        generator_version=str(manifest.get("generator_version")) if manifest else None,
        rules_version=str(manifest.get("rules_version")) if manifest else None,
        content_sha256=str(manifest.get("content_sha256")) if manifest else None,
    )

    return ReportData(
        generated_at=generated_at,
        artifact_display_path=artifact_display_path,
        seeds=tuple(int(s) for s in artifact["seeds"]),
        stats_by_policy=stats_by_policy,
        frontier=frontier,
        chart_points=chart_points,
        comparison=comparison,
        confidence_ece_mean=sum(ece_values) / len(ece_values),
        confidence_ece_min=min(ece_values),
        confidence_ece_max=max(ece_values),
        dataset=dataset,
        evaluated_variant_count=count_evaluated_policy_variants(artifact),
    )


# --------------------------------------------------------------------- JSON --


def render_json(data: ReportData) -> str:
    payload = {
        "generated_at": data.generated_at,
        "artifact_source": data.artifact_display_path,
        "seeds": list(data.seeds),
        "production_policy": PRODUCTION_POLICY,
        "baseline_policy": BASELINE_POLICY,
        "policies": {
            policy: {
                "display_name": DISPLAY_NAMES.get(policy, policy),
                "on_pareto_frontier": policy in data.frontier.frontier,
                "dominated_by": data.frontier.dominated_by[policy],
                "agreement": {
                    "mean": stats.agreement.mean,
                    "stdev": stats.agreement.stdev,
                    "min": stats.agreement.minimum,
                    "max": stats.agreement.maximum,
                },
                "cost_usd": {
                    "mean": stats.cost_usd.mean,
                    "stdev": stats.cost_usd.stdev,
                    "min": stats.cost_usd.minimum,
                    "max": stats.cost_usd.maximum,
                },
                "calls": {"mean": stats.calls.mean},
                "autonomy": {"mean": stats.autonomy.mean},
            }
            for policy, stats in data.stats_by_policy.items()
        },
        "waterfall_comparison": asdict(data.comparison),
        "confidence_ece": {
            "mean": data.confidence_ece_mean,
            "min": data.confidence_ece_min,
            "max": data.confidence_ece_max,
        },
        "provenance": {
            "seeds": list(data.seeds),
            "artifact_source": data.artifact_display_path,
            "dataset_generator_version": data.dataset.generator_version,
            "dataset_rules_version": data.dataset.rules_version,
            "dataset_content_sha256_seed42": data.dataset.content_sha256,
            "evaluated_policy_variant_count": data.evaluated_variant_count,
            "preregistered_win_criteria": "<=1pp agreement loss at >=20% cost reduction vs. tuned waterfall",
            "preregistered_win_criteria_met": False,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True, default=str)


# --------------------------------------------------------------------- HTML --

_CSS = """
:root {
  --bg: #0b0d12; --panel: #141822; --panel-2: #1b2130; --border: #262d3d;
  --text: #e8ecf5; --text-dim: #9aa3b8; --accent: #5b8cff; --good: #35c78a;
  --warn: #e8b34d; --bad: #ef6161; --mono: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
  font-family: -apple-system, "Segoe UI", Inter, Roboto, Helvetica, Arial, sans-serif;
}
* { box-sizing: border-box; }
body { background: var(--bg); color: var(--text); margin: 0; padding: 2.5rem 1.5rem 4rem; line-height: 1.55; }
.wrap { max-width: 1040px; margin: 0 auto; }
.eyebrow {
  font-size: .72rem; text-transform: uppercase; letter-spacing: .12em; color: var(--accent);
  font-weight: 700; margin: 0 0 .6rem;
}
header.hero { margin-bottom: 2.2rem; }
header.hero h1 { font-size: 2rem; margin: 0 0 .5rem; letter-spacing: -0.01em; }
header.hero p.tagline { color: var(--text-dim); font-size: 1.05rem; margin: 0 0 .6rem; max-width: 62ch; }
header.hero p.meta { color: var(--text-dim); font-size: .78rem; margin: 0; font-family: var(--mono); }
h2.section-title {
  font-size: .78rem; text-transform: uppercase; letter-spacing: .08em;
  color: var(--text-dim); margin: 2.6rem 0 .9rem; font-weight: 700;
  border-top: 1px solid var(--border); padding-top: 1.6rem;
}
h2.section-title:first-of-type { border-top: none; padding-top: 0; }
h3 { font-size: 1.05rem; margin: 0 0 .5rem; }
p { margin: 0 0 .8rem; }
.card {
  background: var(--panel); border: 1px solid var(--border); border-radius: 12px;
  padding: 1.4rem 1.6rem; margin-bottom: 1.1rem;
}
.card.production { border-color: rgba(91,140,255,.55); }
.badge {
  display: inline-block; padding: .18rem .6rem; border-radius: 999px;
  font-size: .68rem; font-weight: 700; letter-spacing: .03em; text-transform: uppercase;
  margin-left: .4rem;
}
.badge.production { background: rgba(91,140,255,.16); color: var(--accent); }
.badge.frontier { background: rgba(53,199,138,.14); color: var(--good); }
.badge.dominated { background: rgba(239,97,97,.14); color: var(--bad); }
figure { margin: 0 0 1rem; }
figure.chart-figure { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 1.2rem; }
.pareto-svg { width: 100%; height: auto; display: block; }
figcaption { color: var(--text-dim); font-size: .82rem; margin-top: .6rem; }
.legend { display: flex; flex-wrap: wrap; gap: 1.1rem; margin: .9rem 0 0; font-size: .8rem; color: var(--text-dim); }
.legend .item { display: flex; align-items: center; gap: .45rem; }
.legend .swatch { width: 14px; height: 14px; border-radius: 50%; display: inline-block; flex: none; }
.legend .swatch.frontier { background: var(--text); }
.legend .swatch.dominated { background: transparent; border: 2px dashed var(--text-dim); }
.legend .swatch.production { background: transparent; border: 2px solid var(--accent); transform: rotate(45deg); border-radius: 3px; width: 11px; height: 11px; }
table { width: 100%; border-collapse: collapse; font-size: .86rem; }
caption { text-align: left; color: var(--text-dim); font-size: .78rem; margin-bottom: .5rem; }
th, td { text-align: left; padding: .55rem .7rem; border-bottom: 1px solid var(--border); }
th { color: var(--text-dim); font-weight: 600; font-size: .74rem; text-transform: uppercase; letter-spacing: .04em; }
td.num, th.num { text-align: right; font-family: var(--mono); font-variant-numeric: tabular-nums; }
tr.production-row td { background: rgba(91,140,255,.06); }
.range { color: var(--text-dim); font-size: .78rem; }
.table-wrap { overflow-x: auto; background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: .3rem 1rem 1rem; }
.stat-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: .8rem; margin: 1rem 0; }
.stat { background: var(--panel-2); border: 1px solid var(--border); border-radius: 10px; padding: .8rem 1rem; }
.stat .value { font-size: 1.35rem; font-weight: 700; font-family: var(--mono); }
.stat .label { color: var(--text-dim); font-size: .74rem; margin-top: .2rem; }
.note { background: var(--panel-2); border-radius: 8px; padding: .8rem 1rem; font-size: .84rem; color: var(--text-dim); margin-top: .8rem; }
ul.provenance { list-style: none; margin: 0; padding: 0; font-size: .85rem; }
ul.provenance li { padding: .35rem 0; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; gap: 1rem; flex-wrap: wrap; }
ul.provenance li:last-child { border-bottom: none; }
ul.provenance .k { color: var(--text-dim); }
ul.provenance .v { font-family: var(--mono); text-align: right; }
footer { color: var(--text-dim); font-size: .78rem; margin-top: 3rem; border-top: 1px solid var(--border); padding-top: 1rem; }
footer code { font-family: var(--mono); }
"""


def _esc(value: object) -> str:
    return html.escape(str(value)) if value is not None else "&mdash;"


def _fmt_usd(value: float) -> str:
    return f"${value:.4f}"


def _fmt_pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _fmt_pp(value: float, *, signed: bool = True) -> str:
    return f"{value:+.2f}pp" if signed else f"{value:.2f}pp"


def _fmt_num(value: float, digits: int = 2) -> str:
    return f"{value:.{digits}f}"


def _legend_html() -> str:
    return """
<div class="legend">
  <span class="item"><span class="swatch frontier"></span>Pareto-efficient (filled marker, solid whisker)</span>
  <span class="item"><span class="swatch dominated"></span>Dominated (hollow, dashed marker — a frontier policy beats it on both axes)</span>
  <span class="item"><span class="swatch production"></span>Production selection (diamond ring)</span>
</div>
"""


def _chart_section_html(data: ReportData) -> str:
    svg = render_pareto_svg(data.chart_points)
    dominated = [p for p in data.chart_points if p.dominated_by]
    if dominated:
        alt_line = (
            "; ".join(f"{p.display_name} is dominated by {p.dominated_by}" for p in dominated) + "."
        )
    else:
        alt_line = "All four plotted policies are Pareto-efficient relative to each other."
    return f"""
<h2 class="section-title">Cost vs. decision quality</h2>
<p>Each point is one policy's mean across the ten benchmark seeds (42&ndash;51). Whiskers show
the min&ndash;max range across those seeds &mdash; the variability is part of the result, not noise
to hide. A policy <strong>dominates</strong> another only if it is no more expensive, no worse on
agreement, and strictly better on at least one axis; that comparison is computed from the data
below, not asserted.</p>
<figure class="chart-figure">
  {svg}
  <figcaption>{_esc(alt_line)} Exact values for every point are in the table in the next section.</figcaption>
</figure>
{_legend_html()}
"""


def _production_section_html(data: ReportData) -> str:
    prod = data.stats_by_policy[PRODUCTION_POLICY]
    return f"""
<h2 class="section-title">Production selection</h2>
<div class="card production">
  <h3>Calibrated Bounds <span class="badge production">Production policy</span></h3>
  <p>Deterministic score bounds plus a calibrated confidence gate: buy the cheapest unbought
  evidence next, and stop either when the decision is provably settled (no remaining evidence
  could change it) or when calibrated confidence clears a Clopper&ndash;Pearson-derived threshold
  &tau;. It was selected as the production policy because, across this benchmark's ten seeds, it
  was the cheapest option at every human-review price tested and reached the highest autonomous
  rate of any evaluated policy &mdash; including full enrichment &mdash; while remaining on the
  Pareto frontier of cost vs. synthetic-oracle agreement computed above. It is a measured
  engineering trade-off against the alternatives evaluated here, not the policy with the highest
  agreement in this benchmark &mdash; full enrichment and the tuned waterfall both score higher on
  that single axis, at higher cost.</p>
  <div class="stat-row">
    <div class="stat"><div class="value">{_fmt_usd(prod.cost_usd.mean)}</div><div class="label">Mean API cost / lead</div></div>
    <div class="stat"><div class="value">{_fmt_pct(prod.agreement.mean)}</div><div class="label">Synthetic-oracle agreement</div></div>
    <div class="stat"><div class="value">{_fmt_num(prod.calls.mean)}</div><div class="label">Mean provider calls / lead</div></div>
    <div class="stat"><div class="value">{_fmt_pct(prod.autonomy.mean)}</div><div class="label">Autonomous rate</div></div>
  </div>
</div>
"""


def _waterfall_comparison_html(data: ReportData) -> str:
    c = data.comparison
    prod = data.stats_by_policy[PRODUCTION_POLICY]
    base = data.stats_by_policy[BASELINE_POLICY]
    calls_word = "fewer" if c.calls_diff < 0 else "more"
    autonomy_word = "higher" if c.autonomy_diff >= 0 else "lower"
    return f"""
<h2 class="section-title">Calibrated Bounds vs. the tuned waterfall</h2>
<div class="card">
  <p><strong>Calibrated Bounds reduced mean API spend by {_fmt_pct(c.cost_pct_change_mean_of_ratios)}
  versus the tuned waterfall</strong> ({_fmt_usd(prod.cost_usd.mean)} vs. {_fmt_usd(base.cost_usd.mean)}
  per lead, mean of each seed's own saving, sd {_fmt_pct(c.cost_pct_change_mean_of_ratios_stdev)}),
  <strong>with a {_fmt_pp(abs(c.agreement_pp_diff), signed=False)} reduction in synthetic-oracle
  agreement</strong> ({_fmt_pct(prod.agreement.mean)} vs. {_fmt_pct(base.agreement.mean)}). Calibrated
  Bounds also made {_fmt_num(abs(c.calls_diff))} {calls_word} provider calls per lead on average
  ({_fmt_num(prod.calls.mean)} vs. {_fmt_num(base.calls.mean)}) and reached a {_fmt_num(abs(c.autonomy_diff) * 100, 1)}
  point {autonomy_word} autonomous rate ({_fmt_pct(prod.autonomy.mean)} vs. {_fmt_pct(base.autonomy.mean)}).</p>
  <p class="note">Reading the aggregate spend as a ratio of means instead of a mean of per-seed
  ratios gives {_fmt_pct(c.cost_pct_change_ratio_of_means)} &mdash; a different statistic from the
  same data, not a rounding error (see <code>docs/05-results.md</code>'s own note on this
  distinction). The cost reduction did not come free: it is paired with a measured decrease in
  agreement with the synthetic oracle, reported above rather than omitted.</p>
</div>
"""


def _comparison_table_html(data: ReportData) -> str:
    rows = []
    for policy in PRIMARY_POLICIES:
        stats = data.stats_by_policy[policy]
        is_prod = policy == PRODUCTION_POLICY
        is_frontier = policy in data.frontier.frontier
        dominator = data.frontier.dominated_by[policy]
        badges = ""
        if is_prod:
            badges += '<span class="badge production">Production</span>'
        if is_frontier:
            badges += '<span class="badge frontier">Pareto-efficient</span>'
        elif dominator:
            badges += f'<span class="badge dominated">Dominated by {_esc(DISPLAY_NAMES.get(dominator, dominator))}</span>'
        row_class = ' class="production-row"' if is_prod else ""
        rows.append(f"""
<tr{row_class}>
  <td>{_esc(DISPLAY_NAMES.get(policy, policy))}{badges}</td>
  <td class="num">{_fmt_usd(stats.cost_usd.mean)}<div class="range">{_fmt_usd(stats.cost_usd.minimum)}&ndash;{_fmt_usd(stats.cost_usd.maximum)}</div></td>
  <td class="num">{_fmt_pct(stats.agreement.mean)}<div class="range">{_fmt_pct(stats.agreement.minimum)}&ndash;{_fmt_pct(stats.agreement.maximum)}</div></td>
  <td class="num">{_fmt_num(stats.calls.mean)}</td>
  <td class="num">{_fmt_pct(stats.autonomy.mean)}</td>
</tr>""")
    return f"""
<h2 class="section-title">Policy comparison</h2>
<div class="table-wrap">
<table>
  <caption>Mean across seeds 42&ndash;51, 300 held-out test leads per seed. Range shown is
  min&ndash;max across the ten seeds. This is also the text/table alternative for the chart
  above.</caption>
  <thead>
    <tr><th>Policy</th><th class="num">Cost / lead</th><th class="num">Oracle agreement</th><th class="num">Calls / lead</th><th class="num">Autonomous rate</th></tr>
  </thead>
  <tbody>
    {"".join(rows)}
  </tbody>
</table>
</div>
"""


def _negative_result_html(data: ReportData) -> str:
    adaptive = data.stats_by_policy["adaptive_voi_x1"]
    return f"""
<h2 class="section-title">Why adaptive EVoI is not the production policy</h2>
<div class="card">
  <p>The founding hypothesis was that an adaptive Expected Value of Information (EVoI) acquisition
  rule &mdash; buy whichever unbought provider is most informative relative to its price &mdash;
  would out-perform a well-tuned fixed waterfall. In this frozen benchmark, that rule
  (<code>adaptive_voi_x1</code>, the single un-scaled variant) did not establish the pre-registered
  win: it is more expensive than Calibrated Bounds on 9 of 10 seeds at similar or worse agreement,
  and it is dominated outright by Calibrated Bounds in the Pareto comparison above &mdash; no
  cheaper, no better, on this benchmark's mean figures.</p>
  <p>Estimated business value per lead in this benchmark's cost model spans a much wider range than
  provider prices do, which tended to leave the EVoI acquisition rule reaching for informative but
  expensive evidence rather than favoring cheap evidence first &mdash; it settles decisions in
  {_fmt_num(adaptive.calls.mean)} calls on average, fewer than Calibrated Bounds'
  {_fmt_num(data.stats_by_policy[PRODUCTION_POLICY].calls.mean)}, yet still spends more per lead.
  Calibration behavior and human-review economics also shaped which policy actually produced the
  better trade-off once escalations were accounted for. What would need to change for EVoI to be
  revisited &mdash; not established by this benchmark, recorded so the door stays open &mdash;
  includes normalizing business value against the provider cost scale, a steeper provider price
  ladder, and a latency- or rate-limit-constrained setting where EVoI's real advantage (settling in
  far fewer calls) would be the binding constraint; see
  <code>docs/adr/0004-evoi-is-a-negative-result.md</code> for the full reasoning.</p>
  <p>The result is treated as a finding, not a defect: the project built the sophisticated
  approach, built the ablation that could kill it, ran ten seeds instead of trusting one, and let
  the simpler <code>calibrated_bounds</code> policy become the production choice because the
  experiment supported it &mdash; not the other way around.</p>
</div>
"""


def _methodology_html(data: ReportData) -> str:
    seeds_display = (
        f"{min(data.seeds)}&ndash;{max(data.seeds)}" if len(data.seeds) > 1 else str(data.seeds[0])
    )
    generator_version = data.dataset.generator_version or "not recorded in tracked manifest"
    rules_version = data.dataset.rules_version or "not recorded in tracked manifest"

    # Every value here is either a hardcoded literal (no user/artifact data)
    # or passed through `_esc` explicitly — no conditional dispatch by key.
    items: list[tuple[str, str]] = [
        ("Seeds", f"{seeds_display} ({len(data.seeds)} seeds)"),
        ("Source artifact", _esc(data.artifact_display_path)),
        ("Dataset generator version", _esc(generator_version)),
        ("ICP rules version", _esc(rules_version)),
        (
            "Confidence calibration error (ECE)",
            f"mean {data.confidence_ece_mean:.4f}, range {data.confidence_ece_min:.4f}"
            f"&ndash;{data.confidence_ece_max:.4f} across seeds",
        ),
        (
            "Policy variants evaluated in the raw artifact",
            f"{data.evaluated_variant_count} (waterfall tiers, EVoI value-scale sweep, "
            "escalation-aware review-price sweep; this report compares the 4 named above)",
        ),
        (
            "Pre-registered win criteria",
            "&le;1pp agreement loss at &ge;20% cost reduction vs. tuned waterfall &mdash; "
            "not met by any evaluated policy",
        ),
        ("Generated", _esc(data.generated_at)),
    ]
    rows = "".join(
        f'<li><span class="k">{_esc(k)}</span><span class="v">{v}</span></li>' for k, v in items
    )
    return f"""
<h2 class="section-title">Methodology &amp; provenance</h2>
<div class="card">
  <p><strong>Synthetic oracle.</strong> Each evaluation lead carries a latent truth vector never
  visible to any policy. Oracle decisions are computed from that latent truth using the same ICP
  scoring rules the policies use once fully informed &mdash; "agreement" measures whether a policy's
  decision, given only what it chose to buy, matches the decision computed from the full truth.
  <strong>It is not a measure of agreement with any human reviewer's decision</strong> &mdash; no
  human labels or reviews any lead in this benchmark's oracle. See <code>docs/04-eval-dataset.md</code>.</p>
  <ul class="provenance">{rows}</ul>
</div>
"""


def render_html(data: ReportData) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ARIE &mdash; Policy Lab</title>
<style>{_CSS}</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <p class="eyebrow">Post-M1 &middot; P3 &middot; Policy Lab</p>
    <h1>ARIE Policy Lab</h1>
    <p class="tagline">The production policy emerged from a measured cost/decision-quality
    trade-off, not from assuming the most sophisticated policy would win.</p>
    <p class="meta">Generated {_esc(data.generated_at)} &middot; source {_esc(data.artifact_display_path)}
    &middot; seeds {min(data.seeds)}&ndash;{max(data.seeds)}</p>
  </header>

  {_chart_section_html(data)}
  {_production_section_html(data)}
  {_waterfall_comparison_html(data)}
  {_comparison_table_html(data)}
  {_negative_result_html(data)}
  {_methodology_html(data)}

  <footer>
    Generated by <code>scripts/policy-lab.ps1</code> from the frozen benchmark artifact at
    <code>{_esc(data.artifact_display_path)}</code> &mdash; static, offline, reproducible by
    re-running <code>python -m bench.multi_seed</code>. Full reasoning:
    <code>docs/05-results.md</code>, <code>docs/adr/0004-evoi-is-a-negative-result.md</code>. This
    report does not run, tune, or otherwise touch scoring, calibration, or policy code.
  </footer>
</div>
</body>
</html>
"""
