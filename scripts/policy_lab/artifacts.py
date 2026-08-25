"""Locates and parses the frozen `bench.multi_seed` benchmark artifact.

`bench/out/` is gitignored (see `.gitignore`) — it is regenerated locally by
`python -m bench.multi_seed`, never committed. On a fresh clone the file
genuinely does not exist yet; that is a normal state this module reports
clearly, not a bug to paper over.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ARTIFACT_PATH = REPO_ROOT / "bench" / "out" / "multi_seed.json"
DEFAULT_MANIFEST_PATH = REPO_ROOT / "data" / "eval" / "manifest.json"

_REQUIRED_TOP_LEVEL_KEYS = ("seeds", "per_seed", "stability")


class ArtifactError(RuntimeError):
    """The frozen benchmark artifact is missing, unreadable, or malformed.

    The message always names the exact command that produces it — this is a
    "go run the benchmark" state, not a code defect.
    """


def _regenerate_hint() -> str:
    return (
        "Run:\n  python -m bench.multi_seed\n"
        "(~15 minutes, offline, no API keys — reproduces the numbers in "
        "docs/benchmark.md) or pass -Regenerate to scripts\\policy-lab.ps1 "
        "to do this automatically."
    )


def load_artifact(path: Path = DEFAULT_ARTIFACT_PATH) -> dict[str, Any]:
    """Load and structurally validate the multi-seed benchmark artifact.

    Raises `ArtifactError` for anything that would otherwise fail confusingly
    downstream: missing file, invalid JSON, or a JSON document that doesn't
    look like `bench.multi_seed`'s output shape.
    """
    if not path.exists():
        raise ArtifactError(f"Frozen benchmark artifact not found: {path}\n\n{_regenerate_hint()}")
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ArtifactError(f"Could not read {path}: {exc}") from exc

    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ArtifactError(
            f"{path} is not valid JSON ({exc}). It may be a partial write from an "
            f"interrupted benchmark run.\n\n{_regenerate_hint()}"
        ) from exc

    if not isinstance(data, dict):
        raise ArtifactError(f"{path} does not contain a JSON object at its root.")

    missing = [key for key in _REQUIRED_TOP_LEVEL_KEYS if key not in data]
    if missing:
        raise ArtifactError(
            f"{path} is missing expected key(s) {missing} — this doesn't look like "
            f"bench.multi_seed's output.\n\n{_regenerate_hint()}"
        )
    if not isinstance(data["per_seed"], list) or not data["per_seed"]:
        raise ArtifactError(f"{path} has an empty or malformed 'per_seed' list.")
    if not isinstance(data["seeds"], list) or not data["seeds"]:
        raise ArtifactError(f"{path} has an empty or malformed 'seeds' list.")

    return data


def load_dataset_manifest(path: Path = DEFAULT_MANIFEST_PATH) -> dict[str, Any] | None:
    """Best-effort read of the tracked dataset manifest for provenance display.

    Returns `None` rather than raising — the manifest is provenance context,
    not something the report should refuse to generate without. It reflects
    seed 42's dataset only; every seed in the sweep shares the same generator
    and rules version, just a different RNG seed.
    """
    if not path.exists():
        return None
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def relative_to_repo(path: Path) -> str:
    """Repo-relative display path — never leak an absolute machine path into
    a generated artifact meant to be shared."""
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name
