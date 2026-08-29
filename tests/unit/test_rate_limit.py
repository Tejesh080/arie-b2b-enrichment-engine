"""``MinIntervalPacer`` — deterministic, mocked-clock timing tests.

No real ``time.sleep`` anywhere in this file: a fake clock and a recording
sleep function make every assertion exact instead of racy.
"""

from __future__ import annotations

from arie.live.rate_limit import MinIntervalPacer


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.slept: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def _pacer(interval: float, clock: _FakeClock) -> MinIntervalPacer:
    return MinIntervalPacer(
        min_interval_seconds=interval, sleep=clock.sleep, monotonic=clock.monotonic
    )


def test_the_first_call_never_waits() -> None:
    clock = _FakeClock()
    pacer = _pacer(1.0, clock)
    pacer.wait()
    assert clock.slept == []


def test_a_call_before_the_interval_elapsed_waits_the_remainder() -> None:
    clock = _FakeClock()
    pacer = _pacer(1.0, clock)
    pacer.wait()
    clock.now += 0.4  # 400ms of real work happened between calls
    pacer.wait()
    assert clock.slept == [0.6]


def test_a_call_after_the_interval_already_elapsed_never_waits() -> None:
    clock = _FakeClock()
    pacer = _pacer(1.0, clock)
    pacer.wait()
    clock.now += 5.0
    pacer.wait()
    assert clock.slept == []


def test_zero_interval_never_waits() -> None:
    clock = _FakeClock()
    pacer = _pacer(0.0, clock)
    pacer.wait()
    pacer.wait()
    pacer.wait()
    assert clock.slept == []


def test_five_calls_back_to_back_are_spaced_by_the_interval() -> None:
    """The exact 2026-08-29 experiment scenario: five sequential Abstract
    calls with no pacing hit a real rate_limited on the fifth. Five paced
    calls must land at least one interval apart, deterministically."""
    clock = _FakeClock()
    pacer = _pacer(1.0, clock)
    for _ in range(5):
        pacer.wait()
    assert sum(clock.slept) == 4.0  # first call free, four subsequent waits of 1s each
    assert clock.now == 4.0


def test_note_retry_after_delays_the_next_call_even_with_zero_interval() -> None:
    clock = _FakeClock()
    pacer = _pacer(0.0, clock)
    pacer.wait()
    pacer.note_retry_after(12.0)
    pacer.wait()
    assert clock.slept == [12.0]


def test_note_retry_after_never_shortens_a_later_deadline() -> None:
    clock = _FakeClock()
    pacer = _pacer(0.0, clock)
    pacer.note_retry_after(10.0)
    pacer.note_retry_after(2.0)  # a shorter, later-arriving hint must not win
    pacer.wait()
    assert clock.slept == [10.0]


def test_retry_after_is_a_floor_not_an_addition_to_the_ordinary_pace() -> None:
    """A Retry-After deadline that has already passed must not add extra
    delay on top of the ordinary interval."""
    clock = _FakeClock()
    pacer = _pacer(1.0, clock)
    pacer.wait()
    pacer.note_retry_after(0.1)
    clock.now += 5.0  # well past both the interval and the retry-after
    pacer.wait()
    assert clock.slept == []


def test_pacing_is_per_instance_not_shared() -> None:
    clock = _FakeClock()
    a = _pacer(1.0, clock)
    b = _pacer(1.0, clock)
    a.wait()
    b.wait()  # a different provider's pacer must not see a's last call
    assert clock.slept == []
