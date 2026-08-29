"""Canonical vocabularies, and the deliberate mapping from provider strings onto them.

**The canonical vocabulary is the scorer's vocabulary, not a new one.**

Phase 2 of the Live V1 Foundation brief offered an example industry family list
(``software``/``saas``/``technology``/``financial-services``/...) and explicitly
allowed a different clean mapping "if the existing scorer requires" one. It
does. ``arie.scoring.rules._INDUSTRY_POINTS`` is a closed, weighted set frozen
by the M0 benchmark; inventing a second, parallel set here would mean either
(a) two vocabularies that drift, or (b) editing the scoring weights, which is
benchmark science this step must not touch (Phase 5). So the canonical sets
below *are* the scorer's sets, widened only with **recognised-but-unscored**
families and the ``UNKNOWN`` sentinel — neither of which changes any weight.

That widening is the point. ``construction`` is not in ``_INDUSTRY_POINTS``, so
it scores 0.0 — but it is in ``CANONICAL_INDUSTRIES``, so it is *known* 0.0:
deliberately assessed, poor fit, bounds tightened. ``"pet grooming
franchises"`` maps to ``UNKNOWN``: also 0.0 points, but the field stays
*unknown*, keeps its full contribution to the reachable upper bound, and keeps
counting against completeness. Same number, opposite epistemics. See
``arie.scoring.rules.UNKNOWN`` for the full argument.

**Mapping is explicit, then heuristic, in that order.** Every lookup first
tries an exact alias table (deterministic, auditable, the place to add a
vendor's exact string), then an ordered list of phrase rules whose order
encodes real precedence decisions — ``"financial technology"`` must be tested
before ``"financial"``, or every fintech would land in generic financial
services. Nothing falls through to a guess: an unmatched string is ``UNKNOWN``.

**Phrase matching is whole-word, and stemming is opt-in.** A rule phrase
matches only at word boundaries, so ``"tech"`` matches ``"Tech"`` and
``"Technology"`` but never ``"Biotechnology"``. A trailing ``*``
(``"manufactur*"``) opts that phrase into prefix matching. Substring matching
without this discipline is how ``"bio*tech*nology"`` silently becomes a
software company worth 15 ICP points.

**Every mapping choice that credits or withholds ICP points is recorded here**,
next to the rule, rather than in a design document nobody reads at 2am. The
consistent bias is *conservative*: where a family is ambiguous between an
ICP-scoring canonical value and a non-scoring one, it maps to the non-scoring
one. Under-crediting a good lead costs one escalation to a human; over-crediting
a bad one is a false auto-route, and until real-world recalibration exists
(``arie.live.safety``) there is no measurement that would catch it.
"""

from __future__ import annotations

import re
from typing import Any

from arie.scoring.rules import UNKNOWN, is_unknown

__all__ = [
    "CANONICAL_FUNCTIONS",
    "CANONICAL_INDUSTRIES",
    "CANONICAL_SENIORITIES",
    "EMPLOYEE_COUNT_MAX",
    "EMPLOYEE_COUNT_MIN",
    "UNKNOWN",
    "canonical_key",
    "function_from_title",
    "is_unknown",
    "normalize_employee_count",
    "normalize_function",
    "normalize_industry",
    "normalize_seniority",
    "seniority_from_title",
]

# --- surface-form normalization ----------------------------------------------

_SEPARATORS_RE = re.compile(r"[/,;|+()\[\]{}·•]+")
_NON_TOKEN_RE = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE_RE = re.compile(r"\s+")


