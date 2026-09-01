"""Productization M5 Issue 3 — `arie.live.outcome_cache.is_uncertain_outcome`'s
pure classification, no database. The guard's own SQL-backed behavior
(suppression, TTL, tenant scoping) is covered against a real ledger in
``tests/integration/test_provider_outcome_and_identity_integration.py``.
"""

from __future__ import annotations

import pytest

from arie.live.outcome_cache import is_uncertain_outcome


def test_a_timeout_is_uncertain() -> None:
    assert is_uncertain_outcome("timeout") is True


@pytest.mark.parametrize(
    "error_kind",
    [
        "transport_error:ConnectError",
        "transport_error:ReadError",
        "transport_error:RemoteProtocolError",
        "transport_error:",  # any suffix, including empty, is still uncertain
    ],
)
def test_any_transport_error_is_uncertain(error_kind: str) -> None:
    assert is_uncertain_outcome(error_kind) is True


@pytest.mark.parametrize(
    "error_kind",
    [
        None,
        "authentication_failed",
        "rate_limited",
        "quota_exhausted",
        "insufficient_credits",
        "server_error",
        "unprocessable_request",
        "malformed_response",
        "unexpected_status:418",
    ],
)
def test_a_definite_result_is_never_uncertain(error_kind: str | None) -> None:
    """A real HTTP response — success, miss, or a rejection — means the
    vendor was reached. Only the two "no response at all" failure modes
    are uncertain; everything else is a known, settled outcome."""
    assert is_uncertain_outcome(error_kind) is False


def test_a_kind_that_merely_contains_timeout_is_not_matched_by_substring() -> None:
    """Exact-match for `"timeout"`, not a substring check — a future,
    unrelated `error_kind` that happens to contain the word must not be
    silently swept into this classification."""
    assert is_uncertain_outcome("read_timeout_but_actually_a_definite_status") is False
