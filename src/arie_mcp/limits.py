"""Output-size and row constants (spec § 11).

Kept as plain constants rather than ``Settings`` fields: these are safety
ceilings, not operator-tunable configuration — a tool must not become
unbounded because an environment variable was set wrong. The two timeouts
(DB statement, tool wall-clock) do live in ``Settings`` since a slower
environment may legitimately need to raise them; row/string/response caps
do not have that legitimate reason to move.
"""

from __future__ import annotations

MAX_ROWS = 100
"""Hard ceiling for any list-shaped tool result, regardless of what a
caller requests."""

DEFAULT_ROWS = 20
"""Default page size when a caller doesn't specify one."""

MAX_STRING_LEN = 500
"""Any free-text field (e.g. ``last_error``) is truncated to this length.
Also enforced at the view layer (migrations/0039) as defense in depth."""

MAX_RESPONSE_BYTES = 100_000
"""Serialized JSON response cap. A tool that would exceed this returns an
aggregated/truncated form instead — never a silently-cut JSON body."""


def cap_limit(
    requested: int | None, *, default: int = DEFAULT_ROWS, maximum: int = MAX_ROWS
) -> int:
    """Clamp a caller-requested row limit into ``[1, maximum]``.

    A non-positive or missing request falls back to ``default``; anything
    above ``maximum`` is silently capped, not rejected — the response's own
    ``truncated``/``row_count`` fields are what tell the caller a cap was
    hit (spec § 11), not a validation error.
    """
    if requested is None or requested <= 0:
        return default
    return min(requested, maximum)


def truncate_str(value: str | None, *, max_len: int = MAX_STRING_LEN) -> str | None:
    if value is None:
        return None
    if len(value) <= max_len:
        return value
    return value[:max_len]
