"""Renders the Pareto chart as self-contained inline SVG.

No charting library, no CDN. Layout (label placement, callout box side) is
computed from the actual pixel positions of the points being drawn, not
hardcoded per policy name — so it stays correct if the underlying numbers
move on a benchmark re-run.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass

from scripts.policy_lab.pareto import FrontierResult, ParetoPoint
from scripts.policy_lab.stats import DISPLAY_NAMES, PolicyStats

_WIDTH = 820
_HEIGHT = 520
_MARGIN_LEFT = 82
_MARGIN_RIGHT = 34
_MARGIN_TOP = 28
_MARGIN_BOTTOM = 64
_PLOT_W = _WIDTH - _MARGIN_LEFT - _MARGIN_RIGHT
_PLOT_H = _HEIGHT - _MARGIN_TOP - _MARGIN_BOTTOM

_PALETTE: dict[str, str] = {
    "full_enrichment": "#8b93a7",
    "waterfall_expensive": "#d9a441",
    "calibrated_bounds": "#5b8cff",
    "adaptive_voi_x1": "#9c8cff",
}
_DEFAULT_COLOR = "#8b93a7"
_DOMINATED_COLOR = "#6b7280"
_TEXT_DIM = "#9aa3b8"
_GRID_COLOR = "#262d3d"
_TEXT = "#e8ecf5"


@dataclass(frozen=True)
class ChartPoint:
    policy: str
    display_name: str
    cost_mean: float
    cost_min: float
    cost_max: float
    agreement_mean: float
    agreement_min: float
    agreement_max: float
    is_frontier: bool
    is_production: bool
    dominated_by: str | None
    """Display name of a dominating policy, or None if on the frontier."""


def build_chart_points(
    stats_by_policy: dict[str, PolicyStats], frontier: FrontierResult, production_policy: str
) -> list[ChartPoint]:
    points = []
    for policy, stats in stats_by_policy.items():
        dominator = frontier.dominated_by[policy]
        points.append(
            ChartPoint(
                policy=policy,
                display_name=DISPLAY_NAMES.get(policy, policy),
                cost_mean=stats.cost_usd.mean,
                cost_min=stats.cost_usd.minimum,
                cost_max=stats.cost_usd.maximum,
                agreement_mean=stats.agreement.mean,
                agreement_min=stats.agreement.minimum,
                agreement_max=stats.agreement.maximum,
                is_frontier=policy in frontier.frontier,
                is_production=(policy == production_policy),
                dominated_by=DISPLAY_NAMES.get(dominator, dominator) if dominator else None,
            )
        )
    return points


def to_pareto_points(stats_by_policy: dict[str, PolicyStats]) -> list[ParetoPoint]:
    return [
        ParetoPoint(policy=p, cost=s.cost_usd.mean, agreement=s.agreement.mean)
        for p, s in stats_by_policy.items()
    ]


def _round_down(value: float, step: float) -> float:
    return math.floor(value / step) * step


def _round_up(value: float, step: float) -> float:
    return math.ceil(value / step) * step


def _linspace(lo: float, hi: float, n: int) -> list[float]:
    if n <= 1:
        return [lo]
    step = (hi - lo) / (n - 1)
    return [lo + i * step for i in range(n)]


def _esc(value: object) -> str:
    return html.escape(str(value))


def render_pareto_svg(points: list[ChartPoint]) -> str:
    cost_hi = max(p.cost_max for p in points)
    agreement_lo = min(p.agreement_min for p in points)
    agreement_hi = max(p.agreement_max for p in points)

    x_lo, x_hi = 0.0, _round_up(cost_hi * 1.08, 0.05)
    y_lo, y_hi = _round_down(agreement_lo, 0.05), _round_up(agreement_hi, 0.05)
    if y_hi - y_lo < 0.05:
        y_hi += 0.05

    def x_pos(cost: float) -> float:
        return _MARGIN_LEFT + (cost - x_lo) / (x_hi - x_lo) * _PLOT_W

    def y_pos(agreement: float) -> float:
        return _MARGIN_TOP + (1 - (agreement - y_lo) / (y_hi - y_lo)) * _PLOT_H

    svg: list[str] = []
    svg.append(
        f'<svg viewBox="0 0 {_WIDTH} {_HEIGHT}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-labelledby="pareto-title pareto-desc" class="pareto-svg">'
    )
    svg.append('<title id="pareto-title">API cost per lead vs. synthetic-oracle agreement</title>')
    dominated_note = "; ".join(
        f"{p.display_name} is dominated by {p.dominated_by}" for p in points if p.dominated_by
    )
    desc = (
        f"Pareto chart comparing {len(points)} policies on mean API cost per lead (x axis, "
        f"lower is cheaper) against synthetic-oracle decision agreement (y axis, higher agrees "
        f"more with the oracle). Whiskers show the min-max range across seeds 42 to 51. "
        f"{dominated_note or 'All plotted policies are Pareto-efficient.'} "
        f"Exact values are in the table below the chart."
    )
    svg.append(f'<desc id="pareto-desc">{_esc(desc)}</desc>')

    # Plot background + gridlines.
    svg.append(
        f'<rect x="{_MARGIN_LEFT}" y="{_MARGIN_TOP}" width="{_PLOT_W}" height="{_PLOT_H}" '
        f'fill="none" stroke="{_GRID_COLOR}" stroke-width="1"/>'
    )
    x_ticks = _linspace(x_lo, x_hi, 6)
    for tick in x_ticks:
        gx = x_pos(tick)
        svg.append(
            f'<line x1="{gx:.1f}" y1="{_MARGIN_TOP}" x2="{gx:.1f}" '
            f'y2="{_MARGIN_TOP + _PLOT_H}" stroke="{_GRID_COLOR}" stroke-width="1" opacity="0.6"/>'
        )
        svg.append(
            f'<text x="{gx:.1f}" y="{_MARGIN_TOP + _PLOT_H + 20}" fill="{_TEXT_DIM}" '
            f'font-size="12" text-anchor="middle">${tick:.2f}</text>'
        )
    y_ticks = _linspace(y_lo, y_hi, 5)
    for tick in y_ticks:
        gy = y_pos(tick)
        svg.append(
            f'<line x1="{_MARGIN_LEFT}" y1="{gy:.1f}" x2="{_MARGIN_LEFT + _PLOT_W}" '
            f'y2="{gy:.1f}" stroke="{_GRID_COLOR}" stroke-width="1" opacity="0.6"/>'
        )
        svg.append(
            f'<text x="{_MARGIN_LEFT - 10}" y="{gy + 4:.1f}" fill="{_TEXT_DIM}" '
            f'font-size="12" text-anchor="end">{tick * 100:.0f}%</text>'
        )

    # Axis titles.
    svg.append(
        f'<text x="{_MARGIN_LEFT + _PLOT_W / 2:.1f}" y="{_HEIGHT - 10}" fill="{_TEXT_DIM}" '
        f'font-size="13" text-anchor="middle">Mean API cost per lead (USD)</text>'
    )
    svg.append(
        f'<text x="16" y="{_MARGIN_TOP + _PLOT_H / 2:.1f}" fill="{_TEXT_DIM}" font-size="13" '
        f'text-anchor="middle" transform="rotate(-90 16 {_MARGIN_TOP + _PLOT_H / 2:.1f})">'
        f"Synthetic-oracle agreement</text>"
    )
    svg.append(
        f'<text x="{_MARGIN_LEFT + _PLOT_W:.1f}" y="{_MARGIN_TOP - 10}" fill="{_TEXT_DIM}" '
        f'font-size="11" text-anchor="end" font-style="italic">'
        f"axes zoomed to the data range, not from zero — see table for exact values</text>"
    )

    # Frontier connector: a light step-line through frontier points in cost order.
    frontier_pts = sorted((p for p in points if p.is_frontier), key=lambda p: p.cost_mean)
    if len(frontier_pts) > 1:
        path_d = " ".join(
            f"{'M' if i == 0 else 'L'}{x_pos(p.cost_mean):.1f},{y_pos(p.agreement_mean):.1f}"
            for i, p in enumerate(frontier_pts)
        )
        svg.append(
            f'<path d="{path_d}" fill="none" stroke="{_TEXT_DIM}" stroke-width="1.5" '
            f'stroke-dasharray="3 4" opacity="0.55"/>'
        )

    # Sort by x for deterministic collision-avoidance in label placement.
    ordered = sorted(points, key=lambda p: p.cost_mean)

    # Bounding-box collision avoidance shared by point labels and the
    # production callout: each already-placed rect blocks later ones from
    # landing on top of it. Markers are seeded in first so labels steer
    # clear of *every* point, not just other labels.
    placed_rects: list[tuple[float, float, float, float]] = []
    for point in ordered:
        mx, my = x_pos(point.cost_mean), y_pos(point.agreement_mean)
        placed_rects.append((mx - 10, my - 10, mx + 10, my + 10))

    def rects_overlap(
        a: tuple[float, float, float, float], b: tuple[float, float, float, float]
    ) -> bool:
        pad = 5.0
        return not (
            a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1]
        )

    def place_block(px: float, py: float, block_w: float, block_h: float) -> tuple[float, float]:
        """Returns the (center_x, top_y) of the first non-overlapping
        candidate position around (px, py), trying progressively further
        offsets before giving up and using the last candidate anyway."""
        candidates = [
            (0.0, -block_h - 8),
            (0.0, 16.0),
            (block_w / 2 + 14, -block_h / 2),
            (-(block_w / 2 + 14), -block_h / 2),
            (block_w / 2 + 14, 16.0),
            (-(block_w / 2 + 14), 16.0),
            (0.0, -block_h - 40),
            (0.0, 48.0),
            (block_w / 2 + 14, -block_h - 8),
            (-(block_w / 2 + 14), -block_h - 8),
        ]
        chosen = candidates[-1]
        for dx, dy in candidates:
            cx, top = px + dx, py + dy
            rect = (cx - block_w / 2, top, cx + block_w / 2, top + block_h)
            if not any(rects_overlap(rect, other) for other in placed_rects):
                chosen = (dx, dy)
                break
        cx, top = px + chosen[0], py + chosen[1]
        placed_rects.append((cx - block_w / 2, top, cx + block_w / 2, top + block_h))
        return cx, top

    for point in ordered:
        px, py = x_pos(point.cost_mean), y_pos(point.agreement_mean)
        color = (
            _PALETTE.get(point.policy, _DEFAULT_COLOR) if point.is_frontier else _DOMINATED_COLOR
        )

        # Whiskers: horizontal (cost range) and vertical (agreement range).
        x0, x1 = x_pos(point.cost_min), x_pos(point.cost_max)
        y0, y1 = y_pos(point.agreement_max), y_pos(point.agreement_min)
        svg.append(
            f'<line x1="{x0:.1f}" y1="{py:.1f}" x2="{x1:.1f}" y2="{py:.1f}" '
            f'stroke="{color}" stroke-width="1.5" opacity="0.45"/>'
        )
        svg.append(
            f'<line x1="{px:.1f}" y1="{y0:.1f}" x2="{px:.1f}" y2="{y1:.1f}" '
            f'stroke="{color}" stroke-width="1.5" opacity="0.45"/>'
        )
        for cap_x0, cap_x1, cap_y0, cap_y1 in (
            (x0, x0, py - 4, py + 4),
            (x1, x1, py - 4, py + 4),
            (px - 4, px + 4, y0, y0),
            (px - 4, px + 4, y1, y1),
        ):
            svg.append(
                f'<line x1="{cap_x0:.1f}" y1="{cap_y0:.1f}" x2="{cap_x1:.1f}" y2="{cap_y1:.1f}" '
                f'stroke="{color}" stroke-width="1.5" opacity="0.45"/>'
            )

        if point.is_production:
            svg.append(
                f'<rect x="{px - 10:.1f}" y="{py - 10:.1f}" width="20" height="20" '
                f'transform="rotate(45 {px:.1f} {py:.1f})" fill="none" stroke="{color}" '
                f'stroke-width="2" opacity="0.9"/>'
            )

        if point.is_frontier:
            svg.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7.5" fill="{color}" stroke="{_TEXT}" stroke-width="1.5"/>'
            )
        else:
            svg.append(
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="7" fill="#141822" stroke="{color}" '
                f'stroke-width="2.5" stroke-dasharray="3 3"/>'
            )

        # Label block: name, then value, then an optional "dominated by" note
        # — sized to content and placed by the shared collision avoider.
        n_lines = 3 if point.dominated_by else 2
        block_w, block_h = 152.0, 14.0 * n_lines
        cx, top = place_block(px, py, block_w, block_h)
        anchor = "middle"
        svg.append(
            f'<text x="{cx:.1f}" y="{top + 11:.1f}" fill="{_TEXT}" font-size="12.5" '
            f'font-weight="600" text-anchor="{anchor}">{_esc(point.display_name)}</text>'
        )
        svg.append(
            f'<text x="{cx:.1f}" y="{top + 25:.1f}" fill="{_TEXT_DIM}" font-size="10.5" '
            f'text-anchor="{anchor}">${point.cost_mean:.3f} · {point.agreement_mean * 100:.1f}%</text>'
        )
        if point.dominated_by:
            svg.append(
                f'<text x="{cx:.1f}" y="{top + 39:.1f}" fill="{_DOMINATED_COLOR}" font-size="10" '
                f'font-style="italic" text-anchor="{anchor}">dominated by {_esc(point.dominated_by)}</text>'
            )

    # Production callout — placed by the same collision avoider, so it steers
    # clear of every marker and every label already on the chart.
    production = next((p for p in points if p.is_production), None)
    if production is not None:
        px, py = x_pos(production.cost_mean), y_pos(production.agreement_mean)
        box_w, box_h = 176.0, 40.0
        box_cx, box_top = place_block(px, py, box_w, box_h)
        box_x = max(
            _MARGIN_LEFT + 2.0, min(box_cx - box_w / 2, _MARGIN_LEFT + _PLOT_W - box_w - 2.0)
        )
        box_y = max(_MARGIN_TOP + 2.0, min(box_top, _MARGIN_TOP + _PLOT_H - box_h - 2.0))
        # Leader line to the box edge nearest the point.
        anchor_x = box_x if px < box_cx else box_x + box_w
        anchor_y = max(box_y + 6, min(py, box_y + box_h - 6))
        svg.append(
            f'<line x1="{px:.1f}" y1="{py:.1f}" x2="{anchor_x:.1f}" y2="{anchor_y:.1f}" '
            f'stroke="{_PALETTE["calibrated_bounds"]}" stroke-width="1" opacity="0.7"/>'
        )
        svg.append(
            f'<rect x="{box_x:.1f}" y="{box_y:.1f}" width="{box_w}" height="{box_h}" rx="6" '
            f'fill="#1b2130" stroke="{_PALETTE["calibrated_bounds"]}" stroke-width="1.5"/>'
        )
        svg.append(
            f'<text x="{box_x + box_w / 2:.1f}" y="{box_y + 17:.1f}" fill="{_TEXT_DIM}" '
            f'font-size="10.5" text-anchor="middle">Production selection</text>'
        )
        svg.append(
            f'<text x="{box_x + box_w / 2:.1f}" y="{box_y + 32:.1f}" fill="{_TEXT}" '
            f'font-size="12.5" font-weight="700" text-anchor="middle">Calibrated Bounds</text>'
        )

    svg.append("</svg>")
    return "".join(svg)
