"""`scripts.policy_lab.chart` — SVG rendering. Checks structure, escaping,
and the production/dominated visual markers exist as elements the report's
legend actually promises, not just as color."""

from __future__ import annotations

from scripts.policy_lab.chart import ChartPoint, render_pareto_svg


def _point(
    policy: str,
    *,
    cost: float,
    agreement: float,
    is_frontier: bool = True,
    is_production: bool = False,
    dominated_by: str | None = None,
) -> ChartPoint:
    return ChartPoint(
        policy=policy,
        display_name=policy,
        cost_mean=cost,
        cost_min=cost - 0.02,
        cost_max=cost + 0.02,
        agreement_mean=agreement,
        agreement_min=agreement - 0.02,
        agreement_max=agreement + 0.02,
        is_frontier=is_frontier,
        is_production=is_production,
        dominated_by=dominated_by,
    )


def test_svg_is_well_formed_enough_to_parse() -> None:
    import xml.etree.ElementTree as ET

    svg = render_pareto_svg([_point("a", cost=0.2, agreement=0.8, is_production=True)])
    root = ET.fromstring(svg)
    assert root.tag.endswith("svg")


def test_svg_includes_accessible_title_and_desc() -> None:
    svg = render_pareto_svg([_point("a", cost=0.2, agreement=0.8)])
    assert "<title" in svg
    assert "<desc" in svg
    assert 'role="img"' in svg


def test_svg_desc_names_the_dominated_relationship() -> None:
    svg = render_pareto_svg(
        [
            _point("calibrated_bounds", cost=0.25, agreement=0.81, is_production=True),
            _point(
                "adaptive_voi_x1",
                cost=0.29,
                agreement=0.81,
                is_frontier=False,
                dominated_by="calibrated_bounds",
            ),
        ]
    )
    assert "dominated by calibrated_bounds" in svg


def test_svg_escapes_a_malicious_display_name() -> None:
    point = ChartPoint(
        policy="x",
        display_name="<script>alert(1)</script>",
        cost_mean=0.2,
        cost_min=0.18,
        cost_max=0.22,
        agreement_mean=0.8,
        agreement_min=0.78,
        agreement_max=0.82,
        is_frontier=True,
        is_production=False,
        dominated_by=None,
    )
    svg = render_pareto_svg([point])
    assert "<script>alert(1)</script>" not in svg
    assert "&lt;script&gt;" in svg


def test_production_point_gets_a_diamond_marker_and_callout() -> None:
    svg = render_pareto_svg(
        [_point("calibrated_bounds", cost=0.25, agreement=0.81, is_production=True)]
    )
    assert "rotate(45" in svg  # the diamond ring
    assert "Production selection" in svg


def test_dominated_point_gets_a_dashed_hollow_marker_not_just_a_color() -> None:
    svg = render_pareto_svg(
        [
            _point("calibrated_bounds", cost=0.25, agreement=0.81, is_production=True),
            _point(
                "adaptive_voi_x1",
                cost=0.29,
                agreement=0.80,
                is_frontier=False,
                dominated_by="calibrated_bounds",
            ),
        ]
    )
    assert 'stroke-dasharray="3 3"' in svg


def test_frontier_connector_drawn_when_multiple_frontier_points() -> None:
    svg = render_pareto_svg(
        [
            _point("a", cost=0.2, agreement=0.75),
            _point("b", cost=0.4, agreement=0.85),
        ]
    )
    assert 'stroke-dasharray="3 4"' in svg
