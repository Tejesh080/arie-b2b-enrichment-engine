"""Exponential-with-full-jitter retry backoff — no database."""

from __future__ import annotations

import random

import pytest

from arie.jobs.backoff import backoff_cap, compute_backoff


def test_cap_grows_exponentially() -> None:
    assert backoff_cap(1, base_seconds=2.0, cap_seconds=1000.0) == 2.0
    assert backoff_cap(2, base_seconds=2.0, cap_seconds=1000.0) == 4.0
    assert backoff_cap(3, base_seconds=2.0, cap_seconds=1000.0) == 8.0
    assert backoff_cap(4, base_seconds=2.0, cap_seconds=1000.0) == 16.0


def test_cap_saturates_at_the_ceiling() -> None:
    assert backoff_cap(20, base_seconds=2.0, cap_seconds=300.0) == 300.0


def test_cap_rejects_a_non_positive_attempt() -> None:
    with pytest.raises(ValueError):
        backoff_cap(0)
    with pytest.raises(ValueError):
        backoff_cap(-1)


@pytest.mark.parametrize("attempt", [1, 2, 3, 5, 10])
def test_jittered_delay_never_exceeds_the_cap(attempt: int) -> None:
    rng = random.Random(1234)
    cap = backoff_cap(attempt, base_seconds=2.0, cap_seconds=300.0)
    for _ in range(200):
        delay = compute_backoff(attempt, base_seconds=2.0, cap_seconds=300.0, rng=rng)
        assert 0.0 <= delay <= cap


def test_jitter_is_reproducible_with_a_seeded_rng() -> None:
    a = compute_backoff(3, rng=random.Random(42))
    b = compute_backoff(3, rng=random.Random(42))
    assert a == b


def test_jitter_varies_across_calls_without_a_fixed_seed() -> None:
    rng = random.Random(7)
    delays = {compute_backoff(4, rng=rng) for _ in range(20)}
    assert len(delays) > 1  # vanishingly unlikely to collide 20 times if truly random


def test_higher_attempts_have_a_higher_expected_delay() -> None:
    rng = random.Random(99)
    early = [compute_backoff(1, base_seconds=2.0, cap_seconds=300.0, rng=rng) for _ in range(500)]
    late = [compute_backoff(5, base_seconds=2.0, cap_seconds=300.0, rng=rng) for _ in range(500)]
    assert (sum(late) / len(late)) > (sum(early) / len(early))
