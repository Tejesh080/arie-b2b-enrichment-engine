"""The Apollo person-enrichment adapter *contract* — no client, no key, no calls.

**This module makes zero network requests and requires zero credentials.** It
is the normalization half of a provider adapter, written and tested against
fixtures first, so that the shape of what Apollo must return — and what ARIE
will do with it — was reviewable *before* a paid provider was wired in.

The transport half now exists, in ``arie.providers.live_apollo``: an
``httpx.Client``, a ``fetch``, a ledger entry, and a registration in
``arie.live.providers.REGISTERED_LIVE_PROVIDER_NAMES``. **The split survives
that, and is meant to.** Everything the transport emits still passes through
:func:`normalize_apollo_person` here, so the part that decides what a job title
is *worth* stays readable, reviewable, and exhaustively fixture-testable
without a key, a client, or a mock — a property
``tests/unit/test_apollo_contract.py`` asserts against this module's imports
rather than its prose.

**Why Apollo, and why person enrichment specifically.** The Live V1 audit found
Abstract supplies only ``employee_count`` and ``industry`` — both company-level.
That leaves ``title_seniority`` (up to 20.0 ICP points) and ``title_function``
(up to 15.0) permanently unknown for every live lead, which is 35 of the
scorer's 100 reachable points and, more importantly, keeps the score bounds so
wide that almost nothing can settle. Apollo's People Enrichment endpoint
returns both, keyed by an email address ARIE already has at ingestion. No other
candidate provider closes that gap with an identifier already in hand.

**What this adapter will and will not produce.**

Produces (canonical, via ``arie.normalization.contract``):

* ``title_seniority`` — from Apollo's own ``seniority`` enum where present,
  falling back to title parsing. Both paths go through
  ``arie.normalization.taxonomy``; neither ever emits a raw Apollo string.
* ``title_function`` — from ``departments``/``subdepartments`` where present,
  falling back to title parsing.

Will **not** produce, and this is a scope decision rather than an oversight:

* ``buying_intent`` — Apollo sells intent data, and it is a modelled score from
  a vendor whose methodology ARIE cannot inspect or validate. The Live V1 brief
  defers intent until a trustworthy source exists; wiring Apollo's would put a
  20-point field, the single largest in the ruleset, on an unvalidated number.
* ``recent_trigger_event`` — same reasoning; job-change and funding signals are
  available and unvalidated.
* ``disqualifying_flag`` — Apollo returns no such thing. Deriving one from
  "this looks like a freelancer" would be inventing a blocker, which
  ``arie.icp`` explicitly refuses to do. The field stays unknown, and the score
  floor stays at zero, which is the honest consequence.

**Identity fields are carried but never scored.** ``normalized_identity``
returns name/title/linkedin/organization-domain for the receipt and for a human
reviewer to sanity-check who ARIE actually matched. None of them is a
``SCORED_FIELD``, so none can reach the scorer through
``arie.normalization.contract`` — which only normalizes fields with a
registered normalizer.

**The response shapes below are the contract, not a guess at Apollo's schema.**
Where Apollo's live payload differs from these fixtures, the fix is in
:func:`normalize_apollo_person`'s extraction step — not in the taxonomy, not in
the scorer. That is the whole point of putting the boundary here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from arie.core.types import EntityType
from arie.normalization.contract import NormalizationReport, normalize_provider_fields
from arie.normalization.taxonomy import (
    UNKNOWN,
    function_from_title,
    is_unknown,
    normalize_function,
    normalize_seniority,
    seniority_from_title,
)

__all__ = [
    "APOLLO_PROVIDER_NAME",
    "APOLLO_PROVIDES_FIELDS",
    "ApolloPersonIdentity",
    "extract_apollo_person",
    "normalize_apollo_person",
    "normalized_identity",
]

APOLLO_PROVIDER_NAME = "apollo_person_enrichment"
"""Declared here, in the module with no credentials, rather than in the adapter.

That ordering was the point: ``arie.live.providers.LIVE_PROVIDER_NAMES`` could
include this name while no client existed, so the spend caps covered Apollo
from its first real call rather than from the moment someone remembered to add
it. ``provider_calls`` rows carry it now, and the budget query has been summing
them since the first one."""

APOLLO_PROVIDES_FIELDS: tuple[str, ...] = ("title_seniority", "title_function")
"""Exactly the two scored fields. Kept narrow on purpose: the EVoI controller
reads ``provides_fields`` to estimate whether a call is worth making, and
over-declaring would make this provider look more valuable than it is."""

_ENTITY_TYPE: EntityType = "person"


@dataclass(frozen=True)
class ApolloPersonIdentity:
    """Who Apollo says this is — for the receipt and the reviewer, never the scorer."""

    full_name: str | None
    title: str | None
    email: str | None
    linkedin_url: str | None
    organization_name: str | None
    organization_domain: str | None

    def audit(self) -> dict[str, str]:
        """Non-null fields only, as a flat log/span-safe dict."""
        return {
            key: value
            for key, value in {
                "full_name": self.full_name,
                "title": self.title,
                "email": self.email,
                "linkedin_url": self.linkedin_url,
                "organization_name": self.organization_name,
                "organization_domain": self.organization_domain,
            }.items()
            if value
        }


def _first_string(values: Any) -> str | None:
    """First usable string from Apollo's list-shaped fields.

    ``departments`` and ``subdepartments`` are arrays; a person in two
    departments gets one function under this ruleset, and taking the first is
    both deterministic and what Apollo's own ordering means (primary first).
    """
    if isinstance(values, str):
        return values.strip() or None
    if isinstance(values, Sequence):
        for item in values:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


def _person_payload(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap the envelope. Apollo returns ``{"person": {...}}`` for a match and
    ``{"person": null}`` (or an empty body) for a miss — the miss is a
    ``ProviderStatus.MISS``, not an error, exactly as
    ``arie.providers.live_abstract`` already treats an empty 200."""
    person = body.get("person")
    if isinstance(person, Mapping):
        return person
    return body if "title" in body or "seniority" in body else {}