def canonical_key(raw: Any) -> str:
    """Fold a provider string to the comparison form the tables below are keyed by.

    Case, punctuation, ampersands, hyphens, and separator characters are all
    surface noise a vendor varies without meaning anything by it —
    ``"Computer Software"``, ``"computer-software"``, and ``"COMPUTER
    SOFTWARE"`` are one value. Folding them here rather than at each call site
    is what makes the alias tables small enough to actually read.
    """
    if raw is None:
        return ""
    text = str(raw).strip().lower()
    if not text:
        return ""
    text = text.replace("&", " and ")
    text = _SEPARATORS_RE.sub(" ", text)
    text = _NON_TOKEN_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _phrase_pattern(phrase: str) -> re.Pattern[str]:
    """Compile one rule phrase into a word-boundary matcher.

    A trailing ``*`` opts into prefix matching on the final word, which is how
    ``"manufactur*"`` covers manufacturing/manufacturer/manufactured without
    three table entries. Without it the match is exact-word, which is what
    keeps ``"tech"`` out of ``"biotechnology"``.
    """
    stem = phrase.endswith("*")
    words = (phrase[:-1] if stem else phrase).split()
    body = r"\s+".join(re.escape(word) for word in words)
    tail = r"[a-z]*" if stem else ""
    return re.compile(rf"(?<![a-z0-9]){body}{tail}(?![a-z0-9])")


def _compile_rules(
    rules: tuple[tuple[tuple[str, ...], str], ...],
) -> tuple[tuple[tuple[re.Pattern[str], ...], str], ...]:
    return tuple(
        (tuple(_phrase_pattern(phrase) for phrase in phrases), canonical)
        for phrases, canonical in rules
    )


def _first_match(
    key: str, rules: tuple[tuple[tuple[re.Pattern[str], ...], str], ...]
) -> str | None:
    for patterns, canonical in rules:
        if any(pattern.search(key) for pattern in patterns):
            return canonical
    return None


# --- industry -----------------------------------------------------------------

# Canonical industry families. The first eight are exactly
# ``arie.scoring.rules._INDUSTRY_POINTS``'s keys and carry ICP points; the rest
# are recognised-but-unscored families that exist so a real-world industry can
# be *known* and worth zero rather than unknown. Adding one here can never
# change a benchmark number — the synthetic generator emits only the first
# eight — but it can change a live lead's bounds, which is the whole point.
CANONICAL_INDUSTRIES: frozenset[str] = frozenset(
    {
        # scored by arie.scoring.rules
        "software",
        "fintech",
        "healthtech",
        "ecommerce",
        "logistics",
        "manufacturing",
        "education",
        "nonprofit",
        # recognised, deliberately unscored under the reference ICP
        "financial_services",
        "professional_services",
        "healthcare",
        "construction",
        "retail",
        "real_estate",
        "media",
        "telecom",
        "energy",
        "hospitality",
        "government",
        "agriculture",
        "other",
        UNKNOWN,
    }
)

