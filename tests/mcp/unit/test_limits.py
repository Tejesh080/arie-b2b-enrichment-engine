from __future__ import annotations

from arie_mcp.limits import MAX_ROWS, cap_limit, truncate_str


def test_cap_limit_defaults_when_not_requested() -> None:
    assert cap_limit(None) == 20


def test_cap_limit_defaults_when_non_positive() -> None:
    assert cap_limit(0) == 20
    assert cap_limit(-5) == 20


def test_cap_limit_passes_through_within_range() -> None:
    assert cap_limit(50) == 50


def test_cap_limit_caps_at_maximum_regardless_of_request() -> None:
    assert cap_limit(500) == MAX_ROWS
    assert cap_limit(10_000, maximum=10) == 10


def test_truncate_str_leaves_short_strings_untouched() -> None:
    assert truncate_str("short") == "short"


def test_truncate_str_caps_long_strings() -> None:
    long_value = "x" * 1000
    result = truncate_str(long_value, max_len=500)
    assert result is not None
    assert len(result) == 500


def test_truncate_str_passes_through_none() -> None:
    assert truncate_str(None) is None
