"""`scripts.policy_lab.artifacts` — locating and validating the frozen
benchmark artifact. Uses only fixture files under a pytest tmp_path; never
touches the real (gitignored) `bench/out/multi_seed.json`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.policy_lab.artifacts import (
    ArtifactError,
    load_artifact,
    load_dataset_manifest,
    relative_to_repo,
)

_MINIMAL_ARTIFACT: dict[str, object] = {
    "seeds": [42, 43],
    "per_seed": [
        {"seed": 42, "policies": [{"policy": "calibrated_bounds", "mean_cost_usd": 0.2}]},
        {"seed": 43, "policies": [{"policy": "calibrated_bounds", "mean_cost_usd": 0.3}]},
    ],
    "stability": [],
}


def test_load_artifact_reads_a_valid_file(tmp_path: Path) -> None:
    path = tmp_path / "multi_seed.json"
    path.write_text(json.dumps(_MINIMAL_ARTIFACT), encoding="utf-8")
    data = load_artifact(path)
    assert data["seeds"] == [42, 43]


def test_load_artifact_missing_file_names_the_regenerate_command(tmp_path: Path) -> None:
    path = tmp_path / "does-not-exist.json"
    with pytest.raises(ArtifactError, match=r"python -m bench\.multi_seed"):
        load_artifact(path)


def test_load_artifact_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "multi_seed.json"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ArtifactError, match="not valid JSON"):
        load_artifact(path)


def test_load_artifact_rejects_a_json_array_at_the_root(tmp_path: Path) -> None:
    path = tmp_path / "multi_seed.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ArtifactError, match="JSON object"):
        load_artifact(path)


def test_load_artifact_rejects_missing_required_keys(tmp_path: Path) -> None:
    path = tmp_path / "multi_seed.json"
    path.write_text(json.dumps({"seeds": [42]}), encoding="utf-8")
    with pytest.raises(ArtifactError, match="missing expected key"):
        load_artifact(path)


def test_load_artifact_rejects_empty_per_seed(tmp_path: Path) -> None:
    path = tmp_path / "multi_seed.json"
    broken = {**_MINIMAL_ARTIFACT, "per_seed": []}
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ArtifactError, match="per_seed"):
        load_artifact(path)


def test_load_artifact_rejects_empty_seeds(tmp_path: Path) -> None:
    path = tmp_path / "multi_seed.json"
    broken = {**_MINIMAL_ARTIFACT, "seeds": []}
    path.write_text(json.dumps(broken), encoding="utf-8")
    with pytest.raises(ArtifactError, match="seeds"):
        load_artifact(path)


def test_load_dataset_manifest_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_dataset_manifest(tmp_path / "nope.json") is None


def test_load_dataset_manifest_returns_none_on_corrupt_json(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text("not json", encoding="utf-8")
    assert load_dataset_manifest(path) is None


def test_load_dataset_manifest_reads_a_valid_manifest(tmp_path: Path) -> None:
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"generator_version": "1.0.0"}), encoding="utf-8")
    manifest = load_dataset_manifest(path)
    assert manifest is not None
    assert manifest["generator_version"] == "1.0.0"


def test_relative_to_repo_produces_a_posix_relative_path() -> None:
    from scripts.policy_lab.artifacts import REPO_ROOT

    path = REPO_ROOT / "bench" / "out" / "multi_seed.json"
    assert relative_to_repo(path) == "bench/out/multi_seed.json"


def test_relative_to_repo_never_leaks_an_absolute_path_outside_the_repo(tmp_path: Path) -> None:
    outside = tmp_path / "somewhere-else" / "multi_seed.json"
    display = relative_to_repo(outside)
    assert str(tmp_path) not in display
    assert display == "multi_seed.json"