# Exact-match aliases, keyed by ``canonical_key`` output. This is the table to
# extend when a vendor's literal string is observed in the wild — a smoke test
# that finds an unmapped value should land a line here, not a new heuristic.
_INDUSTRY_ALIASES: dict[str, str] = {
    # --- software / SaaS: the reference ICP's primary target ------------------
    # The Phase 7 acceptance case. Abstract API returns "Computer Software" for
    # a large share of B2B software companies; before this line it scored zero.
    "computer software": "software",
    "software": "software",
    "software development": "software",
    "software and services": "software",
    "saas": "software",
    "b2b saas": "software",
    "software as a service": "software",
    "enterprise software": "software",
    "application software": "software",
    "cloud software": "software",
    "cloud computing": "software",
    "information technology and services": "software",
    "information technology": "software",
    "it services and it consulting": "software",
    "technology": "software",
    "tech": "software",
    "internet software and services": "software",
    "internet": "software",
    "computer and network security": "software",
    "computer network security": "software",
    "artificial intelligence": "software",
    "developer tools": "software",
    # --- fintech vs. financial services --------------------------------------
    # Kept apart on purpose, and this is the mapping most worth arguing with.
    # `fintech` carries 15.0 points — as much as software — because the M0 ICP
    # treats a payments/lending *software* company as a prime target. A bank,
    # an insurer, or a wealth manager is a financial-services *institution*:
    # a plausible buyer, but not the modelled ICP, and nothing in the corpus
    # calibrated it as one. Crediting them 15.0 by association would be the
    # over-crediting this module's docstring rules out.
    "fintech": "fintech",
    "financial technology": "fintech",
    "financial software": "fintech",
    "banking software": "fintech",
    "payments": "fintech",
    "payment software": "fintech",
    "payments software": "fintech",
    "insurtech": "fintech",
    "financial services": "financial_services",
    "banking": "financial_services",
    "banks": "financial_services",
    "capital markets": "financial_services",
    "investment management": "financial_services",
    "investment banking": "financial_services",
    "venture capital and private equity": "financial_services",
    "insurance": "financial_services",
    "accounting": "financial_services",
    # --- healthtech vs. healthcare -------------------------------------------
    # Same shape of distinction as fintech/financial_services, same reasoning:
    # `healthtech` (13.0 points) is a software company selling into health; a
    # hospital or clinic is `healthcare` (0.0, known).
    "healthtech": "healthtech",
    "health tech": "healthtech",
    "digital health": "healthtech",
    "health information technology": "healthtech",
    "medical software": "healthtech",
    "healthcare software": "healthtech",
    "healthcare": "healthcare",
    "health care": "healthcare",
    "hospital and health care": "healthcare",
    "hospitals and health care": "healthcare",
    "hospitals": "healthcare",
    "medical practice": "healthcare",
    "medical devices": "healthcare",
    "pharmaceuticals": "healthcare",
    "biotechnology": "healthcare",
    # --- ecommerce vs. retail ------------------------------------------------
    # `ecommerce` (12.0) is the online-commerce operator the ICP models;
    # generic bricks-and-mortar `retail` is recognised and unscored.
    "ecommerce": "ecommerce",
    "e commerce": "ecommerce",
    "online retail": "ecommerce",
    "internet retail": "ecommerce",
    "marketplace": "ecommerce",
    "direct to consumer": "ecommerce",
    "consumer goods": "retail",
    "retail": "retail",
    "retail apparel and fashion": "retail",
    "supermarkets": "retail",
    "wholesale": "retail",
    # --- logistics -----------------------------------------------------------
    "logistics": "logistics",
    "logistics and supply chain": "logistics",
    "supply chain": "logistics",
    "transportation": "logistics",
    "transportation trucking railroad": "logistics",
    "freight": "logistics",
    "shipping": "logistics",
    "warehousing": "logistics",
    "package freight delivery": "logistics",
    "airlines aviation": "logistics",
    # --- manufacturing -------------------------------------------------------
    "manufacturing": "manufacturing",
    "industrial manufacturing": "manufacturing",
    "machinery": "manufacturing",
    "industrial machinery manufacturing": "manufacturing",
    "automotive": "manufacturing",
    "aerospace": "manufacturing",
    "aviation and aerospace component manufacturing": "manufacturing",
    "chemicals": "manufacturing",
    "electrical electronic manufacturing": "manufacturing",
    "semiconductors": "manufacturing",
    "food and beverage manufacturing": "manufacturing",
    # --- education -----------------------------------------------------------
    "education": "education",
    "education management": "education",
    "higher education": "education",
    "e learning": "education",
    "edtech": "education",
    "education technology": "education",
    "primary secondary education": "education",
    "professional training and coaching": "education",
    # --- nonprofit -----------------------------------------------------------
    "nonprofit": "nonprofit",
    "non profit": "nonprofit",
    "non profit organization management": "nonprofit",
    "nonprofit organization management": "nonprofit",
    "charity": "nonprofit",
    "philanthropy": "nonprofit",
    "civic and social organization": "nonprofit",
    # --- recognised, unscored ------------------------------------------------
    "professional services": "professional_services",
    "management consulting": "professional_services",
    "consulting": "professional_services",
    "business consulting and services": "professional_services",
    "legal services": "professional_services",
    "law practice": "professional_services",
    "staffing and recruiting": "professional_services",
    "human resources": "professional_services",
    "marketing and advertising": "professional_services",
    "advertising services": "professional_services",
    "design": "professional_services",
    "architecture and planning": "professional_services",
    "construction": "construction",
    "building materials": "construction",
    "civil engineering": "construction",
    "real estate": "real_estate",
    "commercial real estate": "real_estate",
    "property management": "real_estate",
    "media": "media",
    "media production": "media",
    "broadcast media": "media",
    "publishing": "media",
    "entertainment": "media",
    "music": "media",
    "computer games": "media",
    "gaming": "media",
    "telecommunications": "telecom",
    "telecom": "telecom",
    "wireless": "telecom",
    "oil and energy": "energy",
    "energy": "energy",
    "renewables and environment": "energy",
    "utilities": "energy",
    "mining and metals": "energy",
    "hospitality": "hospitality",
    "restaurants": "hospitality",
    "food and beverages": "hospitality",
    "leisure travel and tourism": "hospitality",
    "travel arrangements": "hospitality",
    "government administration": "government",
    "government": "government",
    "public policy": "government",
    "defense and space": "government",
    "military": "government",
    "farming": "agriculture",
    "agriculture": "agriculture",
    "ranching": "agriculture",
    "other": "other",
}

