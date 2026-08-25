"""Per-field TTL policy for cached evidence.

Cache lifetime is a property of the *fact*, not the provider that supplied it —
the same field can arrive from several providers (e.g. ``employee_count`` from
``internal_crm``, ``firmographics_basic``, or ``deep_research``), and how long it
stays trustworthy depends on how fast the real-world attribute changes, not on
which vendor happened to answer. So TTL is keyed by ``field_name``, matching
``arie.scoring.rules.SCORED_FIELDS``.

Values are assumptions, not measurements — following the project's rule
(docs/benchmark.md) that no parameter enters production without a stated
justification. Nothing here has been fitted to real provider drift; revisit once
a real adapter's staleness is observable.
"""

from __future__ import annotations

_DAY = 86_400

FIELD_TTL_SECONDS: dict[str, int] = {
    # Firmographic identity. Changes over quarters, not days — the most
    # cacheable facts in the schema.
    "industry": 90 * _DAY,
    "employee_count": 30 * _DAY,
    # Person attributes. Stable until a job change; monthly re-check balances
    # staleness risk against re-paying for a fact that rarely moved.
    "title_seniority": 60 * _DAY,
    "title_function": 60 * _DAY,
    # The disqualifying flag is safety-relevant, not just score-relevant: acting
    # on a stale "not blocked" is worse than acting on a stale employee count.
    # Short enough to catch a status change within a couple of weeks, long
    # enough that most leads don't re-pay just to reconfirm "still clear".
    "disqualifying_flag": 14 * _DAY,
    # Time-sensitive by construction — "recent" trigger events and live buying
    # intent are only informative while they're fresh, so they expire fastest.
    "recent_trigger_event": 7 * _DAY,
    "buying_intent": 3 * _DAY,
}

DEFAULT_TTL_SECONDS = 7 * _DAY
"""Fallback for a field with no entry above.

Deliberately conservative (short) rather than silently caching an unrecognised
field indefinitely — an unlisted field should degrade to "ask again soon", not
to "trust this forever" by accident.
"""


def ttl_for_field(field_name: str) -> int:
    return FIELD_TTL_SECONDS.get(field_name, DEFAULT_TTL_SECONDS)
