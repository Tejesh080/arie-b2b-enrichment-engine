"""Organization usage limits (Productization M4 Part 9) — the one pure
function in `arie.limits`. Everything else needs a live database (real
calendar-month usage totals via `arie.usage.get_usage_summary`) and is
covered by tests/integration/test_limits_integration.py instead.
"""

from __future__ import annotations

from datetime import UTC, datetime

from arie.limits import _calendar_month_bounds


def test_calendar_month_bounds_mid_month() -> None:
    start, end = _calendar_month_bounds(datetime(2026, 8, 31, 14, 30, 0, tzinfo=UTC))
    assert start == datetime(2026, 8, 1, tzinfo=UTC)
    assert end == datetime(2026, 9, 1, tzinfo=UTC)


def test_calendar_month_bounds_december_wraps_to_next_year() -> None:
    start, end = _calendar_month_bounds(datetime(2026, 12, 15, tzinfo=UTC))
    assert start == datetime(2026, 12, 1, tzinfo=UTC)
    assert end == datetime(2027, 1, 1, tzinfo=UTC)


def test_calendar_month_bounds_first_of_month() -> None:
    start, end = _calendar_month_bounds(datetime(2026, 3, 1, 0, 0, 1, tzinfo=UTC))
    assert start == datetime(2026, 3, 1, tzinfo=UTC)
    assert end == datetime(2026, 4, 1, tzinfo=UTC)
