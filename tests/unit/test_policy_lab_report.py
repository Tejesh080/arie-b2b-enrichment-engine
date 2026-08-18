"""`scripts.policy_lab.report` — end-to-end build_report -> render_html /
render_json over a deterministic fixture artifact. Covers HTML escaping,
expected policy labels, provenance presence, and the scientifically
prohibited phrasing this report must never produce."""

from __future__ import annotations

import json
from typing import Any

import pytest
from scripts.policy_lab.report import build_report, render_html, render_json

# Values chosen close to the real bench/out/multi_seed.json headline so the
# rendered narrative reads the same way the real report does, without
# depending on the (gitignored, locally-generated) real artifact.
_POLICY_VALUES: dict[str, dict[str, tuple[float, float]]] = {
    "full_enrichment": {
        "cost": (0.41, 0.48),
        "agreement": (0.82, 0.86),
        "calls": (8.0, 8.0),
        "autonomy": (0.80, 0.83),
    },
    "waterfall_expensive": {
        "cost": (0.39, 0.45),
        "agreement": (0.81, 0.86),
        "calls": (7.5, 7.7),
        "autonomy": (0.78, 0.81),
    },
    "calibrated_bounds": {
        "cost": (0.22, 0.27),
        "agreement": (0.79, 0.83),
        "calls": (5.0, 5.5),
        "autonomy": (0.81, 0.86),
    },
    "adaptive_voi_x1": {
        "cost": (0.28, 0.30),
        "agreement": (0.79, 0.83),
        "calls": (2.1, 2.3),
        "autonomy": (0.77, 0.80),
    },
}


def _artifact() -> dict[str, Any]:
    per_seed = []
    for i, seed in enumerate((42, 43)):
        policies = [
            {
                "policy": name,
                "mean_cost_usd": values["cost"][i],
                "decision_agreement": values["agreement"][i],
                "mean_calls": values["calls"][i],
                "autonomous_rate": values["autonomy"][i],
            }
            for name, values in _POLICY_VALUES.items()
        ]
        per_seed.append({"seed": seed, "policies": policies, "confidence_ece": 0.05 + i * 0.01})
    return {"seeds": [42, 43], "per_seed": per_seed, "stability": []}


def _manifest() -> dict[str, Any]:
    return {
        "generator_version": "1.0.0",
        "rules_version": "icp-1.0.0",
        "content_sha256": "deadbeef",
    }


def _report(**overrides: Any) -> Any:
    defaults: dict[str, Any] = dict(
        artifact=_artifact(),
        manifest=_manifest(),
        artifact_display_path="bench/out/multi_seed.json",
        generated_at="2026-08-18T00:00:00+00:00",
    )
    defaults.update(overrides)
    return build_report(
        defaults["artifact"],
        defaults["manifest"],
        artifact_display_path=defaults["artifact_display_path"],
        generated_at=defaults["generated_at"],
    )


# --------------------------------------------------------------- structure --


def test_build_report_frontier_matches_pareto_computation() -> None:
    report = _report()
    # calibrated_bounds (cheap, decent agreement) should dominate
    # adaptive_voi_x1 (pricier, similar-or-worse agreement) on these fixture
    # numbers, same shape as the real headline result.
    assert "adaptive_voi_x1" in report.frontier.dominated_by
    assert report.frontier.dominated_by["adaptive_voi_x1"] is not None


def test_json_report_round_trips() -> None:
    body = render_json(_report())
    parsed = json.loads(body)
    assert parsed["production_policy"] == "calibrated_bounds"
    assert parsed["baseline_policy"] == "waterfall_expensive"
    assert set(parsed["policies"]) == set(_POLICY_VALUES)


def test_json_report_includes_provenance() -> None:
    parsed = json.loads(render_json(_report()))
    prov = parsed["provenance"]
    assert prov["seeds"] == [42, 43]
    assert prov["artifact_source"] == "bench/out/multi_seed.json"
    assert prov["dataset_generator_version"] == "1.0.0"
    assert prov["preregistered_win_criteria_met"] is False


# -------------------------------------------------------------------- HTML --


def test_html_report_contains_expected_policy_display_names() -> None:
    body = render_html(_report())
    for label in ("Full enrichment", "Tuned waterfall", "Calibrated Bounds", "Adaptive EVoI"):
        assert label in body


def test_html_report_labels_production_as_production_not_best() -> None:
    body = render_html(_report())
    assert "Production policy" in body
    assert "Best policy" not in body
    assert "best policy" not in body.lower()


def test_html_report_includes_provenance_section() -> None:
    body = render_html(_report())
    assert "bench/out/multi_seed.json" in body
    assert "42" in body and "43" in body
    assert "Pre-registered win criteria" in body
    assert "not met by any evaluated policy" in body


def test_html_report_escapes_a_malicious_artifact_path() -> None:
    report = _report(artifact_display_path="<script>alert(1)</script>")
    body = render_html(report)
    assert "<script>alert(1)</script>" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body


def test_html_report_escapes_a_malicious_generated_at() -> None:
    report = _report(generated_at='"><img src=x onerror=alert(1)>')
    body = render_html(report)
    assert "<img src=x onerror=alert(1)>" not in body


def test_html_report_never_leaks_an_absolute_windows_path() -> None:
    report = _report(artifact_display_path="bench/out/multi_seed.json")
    body = render_html(report)
    assert "C:\\" not in body
    assert "D:\\" not in body


def test_html_report_pairs_the_cost_claim_with_the_agreement_decrease() -> None:
    """Regression guard for the exact failure mode the brief calls out: never
    state the cost saving without the agreement change in the same section.
    Whitespace is normalized first since the source template line-wraps mid
    sentence -- HTML collapses that; a literal substring check shouldn't care."""
    body = render_html(_report())
    section = body.split("Calibrated Bounds vs. the tuned waterfall")[1].split("Policy comparison")[
        0
    ]
    normalized = " ".join(section.split())
    assert "reduction in synthetic-oracle agreement" in normalized
    assert "reduced mean API spend" in normalized


# ------------------------------------------------------- prohibited wording --

_PROHIBITED_PHRASES = (
    "41.6% cheaper with the same quality",
    "no accuracy loss",
    "highest quality",
    "human-level",
    "human judgment",
    "evoi won",
    "best policy",
    "winner",
)


@pytest.mark.parametrize("phrase", _PROHIBITED_PHRASES)
def test_html_report_never_contains_prohibited_phrasing(phrase: str) -> None:
    body = render_html(_report()).lower()
    assert phrase.lower() not in body


def test_html_report_never_calls_the_frontier_pareto_optimal() -> None:
    body = render_html(_report())
    assert "pareto optimal" not in body.lower()
    assert "pareto-optimal" not in body.lower()


def test_html_report_never_claims_savings_without_naming_the_baseline() -> None:
    body = render_html(_report())
    # Every "cheaper" claim in the waterfall-comparison section must name
    # "waterfall" nearby -- spot-check the one sentence that makes the claim.
    idx = body.find("Calibrated Bounds reduced mean API spend")
    assert idx != -1
    window = body[idx : idx + 200]
    assert "waterfall" in window.lower()