# Ordered phrase rules, tried only after the alias table misses. Order is
# semantic, not cosmetic — the FIRST matching entry wins:
#
#   1-4  the four "X-tech" families, before their non-tech parents, so
#        "Financial Technology" is fintech and not financial_services.
#   5-6  the non-tech parents (healthcare, financial_services), before the
#        generic technology catch-all, so "Medical Technology" reads as
#        healthcare (0.0, known) rather than software (15.0). Conservative by
#        the module docstring's rule.
#   7-8  software, narrow then broad.
#   9+   everything else, most specific first.
#
# Reordering these changes live scoring.
_INDUSTRY_RULES = _compile_rules(
    (
        (
            ("financial technology", "fintech", "payment*", "insurtech", "lending platform"),
            "fintech",
        ),
        (
            (
                "health tech",
                "healthtech",
                "digital health",
                "healthcare software",
                "medical software",
                "health information technology",
            ),
            "healthtech",
        ),
        (("edtech", "e learning", "elearning", "education technology"), "education"),
        (
            ("ecommerce", "e commerce", "online retail", "marketplace", "direct to consumer"),
            "ecommerce",
        ),
        (
            (
                "hospital*",
                "clinic*",
                "medical",
                "health",
                "pharma*",
                "biotech*",
                "dental",
                "nursing",
                "veterinar*",
            ),
            "healthcare",
        ),
        (
            (
                "bank*",
                "insurance",
                "insurer*",
                "financial",
                "finance",
                "lending",
                "mortgage",
                "wealth",
                "accounting",
                "credit union",
            ),
            "financial_services",
        ),
        (
            (
                "saas",
                "software",
                "developer",
                "devops",
                "cloud",
                "open source",
                "cyber*",
                "data platform",
            ),
            "software",
        ),
        (("information technology", "computer*", "internet", "technology", "tech"), "software"),
        (
            (
                "logistics",
                "freight",
                "shipping",
                "trucking",
                "supply chain",
                "warehous*",
                "courier*",
            ),
            "logistics",
        ),
        (
            (
                "manufactur*",
                "industrial",
                "machinery",
                "automotive",
                "aerospace",
                "chemical*",
                "semiconductor*",
                "electronics",
            ),
            "manufacturing",
        ),
        (
            ("education", "universit*", "school*", "learning", "training", "academy", "tutoring"),
            "education",
        ),
        (("nonprofit", "non profit", "charit*", "ngo", "foundation", "philanthrop*"), "nonprofit"),
        (
            (
                "consult*",
                "advisory",
                "agency",
                "staffing",
                "recruit*",
                "legal",
                "law firm",
                "advertising",
                "architecture",
                "accountancy",
            ),
            "professional_services",
        ),
        (("construction", "contracting", "contractor*", "builder*", "building"), "construction"),
        (("real estate", "property", "realty", "propert*"), "real_estate"),
        (
            (
                "media",
                "publish*",
                "broadcast*",
                "entertainment",
                "games",
                "gaming",
                "film",
                "music",
            ),
            "media",
        ),
        (("telecom*", "wireless", "broadband", "satellite"), "telecom"),
        (("energy", "oil", "gas", "utilities", "solar", "mining", "renewab*"), "energy"),
        (("hospitality", "restaurant*", "hotel*", "travel", "tourism", "catering"), "hospitality"),
        (("government", "public sector", "municipal*", "defense", "defence"), "government"),
        (("agricultur*", "farming", "agri", "ranch*"), "agriculture"),
        (("retail", "consumer goods", "apparel", "wholesale", "grocer*"), "retail"),
    )
)


