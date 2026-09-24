"""No writer in this repository may rely on the `production` default.

`leads.data_class` defaults to `production` because that is right for a real
customer's ingest. It is wrong for everything in `tests/` and `scripts/`, and
relying on it is how one organization's dashboard, review queue and spend came
to be computed over 193 fixtures and one real lead.

This test reads the repository's own source and fails if a lead-ingest call
site does not say what kind of data it is creating. It is a lint, not a
behavioural test: a new canary script that forgets the header is caught here
rather than in a customer's numbers three weeks later.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SEARCHED = ("tests", "scripts")

_INGEST_CALL = re.compile(r"""post\(\s*\n?\s*["']/leads["']""")
_MARKER = re.compile(r"X-ARIE-Data-Class|HARNESS_HEADERS|data_class")

_EXEMPT: dict[str, str] = {
    "tests/integration/test_data_class_quarantine_integration.py": (
        "the module that proves the header's own rules -- its `_ingest` helper "
        "deliberately posts with and without a marker"
    ),
    "tests/unit/test_harness_hygiene.py": "this scanner's own source",
}


def _python_files() -> list[Path]:
    files: list[Path] = []
    for directory in SEARCHED:
        files.extend(sorted((REPO / directory).rglob("*.py")))
    return files


def _relative(path: Path) -> str:
    return path.relative_to(REPO).as_posix()


def _ingest_sites() -> list[tuple[str, int, str]]:
    """Every `post("/leads")` in the repository's own test and script code."""
    sites: list[tuple[str, int, str]] = []
    for path in _python_files():
        text = path.read_text(encoding="utf-8")
        if "/leads" not in text:
            continue
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if _INGEST_CALL.search(line):
                # The marker may sit on the same line or on the next two, since
                # a long call is wrapped.
                window = "\n".join(lines[index : index + 3])
                sites.append((_relative(path), index + 1, window))
    return sites


def test_there_are_ingest_sites_to_check() -> None:
    """A scanner that finds nothing passes vacuously, which is worse than
    failing — this pins the search itself."""
    assert len(_ingest_sites()) >= 20


def test_every_harness_ingest_site_declares_its_data_class() -> None:
    unmarked = [
        f"{path}:{line}"
        for path, line, window in _ingest_sites()
        if path not in _EXEMPT and not _MARKER.search(window)
    ]

    assert not unmarked, (
        "these lead-ingest call sites would create `production` data by default:\n  "
        + "\n  ".join(unmarked)
        + "\n\nSend `headers=HARNESS_HEADERS` (tests) or "
        '`headers={"X-ARIE-Data-Class": "canary"}` (scripts). A test that '
        "genuinely needs the production default belongs in this module's "
        "_EXEMPT map, with a reason."
    )


def test_direct_inserts_are_covered_structurally() -> None:
    """A test that INSERTs into `leads` cannot send a header.

    Rather than make ~18 seed helpers each name the column and hope the
    nineteenth remembers, `tests/integration/conftest.harness_data_class_default`
    flips the *test* database's own column default to `integration_test`. On
    that database, forgetting produces a harness row; naming `production`
    explicitly is what a test does when its fixture deliberately stands in for
    a customer's lead, and several now do exactly that.

    This pins the fixture's existence, so deleting it fails here rather than
    quietly returning the suite to seeding production data.
    """
    conftest = (REPO / "tests/integration/conftest.py").read_text(encoding="utf-8")

    assert "def harness_data_class_default(" in conftest
    assert "ALTER TABLE leads ALTER COLUMN data_class SET DEFAULT" in conftest
    assert 'HARNESS_DATA_CLASS = "integration_test"' in conftest


@pytest.mark.parametrize("path", sorted(_EXEMPT))
def test_every_exemption_still_points_at_a_real_file(path: str) -> None:
    """An exemption for a file that no longer exists is a rule nobody is
    following any more."""
    assert (REPO / path).exists(), f"{path} is exempted but does not exist"
