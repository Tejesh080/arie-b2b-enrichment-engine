"""Where a lead came from, and what that entitles it to.

A production audit found 193 of 194 leads in a customer's organization were
development exhaust — test harnesses, canaries, frozen-corpus identities,
reserved-domain fixtures — and every aggregate the product showed that
customer was computed over the mixture. The provenance was recoverable from
`leads.source`, `external_ref` prefixes and reserved domains, but only by
re-deriving it with regexes at query time, which drifts the moment a new
harness appears and cannot express "a human looked at this and decided".

So provenance is a column (`leads.data_class`, migration 0042) and this module
is its vocabulary.

**The rule, in one line:** only `PRODUCTION` participates in anything a
customer sees in aggregate; every class stays readable by `lead_id`.

That second half is what makes this quarantine rather than deletion. A
benchmark lead's Decision Receipt still opens, its ledger rows still sum, its
evidence is still there to inspect. It simply stops being counted as if it
were a customer's.

**Nobody can claim `PRODUCTION`.** It is the server's default for ordinary
ingest and there is no request field, header value or parameter that sets it —
see `arie.api.ingest.LeadIngestCommand`. A caller with a machine credential may
*downgrade* its own writes to a non-production class, and that is the only
direction the vocabulary moves.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "PRODUCTION_ONLY_SQL",
    "LeadDataClass",
    "assignable_by_caller",
]


class LeadDataClass(StrEnum):
    """Must stay in sync with `leads_data_class_check` in migration 0042."""

    PRODUCTION = "production"
    """A real customer's own lead. The only class that reaches an aggregate."""

    INTEGRATION_TEST = "integration_test"
    """An automated test or a deployment-validation run."""

    LOAD_TEST = "load_test"
    """A throughput or concurrency exercise."""

    CANARY = "canary"
    """A release canary."""

    BENCHMARK = "benchmark"
    """An identity from the frozen evaluation corpus (`generate_dataset`)."""

    SYNTHETIC_FIXTURE = "synthetic_fixture"
    """A placeholder on a name reserved by RFC 2606/6761 — `.invalid`,
    `.test`, `.example`, `example.com`. These can never be a real company."""

    QUARANTINED = "quarantined"
    """Withdrawn from the product by an operator, for any reason. The escape
    hatch for "this is wrong and I do not want to argue about which kind of
    wrong it is"."""

    UNKNOWN = "unknown"
    """Provenance could not be established.

    Deliberately **not** production. Nine leads in the audited organization
    landed here: created through the app before the organization had a single
    member, on non-reserved domains, with no harness tag. Two of them are named
    `probe6.com` and `probe7.com` and are obviously fixtures — but obviously is
    not evidence, and guessing from a domain name is the blacklist this column
    exists to replace. Unknown stays unknown, and stays out of aggregates."""


PRODUCTION_ONLY_SQL = "l.data_class = 'production'"
"""The predicate every org-scoped aggregate adds, written once.

Assumes the `leads` table is aliased `l`, which every statement this is spliced
into already does. A literal rather than a bound parameter so it can sit inside
statements that are otherwise fully parameterised, and so
`idx_leads_org_production`'s own partial predicate matches it exactly.
"""


def assignable_by_caller(value: str) -> LeadDataClass:
    """Parse a caller-supplied class, refusing `production`.

    Raises `ValueError` for an unknown value *and* for `production`, which is
    the whole point: a machine credential may mark its own writes as test data,
    and nothing may mark anything as a customer's. `production` is reached only
    by the server's default, so there is no path by which a harness talks its
    way into the numbers a customer reads.
    """
    try:
        parsed = LeadDataClass(value)
    except ValueError:
        allowed = ", ".join(
            sorted(c.value for c in LeadDataClass if c is not LeadDataClass.PRODUCTION)
        )
        raise ValueError(f"unknown data class {value!r}; expected one of: {allowed}") from None
    if parsed is LeadDataClass.PRODUCTION:
        raise ValueError(
            "'production' cannot be requested — it is the server's own default for "
            "ordinary ingest, and no caller may assert it"
        )
    return parsed