def normalize_industry(raw: Any) -> str:
    """Map any provider industry string onto ``CANONICAL_INDUSTRIES``.

    Returns ``UNKNOWN`` for a blank, missing, or unrecognised value — never a
    silent zero-scoring canonical value. The distinction is the whole reason
    this function exists; see the module docstring.
    """
    key = canonical_key(raw)
    if not key:
        return UNKNOWN
    alias = _INDUSTRY_ALIASES.get(key)
    if alias is not None:
        return alias
    return _first_match(key, _INDUSTRY_RULES) or UNKNOWN


# --- seniority ----------------------------------------------------------------

# Exactly ``arie.scoring.rules._SENIORITY_POINTS``'s ladder, plus the sentinel.
# Nothing is added: unlike industry, every real-world seniority genuinely does
# belong somewhere on this ladder, so a widened set would only invite
# duplicates ("head" and "director" are the same rung, see below).
CANONICAL_SENIORITIES: frozenset[str] = frozenset(
    {"c_level", "vp", "director", "manager", "ic", UNKNOWN}
)

_SENIORITY_ALIASES: dict[str, str] = {
    "c level": "c_level",
    "clevel": "c_level",
    "c suite": "c_level",
    "csuite": "c_level",
    "chief": "c_level",
    "executive": "c_level",
    "exec": "c_level",
    "founder": "c_level",
    "co founder": "c_level",
    "cofounder": "c_level",
    "owner": "c_level",
    "partner": "c_level",
    "president": "c_level",
    "cxo": "c_level",
    "ceo": "c_level",
    "cto": "c_level",
    "cfo": "c_level",
    "coo": "c_level",
    "cmo": "c_level",
    "cro": "c_level",
    "cio": "c_level",
    "ciso": "c_level",
    "cdo": "c_level",
    "chro": "c_level",
    "vp": "vp",
    "svp": "vp",
    "evp": "vp",
    "avp": "vp",
    "vice president": "vp",
    "senior vice president": "vp",
    "executive vice president": "vp",
    "director": "director",
    "senior director": "director",
    "sr director": "director",
    "managing director": "director",
    # "Head of X" is the rung this mapping is most often asked about. Real
    # usage straddles director and VP — a Head of Revenue Operations at a
    # 1,000-person company usually outranks one at a 60-person company. It maps
    # to `director` (14.0) rather than `vp` (18.0) on this module's stated
    # conservative bias: the cost of the lower mapping is one extra human
    # review, the cost of the higher one is an unvalidated auto-route.
    "head": "director",
    "head of": "director",
    "manager": "manager",
    "senior manager": "manager",
    "sr manager": "manager",
    "group manager": "manager",
    "team lead": "manager",
    "lead": "manager",
    "supervisor": "manager",
    "principal": "manager",
    "staff": "manager",
    "ic": "ic",
    "individual contributor": "ic",
    "entry": "ic",
    "junior": "ic",
    "associate": "ic",
    "analyst": "ic",
    "specialist": "ic",
    "coordinator": "ic",
    "consultant": "ic",
    "engineer": "ic",
    "representative": "ic",
    "intern": "ic",
    "student": "ic",
    "trainee": "ic",
    # Apollo's own `seniority` enum uses "senior" for a senior IC, not for
    # management — mapping it to `manager` would inflate a large slice of every
    # person-enrichment response by 6 points.
    "senior": "ic",
}

# Ordered, because a title says several things at once: "VP of Engineering,
# Data" contains both "vp" and "engineer". Highest rung first, so the most
# senior token present wins.
_SENIORITY_TITLE_RULES = _compile_rules(
    (
        # "Vice President" contains "President", so this one VP form has to be
        # tested ahead of the C-level rule below or every VP in the pipeline
        # would read as C-level and collect 2 points it has not earned. The
        # remaining VP forms stay below C-level, where "most senior token
        # present wins" puts them: "Founder & VP Engineering" is a founder.
        (("vice president", "svp", "evp", "avp"), "vp"),
        (
            (
                "chief",
                "ceo",
                "cto",
                "cfo",
                "coo",
                "cmo",
                "cro",
                "cio",
                "ciso",
                "cdo",
                "chro",
                "founder",
                "co founder",
                "cofounder",
                "owner",
                "president",
                "partner",
                "c level",
                "c suite",
            ),
            "c_level",
        ),
        (("vp",), "vp"),
        (("head of", "head", "director"), "director"),
        (("manager", "mgr", "lead*", "supervisor", "principal", "staff"), "manager"),
        (
            (
                "associate",
                "analyst",
                "specialist",
                "coordinator",
                "engineer",
                "developer",
                "representative",
                "consultant",
                "intern",
                "student",
                "assistant",
                "administrator",
                "technician",
                "junior",
                "trainee",
            ),
            "ic",
        ),
    )
)


