"""The Hunter enrichment adapter *contract* — no client, no key, no calls.

Same split, same reason as ``arie.providers.apollo_contract``: this module is
the raw-payload→canonical-evidence half of a provider adapter, reviewable and
exhaustively fixture-testable with no credential and no transport. The
``httpx`` half lives in ``arie.providers.live_hunter`` and everything it emits
passes through here.

**The payload shape.** Hunter's enrichment endpoints return a Clearbit-style
schema (Hunter positions the API as the drop-in Clearbit replacement):
``people/find`` returns ``{"data": {<person>}}`` and ``combined/find`` returns
``{"data": {"person": {...}, "company": {...}}}``. The person's evidence lives
under ``employment`` — ``title`` (free text), ``role``/``subRole`` (a closed
enum: ``sales``, ``marketing``, ``engineering``, ``customer_service``, ...),
and ``seniority`` (a closed enum: ``executive``, ``director``, ``manager``,
``senior``, ``junior``). :func:`extract_hunter_person` accepts either envelope,
so the adapter can be pointed at the person-only endpoint by config without a
second contract module.

**Seniority is parsed title-first, and this deliberately inverts Apollo's
rule.** ``apollo_contract`` prefers the vendor's structured enum over parsing
its prose, on the argument that a structured answer beats a parsed one. That
argument has a premise: the vendor's vocabulary is at least as fine-grained as
the canonical ladder. Apollo's is (it ships ``vp`` as a value). Hunter's is
not — its five-value ladder folds C-level and VP into one ``executive``
bucket, and those are different rungs with different weights in
``arie.scoring.rules`` (20.0 vs 18.0). Trusting the enum first would score
every VP Hunter returns as a C-level: a systematic over-credit on exactly the
conservative-bias axis ``arie.normalization.taxonomy`` documents. So for
seniority the free-text title (which *can* say "VP") is parsed first, and the
coarse enum is the fallback for a record whose title says nothing. ``function``
keeps the enum-first rule, because Hunter's role enum maps onto the canonical
function set with no such coarseness problem. One vendor, two orderings, each
argued — that is the kind of knowledge this module exists to hold.

**Company data is extracted for comparison, not for evidence.** ``combined``
returns ``category.industry`` and ``metrics.employees``, the same two fields
Abstract supplies. :func:`normalize_hunter_company` maps them through the
canonical layer so the provider bake-off can measure Hunter-vs-Abstract
agreement in canonical vocabulary — but ``arie.providers.live_hunter`` does
not persist them as evidence, and ``HUNTER_PROVIDES_FIELDS`` does not declare
them. Whether Hunter's company data is reliable enough to score from is
precisely what the bake-off exists to measure; persisting it first would be
acting on the answer before asking the question.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from arie.core.types import EntityType
from arie.normalization.contract import NormalizationReport, normalize_provider_fields
from arie.normalization.taxonomy import (
    function_from_title,
    is_unknown,
    normalize_function,
    normalize_seniority,
    seniority_from_title,
)

__all__ = [
    "HUNTER_PROVIDER_NAME",
    "HUNTER_PROVIDES_FIELDS",
    "HunterPersonIdentity",
    "extract_hunter_company",
    "extract_hunter_person",
    "normalize_hunter_company",
    "normalize_hunter_person",
    "normalized_identity",
]

HUNTER_PROVIDER_NAME = "hunter_combined_enrichment"
"""Named for the endpoint the default config points at. Declared here, in the
module with no credentials, for the same reason ``APOLLO_PROVIDER_NAME`` was:
``arie.live.providers`` lists it and the spend caps therefore cover it from
the first real call."""

HUNTER_PROVIDES_FIELDS: tuple[str, ...] = ("title_seniority", "title_function")
"""The same two scored fields Apollo declares — the overlap is the point. Two
independent person providers over the same fields is what makes cross-provider
agreement measurable, and what gives the optimized loop a cheaper first try
before the more expensive lookup. Company fields are deliberately absent; see
the module docstring."""

_ENTITY_TYPE: EntityType = "person"


@dataclass(frozen=True)
class HunterPersonIdentity:
    """Who Hunter says this is — for the receipt and the reviewer, never the scorer."""

    full_name: str | None
    title: str | None
    email: str | None
    employer_name: str | None
    employer_domain: str | None

    def audit(self) -> dict[str, str]:
        """Non-null fields only, as a flat log/span-safe dict."""
        return {
            key: value
            for key, value in {
                "full_name": self.full_name,
                "title": self.title,
                "email": self.email,
                "employer_name": self.employer_name,
                "employer_domain": self.employer_domain,
            }.items()
            if value
        }


def _clean(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def person_payload(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """Unwrap either envelope to the person object.

    ``combined/find`` nests it as ``data.person``; ``people/find`` puts the
    person directly under ``data``. Distinguished by the presence of a
    ``person`` key rather than by which endpoint was configured, so the same
    contract serves both.
    """
    data = body.get("data")
    if not isinstance(data, Mapping):
        return {}
    if "person" in data or "company" in data:
        person = data.get("person")
        return person if isinstance(person, Mapping) else {}
    return data


def company_payload(body: Mapping[str, Any]) -> Mapping[str, Any]:
    """The company object from a ``combined/find`` response, or empty.

    Empty for ``people/find`` responses and for a combined response whose
    company half came back ``null`` — Hunter can match the person and still
    know nothing about the employer.
    """
    data = body.get("data")
    if not isinstance(data, Mapping):
        return {}
    company = data.get("company")
    return company if isinstance(company, Mapping) else {}


def extract_hunter_person(body: Mapping[str, Any]) -> dict[str, Any]:
    """Pull ARIE field names out of a Hunter payload, still in Hunter's vocabulary.

    The only Hunter-specific person logic in the pipeline. Returns raw vendor
    values — :func:`normalize_hunter_person` is what makes them scoreable.

    Seniority: title first, ``seniority`` enum as fallback (the inversion the
    module docstring argues). Function: ``role`` enum first, title as fallback
    (Apollo's own rule, because no coarseness problem exists here). In both
    cases an unmappable-but-present value is returned raw rather than dropped,
    so ``arie.normalization.contract`` reports it in ``unmapped`` with the
    string Hunter actually sent — the alias-table feedback loop.
    """
    person = person_payload(body)
    employment = person.get("employment")
    job: Mapping[str, Any] = employment if isinstance(employment, Mapping) else {}
    title = job.get("title")

    extracted: dict[str, Any] = {}

    seniority_enum = job.get("seniority")
    parsed_seniority = seniority_from_title(title)
    if not is_unknown(parsed_seniority):
        extracted["title_seniority"] = parsed_seniority
    elif not is_unknown(normalize_seniority(seniority_enum)):
        extracted["title_seniority"] = seniority_enum
    elif seniority_enum is not None or title is not None:
        # Nothing mapped but the vendor said *something* — surface the enum if
        # present (the more table-fixable string), else the title.
        extracted["title_seniority"] = seniority_enum if seniority_enum is not None else title

    role = job.get("role") or job.get("subRole")
    if not is_unknown(normalize_function(role)):
        extracted["title_function"] = role
    elif not is_unknown(function_from_title(title)):
        extracted["title_function"] = function_from_title(title)
    elif role is not None or title is not None:
        extracted["title_function"] = role if role is not None else title

    return extracted


def extract_hunter_company(body: Mapping[str, Any]) -> dict[str, Any]:
    """The Abstract-shaped pair from a combined response, raw.

    ``employee_count`` from ``metrics.employees`` (an integer when Hunter has
    one; the ``employeesRange`` string is deliberately not parsed into a
    number — a range is not a count, and inventing its midpoint would be
    manufacturing precision) and ``industry`` from ``category.industry``.
    """
    company = company_payload(body)
    metrics = company.get("metrics")
    category = company.get("category")
    metrics_map: Mapping[str, Any] = metrics if isinstance(metrics, Mapping) else {}
    category_map: Mapping[str, Any] = category if isinstance(category, Mapping) else {}

    extracted: dict[str, Any] = {}
    if metrics_map.get("employees") is not None:
        extracted["employee_count"] = metrics_map.get("employees")
    if category_map.get("industry") is not None:
        extracted["industry"] = category_map.get("industry")
    return extracted


def normalize_hunter_person(body: Mapping[str, Any]) -> NormalizationReport:
    """The full raw-payload → canonical-evidence path for the person half."""
    return normalize_provider_fields(
        provider=HUNTER_PROVIDER_NAME,
        entity_type=_ENTITY_TYPE,
        raw_fields=extract_hunter_person(body),
    )


def normalize_hunter_company(body: Mapping[str, Any]) -> NormalizationReport:
    """Canonical view of the company half — for comparison, never for evidence.

    Returns the same report type as every evidence-producing path so the
    bake-off can compare Hunter's company claims against Abstract's in
    canonical vocabulary, but no caller persists its ``fields``; the adapter
    only carries ``audit()`` on the result's ``raw`` and the bake-off harness
    reads it from there.
    """
    return normalize_provider_fields(
        provider=HUNTER_PROVIDER_NAME,
        entity_type="company",
        raw_fields=extract_hunter_company(body),
    )


def normalized_identity(body: Mapping[str, Any]) -> HunterPersonIdentity:
    """Identity fields, for display and audit only. Never scored."""
    person = person_payload(body)
    name = person.get("name")
    name_map: Mapping[str, Any] = name if isinstance(name, Mapping) else {}
    employment = person.get("employment")
    job: Mapping[str, Any] = employment if isinstance(employment, Mapping) else {}

    full_name = _clean(name_map.get("fullName"))
    if full_name is None:
        parts = [_clean(name_map.get("givenName")), _clean(name_map.get("familyName"))]
        joined = " ".join(part for part in parts if part)
        full_name = joined or None

    return HunterPersonIdentity(
        full_name=full_name,
        title=_clean(job.get("title")),
        email=_clean(person.get("email")),
        employer_name=_clean(job.get("name")),
        employer_domain=_clean(job.get("domain")),
    )
