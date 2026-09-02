"""CSV export formatting — M7 Slice 7, Part I/W. Pure functions only; the
query itself is covered live in `tests/integration/test_batch_insights_and_export_integration.py`.
"""

from __future__ import annotations

import pytest

from arie.batch_export import batch_export_filename, neutralize_formula_prefix


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("=cmd|'/c calc'!A1", "'=cmd|'/c calc'!A1"),
        ("+1+1", "'+1+1"),
        ("-1+1", "'-1+1"),
        ("@SUM(A1:A2)", "'@SUM(A1:A2)"),
        ("Acme Corp", "Acme Corp"),
        ("", ""),
        ("normal-looking-text", "normal-looking-text"),
    ],
)
def test_neutralize_formula_prefix(raw: str, expected: str) -> None:
    assert neutralize_formula_prefix(raw) == expected


def test_neutralize_only_checks_the_first_character() -> None:
    assert neutralize_formula_prefix("Company = Great") == "Company = Great"


def test_batch_export_filename_strips_extension_and_sanitizes() -> None:
    assert batch_export_filename("September Prospects.csv") == "September-Prospects-results.csv"


def test_batch_export_filename_handles_path_and_quote_injection_attempts() -> None:
    name = batch_export_filename('../../etc/passwd"; DROP TABLE.csv')
    assert "/" not in name
    assert '"' not in name
    assert ";" not in name


def test_batch_export_filename_never_empty() -> None:
    assert batch_export_filename("...csv") == "batch-results.csv"
    assert batch_export_filename("") == "batch-results.csv"