def normalize_seniority(raw: Any) -> str:
    """Map a provider's seniority token onto ``CANONICAL_SENIORITIES``.

    For a free-text job title, use :func:`seniority_from_title` instead — this
    function is for a vendor's own seniority *enum* (Apollo ships one), where
    an exact table is both sufficient and more auditable than title parsing.
    """
    key = canonical_key(raw)
    if not key:
        return UNKNOWN
    return _SENIORITY_ALIASES.get(key, UNKNOWN)


def seniority_from_title(title: Any) -> str:
    """Infer seniority from a free-text job title.

    Strictly a fallback for a provider that ships no seniority enum. Returns
    ``UNKNOWN`` rather than guessing ``ic`` for an unparseable title: "we could
    not read this title" is not evidence that the person is junior, and
    defaulting to the bottom rung would quietly reject every unusual title.
    """
    key = canonical_key(title)
    if not key:
        return UNKNOWN
    exact = _SENIORITY_ALIASES.get(key)
    if exact is not None:
        return exact
    return _first_match(key, _SENIORITY_TITLE_RULES) or UNKNOWN


# --- function -----------------------------------------------------------------

# Exactly ``arie.scoring.rules._FUNCTION_POINTS``'s keys, plus the sentinel.
# Note the deliberate absence of `revenue_operations` and `growth`, which the
# reference ICP names: adding them would create canonical values the scorer
# has no weight for, so they would score 0.0 while being *the ICP's own
# targets* — precisely the unknown-vs-negative confusion this layer exists to
# prevent. They fold into `operations` and `marketing`; see the alias table and
# ``arie.icp`` for the fold and why it is honest rather than lossy.
CANONICAL_FUNCTIONS: frozenset[str] = frozenset(
    {"data", "engineering", "operations", "marketing", "sales", "finance", "other", UNKNOWN}
)

_FUNCTION_ALIASES: dict[str, str] = {
    # --- sales ---------------------------------------------------------------
    "sales": "sales",
    "sales and business development": "sales",
    "business development": "sales",
    "bizdev": "sales",
    "account management": "sales",
    "account executive": "sales",
    "customer success": "sales",
    "partnerships": "sales",
    # --- operations (incl. the reference ICP's revenue_operations) -----------
    # RevOps is the reference ICP's highest-intent function and has no scorer
    # weight of its own. `operations` (9.0) is the closest modelled function
    # and the one a revops leader's remit actually sits inside.
    "operations": "operations",
    "revenue operations": "operations",
    "revops": "operations",
    "rev ops": "operations",
    "sales operations": "operations",
    "salesops": "operations",
    "marketing operations": "operations",
    "business operations": "operations",
    "bizops": "operations",
    "strategy and operations": "operations",
    "supply chain": "operations",
    "procurement": "operations",
    "program management": "operations",
    "project management": "operations",
    # --- marketing (incl. the reference ICP's growth) ------------------------
    # "Growth" is a marketing-owned discipline in the ICP's own framing
    # (demand generation, lifecycle, acquisition). `marketing` is 5.0.
    "marketing": "marketing",
    "growth": "marketing",
    "growth marketing": "marketing",
    "demand generation": "marketing",
    "demandgen": "marketing",
    "brand": "marketing",
    "communications": "marketing",
    "public relations": "marketing",
    "content": "marketing",
    "product marketing": "marketing",
    # --- data ----------------------------------------------------------------
    "data": "data",
    "data science": "data",
    "data engineering": "data",
    "data analytics": "data",
    "analytics": "data",
    "business intelligence": "data",
    "machine learning": "data",
    "artificial intelligence": "data",
    "research": "data",
    # --- engineering ---------------------------------------------------------
    "engineering": "engineering",
    "software engineering": "engineering",
    "development": "engineering",
    "devops": "engineering",
    "platform": "engineering",
    "infrastructure": "engineering",
    "security": "engineering",
    "information technology": "engineering",
    "it": "engineering",
    "technology": "engineering",
    "architecture": "engineering",
    "quality assurance": "engineering",
    # --- finance -------------------------------------------------------------
    "finance": "finance",
    "accounting": "finance",
    "financial planning and analysis": "finance",
    "treasury": "finance",
    "audit": "finance",
    "tax": "finance",
    # --- other ---------------------------------------------------------------
    # `other` is a *known* low-value function (2.0 points), never a stand-in
    # for "we could not parse this" — that is UNKNOWN's job.
    "other": "other",
    "human resources": "other",
    "hr": "other",
    "people": "other",
    "talent": "other",
    "recruiting": "other",
    "legal": "other",
    "compliance": "other",
    "product": "other",
    "product management": "other",
    "design": "other",
    "customer support": "other",
    "support": "other",
    "administrative": "other",
    "general management": "other",
    # Hunter/Clearbit `employment.role` enum values observed nowhere else.
    # (`canonical_key` folds the underscores: "customer_service" arrives here
    # as "customer service".) All land in `other` on the stated conservative
    # bias: each is a genuinely *known* function that is simply not an ICP
    # target — known-and-worth-little, not unknown.
    "customer service": "other",
    "consulting": "other",
    "education": "other",
    "health professional": "other",
    "real estate": "other",
}