def extract_apollo_person(body: Mapping[str, Any]) -> dict[str, Any]:
    """Pull ARIE field names out of an Apollo payload, still in Apollo's vocabulary.

    This is the *only* Apollo-specific step. It renames and picks; it never
    interprets. Everything it returns is raw vendor vocabulary and is unsafe to
    score — :func:`normalize_apollo_person` is what makes it safe.

    Both fields prefer Apollo's structured enums and fall back to the free-text
    title, because the enums are absent for a meaningful share of records.
    The fallback is a *last* resort rather than a blend: mixing a parsed title
    into a present enum would make the result depend on which of two disagreeing
    sources happened to be parsed, with no way to tell afterwards which won.
    """
    person = _person_payload(body)
    title = person.get("title")

    extracted: dict[str, Any] = {}
    seniority = _prefer_enum_then_title(
        enum_value=person.get("seniority"),
        title=title,
        normalize_enum=normalize_seniority,
        parse_title=seniority_from_title,
    )
    if seniority is not None:
        extracted["title_seniority"] = seniority

    function = _prefer_enum_then_title(
        enum_value=(
            _first_string(person.get("departments")) or _first_string(person.get("subdepartments"))
        ),
        title=title,
        normalize_enum=normalize_function,
        parse_title=function_from_title,
    )
    if function is not None:
        extracted["title_function"] = function

    return extracted


def _prefer_enum_then_title(
    *,
    enum_value: Any,
    title: Any,
    normalize_enum: Callable[[Any], str],
    parse_title: Callable[[Any], str],
) -> Any:
    """Apollo's own enum, else the parsed title, else the raw value unchanged.

    Three cases, and the third is the one worth spelling out:

    1. The enum maps. Use it — a vendor's structured answer beats parsing its
       prose, and mixing the two would make the result depend on which of two
       disagreeing sources happened to parse.
    2. The enum is absent or unmappable but the title parses. Use the parsed
       value. Apollo omits these enums for a meaningful share of records, and
       without this those leads would be permanently unknown on 35 of the
       scorer's 100 points.
    3. Neither works. Return the **raw** vendor value rather than ``None``, so
       ``arie.normalization.contract`` reports it in ``unmapped`` with the
       string Apollo actually sent. Returning ``None`` here would silently
       discard the one piece of evidence that tells an operator which alias
       table row is missing — the whole feedback loop ``unmapped`` exists for.
       ``None`` is reserved for "the vendor said nothing", which is not a
       vocabulary problem and needs no audit entry.
    """
    if not is_unknown(normalize_enum(enum_value)):
        return enum_value

    parsed = parse_title(title)
    if not is_unknown(parsed):
        return parsed

    return enum_value if enum_value is not None else title


def normalize_apollo_person(body: Mapping[str, Any]) -> NormalizationReport:
    """The full raw-payload → canonical-evidence path for one Apollo response.

    Returns the same :class:`~arie.normalization.contract.NormalizationReport`
    every other adapter returns, which is what lets the live handler treat a
    second provider as one more source rather than a second code path.
    """
    return normalize_provider_fields(
        provider=APOLLO_PROVIDER_NAME,
        entity_type=_ENTITY_TYPE,
        raw_fields=extract_apollo_person(body),
    )


def normalized_identity(body: Mapping[str, Any]) -> ApolloPersonIdentity:
    """Identity fields, for display and audit only. Never scored."""
    person = _person_payload(body)
    organization = person.get("organization")
    org: Mapping[str, Any] = organization if isinstance(organization, Mapping) else {}

    def _clean(value: Any) -> str | None:
        return value.strip() or None if isinstance(value, str) else None

    name = _clean(person.get("name"))
    if name is None:
        parts = [_clean(person.get("first_name")), _clean(person.get("last_name"))]
        joined = " ".join(part for part in parts if part)
        name = joined or None

    return ApolloPersonIdentity(
        full_name=name,
        title=_clean(person.get("title")),
        email=_clean(person.get("email")),
        linkedin_url=_clean(person.get("linkedin_url")),
        organization_name=_clean(org.get("name")),
        organization_domain=_clean(org.get("primary_domain")) or _clean(org.get("website_url")),
    )


# Re-exported so a reader of this module can see, without a second file open,
# that "unmappable" here means the same thing it means everywhere else.
UNMAPPED_SENTINEL = UNKNOWN
