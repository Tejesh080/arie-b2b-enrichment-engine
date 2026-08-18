"""`scripts.policy_lab.cli` — end-to-end via `main()`. Confirms the default
path never touches the benchmark subprocess and fails clearly when the
artifact is missing, and that a full run against a fixture artifact produces
both output files."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts.policy_lab import cli

_ARTIFACT: dict[str, Any] = {
    "seeds": [42, 43],
    "per_seed": [
        {
            "seed": seed,
            "confidence_ece": 0.06,
            "policies": [
                {
                    "policy": "full_enrichment",
                    "mean_cost_usd": 0.44,
                    "decision_agreement": 0.84,
                    "mean_calls": 8.0,
                    "autonomous_rate": 0.81,
                },
                {
                    "policy": "waterfall_expensive",
                    "mean_cost_usd": 0.42,
                    "decision_agreement": 0.83,
                    "mean_calls": 7.6,
                    "autonomous_rate": 0.79,
                },
                {
                    "policy": "calibrated_bounds",
                    "mean_cost_usd": 0.25,
                    "decision_agreement": 0.81,
                    "mean_calls": 5.3,
                    "autonomous_rate": 0.83,
                },
                {
                    "policy": "adaptive_voi_x1",
                    "mean_cost_usd": 0.29,
                    "decision_agreement": 0.81,
                    "mean_calls": 2.2,
                    "autonomous_rate": 0.79,
                },
            ],
        }
        for seed in (42, 43)
    ],
    "stability": [],
}


def test_main_fails_clearly_when_artifact_is_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "nope" / "multi_seed.json"
    rc = cli.main(["--artifact", str(missing), "--output-dir", str(tmp_path / "out")])
    assert rc == 1
    assert "python -m bench.multi_seed" in capsys.readouterr().err


def test_main_never_invokes_the_benchmark_subprocess_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail_if_called(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("subprocess.run must not be called without --regenerate")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)
    artifact_path = tmp_path / "multi_seed.json"
    artifact_path.write_text(json.dumps(_ARTIFACT), encoding="utf-8")

    rc = cli.main(["--artifact", str(artifact_path), "--output-dir", str(tmp_path / "out")])
    assert rc == 0


def test_main_generates_html_and_json_from_a_fixture_artifact(tmp_path: Path) -> None:
    artifact_path = tmp_path / "multi_seed.json"
    artifact_path.write_text(json.dumps(_ARTIFACT), encoding="utf-8")
    output_dir = tmp_path / "out"

    rc = cli.main(["--artifact", str(artifact_path), "--output-dir", str(output_dir)])

    assert rc == 0
    html = (output_dir / "policy-lab.html").read_text(encoding="utf-8")
    payload = json.loads((output_dir / "policy-lab.json").read_text(encoding="utf-8"))
    assert "ARIE Policy Lab" in html
    assert payload["production_policy"] == "calibrated_bounds"


def test_main_regenerate_stops_if_the_benchmark_subprocess_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _FailedRun:
        returncode = 1

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _FailedRun())
    rc = cli.main(
        [
            "--regenerate",
            "--artifact",
            str(tmp_path / "multi_seed.json"),
            "--output-dir",
            str(tmp_path / "out"),
        ]
    )
    assert rc == 1
    assert not (tmp_path / "out" / "policy-lab.html").exists()