# Ordered phrase rules over a free-text title. Order encodes precedence for
# compound titles: "VP Revenue Operations" must read as operations, not sales,
# so the operations phrases are tested before the bare "revenue"/"sales" ones.
_FUNCTION_TITLE_RULES = _compile_rules(
    (
        (
            (
                "revenue operations",
                "revops",
                "rev ops",
                "sales operations",
                "sales ops",
                "marketing operations",
                "marketing ops",
                "business operations",
                "bizops",
                "biz ops",
                "operations",
                "ops",
                "supply chain",
                "procurement",
                "logistics",
            ),
            "operations",
        ),
        (
            (
                "data",
                "analytics",
                "analysis",
                "business intelligence",
                "machine learning",
                "data science",
                "insights",
            ),
            "data",
        ),
        (
            (
                "engineering",
                "engineer",
                "developer",
                "software",
                "devops",
                "platform",
                "infrastructure",
                "security",
                "architect*",
                "technology",
                "technical",
                "it",
            ),
            "engineering",
        ),
        (
            (
                "marketing",
                "growth",
                "demand generation",
                "demand gen",
                "brand",
                "communications",
                "content",
                "seo",
            ),
            "marketing",
        ),
        (
            (
                "sales",
                "revenue",
                "business development",
                "account executive",
                "account",
                "customer success",
                "partnership*",
                "commercial",
            ),
            "sales",
        ),
        (
            ("finance", "financial", "accounting", "controller", "treasury", "audit", "tax"),
            "finance",
        ),
        (
            (
                "people",
                "human resources",
                "hr",
                "talent",
                "recruit*",
                "legal",
                "counsel",
                "compliance",
                "product",
                "design",
                "support",
                "administrative",
            ),
            "other",
        ),
    )
)


def normalize_function(raw: Any) -> str:
    """Map a provider's department/function token onto ``CANONICAL_FUNCTIONS``."""
    key = canonical_key(raw)
    if not key:
        return UNKNOWN
    return _FUNCTION_ALIASES.get(key, UNKNOWN)


def function_from_title(title: Any) -> str:
    """Infer function from a free-text job title.

    Same fallback status, and the same refusal to guess, as
    :func:`seniority_from_title`. Note that the scorer's ``other`` (2.0 points)
    is a *known* low-value function — it must never be used as a stand-in for
    "unparseable", which is what ``UNKNOWN`` is for.
    """
    key = canonical_key(title)
    if not key:
        return UNKNOWN
    exact = _FUNCTION_ALIASES.get(key)
    if exact is not None:
        return exact
    return _first_match(key, _FUNCTION_TITLE_RULES) or UNKNOWN


# --- employee_count -----------------------------------------------------------

# Numeric, so there is no taxonomy to map onto — only a validity range. Both
# bounds are deliberately wide: this is a sanity filter against a provider
# returning 0, -1, a placeholder, or a parsed-wrong string, not an ICP filter.
# The ICP's own 50-1,000 band lives in ``arie.icp``, and a 5-person company is
# a *known* poor fit (2.0 points), not an invalid reading.
EMPLOYEE_COUNT_MIN = 1
EMPLOYEE_COUNT_MAX = 10_000_000

# A token is a plain comma-grouped integer, optionally with a decimal
# fraction, optionally suffixed with a `k`/`m` order-of-magnitude marker
# ("10,000", "1.5", "10k", "1.5m") — one pattern for both range endpoints
# and the bare-number fallback, so "10K-50K" and "51-200" parse through the
# same path rather than the suffix form silently falling through to UNKNOWN.
_TOKEN_PATTERN = r"\d[\d,]*(?:\.\d+)?[km]?"
_TOKEN_RE = re.compile(r"^(\d[\d,]*(?:\.\d+)?)([km])?$")
_RANGE_RE = re.compile("^(" + _TOKEN_PATTERN + ")(?:-|to|\\u2013|\\u2014)(" + _TOKEN_PATTERN + ")$")
_PLUS_RE = re.compile("^(" + _TOKEN_PATTERN + r")\+$")

_UNIT_MULTIPLIERS: dict[str, int] = {"k": 1_000, "m": 1_000_000}


def _parse_employee_count_token(token: str) -> int | None:
    """One already-lowercased token to an int, or ``None`` if it isn't one.

    Never invents a number: a string that doesn't match the plain-or-k/m-
    suffixed numeric shape returns ``None`` rather than guessing.
    """
    match = _TOKEN_RE.match(token)
    if match is None:
        return None
    number_part, unit = match.groups()
    try:
        value = float(number_part.replace(",", ""))
    except (TypeError, ValueError, OverflowError):
        return None
    if unit is not None:
        value *= _UNIT_MULTIPLIERS[unit]
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def normalize_employee_count(raw: Any) -> int | str:
    """Validate a headcount, returning an ``int`` or ``UNKNOWN``.

    Accepts the shapes vendors actually ship — a number, a banded string
    (``"51-200"``), an open-ended band (``"1001+"``), and the same two forms
    with a `k`/`m` order-of-magnitude suffix (``"10K-50K"``, ``"1.5M+"``) —
    and returns ``UNKNOWN`` for anything else, including a value outside
    ``[EMPLOYEE_COUNT_MIN, EMPLOYEE_COUNT_MAX]``. A band collapses to its lower
    bound: ``"51-200"`` becomes 51 and ``"10K-50K"`` becomes 10000, the
    conservative reading — taking the midpoint of ``"201-1000"`` would
    silently move a company between the scorer's size tiers on a number no
    provider actually reported, and the same argument applies to a k/m band.

    ``UNKNOWN`` rather than ``0`` for a bad reading is the numeric half of this
    package's central distinction: a headcount of 0 is a *known* company size
    worth 0.0 points, and a provider that returns 0 has told us nothing.
    """
    if raw is None or isinstance(raw, bool):
        return UNKNOWN

    if isinstance(raw, int | float):
        try:
            value = int(float(raw))
        except (TypeError, ValueError, OverflowError):
            return UNKNOWN
        return value if EMPLOYEE_COUNT_MIN <= value <= EMPLOYEE_COUNT_MAX else UNKNOWN

    text = str(raw).strip().lower().replace(" ", "").replace("employees", "")
    if not text:
        return UNKNOWN

    band = _RANGE_RE.match(text) or _PLUS_RE.match(text)
    token = band.group(1) if band is not None else text

    parsed = _parse_employee_count_token(token)
    if parsed is None:
        return UNKNOWN
    return parsed if EMPLOYEE_COUNT_MIN <= parsed <= EMPLOYEE_COUNT_MAX else UNKNOWN
