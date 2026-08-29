"""Provider-independent identity validation — did a person-provider answer
about the *intended* person, or merely about *some real person* at that
address?

**The case this module exists for.** Hunter Combined Enrichment, asked about
``patrick@stripe.com``, correctly and honestly resolved that literal mailbox
to its real owner: Patrick Bosmans, an IT Administrator at Stripe. Nothing
about that response is wrong — Hunter did exactly what Combined Enrichment
promises, resolve an email to its assigned owner. What is wrong is treating
"a real person was found at the right company" as equivalent to "the person
ARIE meant was found": those are different claims, and only a validator that
compares the *requested* identity against the *returned* one can tell them
apart. An email-domain check alone cannot — the domain agrees in exactly the
case that fooled a human reader too.

**Deterministic, not fuzzy — the same discipline `arie.identity.normalize`
already commits to.** No similarity score, no edit-distance threshold, no
nickname table. A name either resolves to the same (first, last) token pair
after Unicode-fold and punctuation-strip, or it does not; there is no partial
credit that could quietly wave a mismatch through. Where there is nothing to
compare (most cold inbound leads carry no ``full_name`` before enrichment —
that field usually doesn't exist until a provider fills it in), the verdict
says so honestly (``UNVERIFIABLE``/``PROBABLE``) rather than defaulting to
either extreme.

**What this module does not do.** It never decides *which* vendor is right,
never scores a lead, and never persists anything — it is a pure function from
two small, provider-agnostic records to a verdict and its reasons. The caller
(``arie.jobs.handlers``) decides what a ``MISMATCH`` means for evidence and
the receipt.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from arie.identity.normalize import normalize_domain, normalize_email

__all__ = [
    "MISMATCH",
    "PROBABLE",
    "UNVERIFIABLE",
    "VERIFIED",
    "IdentityValidation",
    "IdentityVerdict",
    "RequestedIdentity",
    "ReturnedIdentity",
    "validate_identity",
]

IdentityVerdict = Literal["VERIFIED", "PROBABLE", "MISMATCH", "UNVERIFIABLE"]

VERIFIED: IdentityVerdict = "VERIFIED"
"""At least two independent signals corroborate — currently domain + name —
and none disagree. The strongest claim this module makes."""

PROBABLE: IdentityVerdict = "PROBABLE"
"""Exactly one signal corroborates and nothing disagrees, but there was no
second, independent signal available to rule out a same-company (or
same-local-part) wrong-person match — the Patrick Bosmans failure mode is
structurally invisible to a single-signal check."""

MISMATCH: IdentityVerdict = "MISMATCH"
"""At least one signal — domain, email, or name — actively disagrees. Never
downgraded by an agreeing signal elsewhere: one contradiction is disqualifying
by design (see the module docstring's "not aggressively fuzzy" argument)."""

UNVERIFIABLE: IdentityVerdict = "UNVERIFIABLE"
"""Nothing comparable was available on both sides — most commonly because the
lead source never supplied an expected name and the provider's response
carried no employer/domain to check either. Not a claim that the match is
bad; a claim that ARIE cannot tell."""

_SIGNAL = Literal["match", "mismatch", "unknown"]


@dataclass(frozen=True)
class RequestedIdentity:
    """Who ARIE asked a provider about — whatever the lead's own source knew
    *before* enrichment.

    Most cold inbound leads carry only ``email``; ``full_name`` is usually
    unset because nothing upstream of enrichment knew it yet. When a lead
    source (a CRM import, a form with a name field) does supply one, this is
    the one signal that can catch a same-domain, wrong-person match — without
    it, that specific failure mode cannot be detected by any provider-
    independent check.
    """

    email: str
    company_domain: str | None = None
    full_name: str | None = None


@dataclass(frozen=True)
class ReturnedIdentity:
    """Who a provider says answered the lookup. Every field optional — a
    provider returning less than it could is ordinary, not an error."""

    full_name: str | None = None
    email: str | None = None
    employer_domain: str | None = None
    employer_name: str | None = None


@dataclass(frozen=True)
class IdentityValidation:
    verdict: IdentityVerdict
    reasons: tuple[str, ...]
    """Ordered, human-readable clauses — exactly what produced the verdict.
    Carried into the receipt so a reviewer reads a sentence, not a code."""

    def explain(self) -> str:
        return "; ".join(self.reasons) if self.reasons else "no comparable signal was available"


_NON_WORD_RE = re.compile(r"[^\w\s]", re.UNICODE)


def _name_key(raw: str) -> tuple[str, str] | None:
    """A name to its (first-token, last-token) key, or ``None`` if that key
    isn't well-formed.

    Unicode-fold (NFKD, strip combining marks) before tokenizing, so
    "Tobi Lütke" and an ASCII-transliterated "Tobi Lutke" compare equal — a
    transliteration difference is not evidence of a different person, and
    treating it as one would be a false mismatch this module must not
    manufacture. Middle names/initials fall out of the comparison entirely by
    construction (only the first and last token are kept), which is what
    keeps "Patrick Collison" and "Patrick J. Collison" the same key while
    still telling "Patrick Collison" and "Patrick Bosmans" apart.
    """
    folded = unicodedata.normalize("NFKD", raw)
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    tokens = [token for token in _NON_WORD_RE.sub(" ", folded.lower()).split() if token]
    if len(tokens) < 2:
        return None
    return (tokens[0], tokens[-1])


def _name_signal(requested: RequestedIdentity, returned: ReturnedIdentity) -> _SIGNAL:
    if requested.full_name is None or returned.full_name is None:
        return "unknown"
    requested_key = _name_key(requested.full_name)
    returned_key = _name_key(returned.full_name)
    if requested_key is None or returned_key is None:
        return "unknown"
    return "match" if requested_key == returned_key else "mismatch"


def _expected_domain(requested: RequestedIdentity) -> str | None:
    if requested.company_domain:
        return requested.company_domain
    _, _, domain = requested.email.partition("@")
    return domain or None


def _domain_signal(requested: RequestedIdentity, returned: ReturnedIdentity) -> _SIGNAL:
    if not returned.employer_domain:
        return "unknown"
    expected = _expected_domain(requested)
    if not expected:
        return "unknown"
    try:
        return (
            "match"
            if normalize_domain(returned.employer_domain) == normalize_domain(expected)
            else "mismatch"
        )
    except ValueError:
        return "unknown"


def _email_signal(requested: RequestedIdentity, returned: ReturnedIdentity) -> _SIGNAL:
    """Almost always 'match' for an email-keyed lookup (the provider is asked
    about this exact address) — kept anyway so a future name-keyed provider,
    which could legitimately return a *different* email, is checked on the
    same terms rather than assumed correct by construction."""
    if not returned.email:
        return "unknown"
    try:
        return (
            "match"
            if normalize_email(returned.email) == normalize_email(requested.email)
            else "mismatch"
        )
    except ValueError:
        return "unknown"


def validate_identity(
    requested: RequestedIdentity, returned: ReturnedIdentity
) -> IdentityValidation:
    """Compare what ARIE asked for against what a provider returned.

    Checks three signals — email, employer domain, and (first, last) name —
    but they do not all count the same way:

    * Any signal that actively **disagrees** (domain, email, or name) makes
      the verdict ``MISMATCH``, regardless of what else agrees. A single
      contradiction is disqualifying by design: this is the check that must
      catch "same company, different person," and an agreeing domain must
      never be allowed to outvote a disagreeing name.
    * Only **domain** and **name** agreement count toward ``VERIFIED``/
      ``PROBABLE``. An agreeing **email** is not independent corroboration
      for the person-providers this module was built for (Hunter, Apollo):
      both are queried *by* the requested email, so the response almost
      always echoes it back — treating that echo as a second vote would make
      "domain matches" alone read as ``VERIFIED`` for every ordinary lookup,
      exactly the false confidence this module exists to prevent. Email
      agreement still appears in ``reasons`` for the audit trail, and an
      email *mismatch* still disqualifies — only agreement is discounted.
    * **Both** domain and name agreeing, neither disagreeing, is ``VERIFIED``.
    * **Exactly one** of domain/name agreeing, neither disagreeing, is
      ``PROBABLE`` — a single corroborating signal cannot rule out a
      same-domain wrong-person match on its own. This is the common real
      shape: most cold leads carry no expected name, so a domain-only match
      caps here, never at ``VERIFIED``.
    * **Nothing comparable** on either side is ``UNVERIFIABLE``.
    """
    if returned.full_name is None and returned.employer_domain is None and returned.email is None:
        return IdentityValidation(UNVERIFIABLE, ("provider returned no identity to compare",))

    domain = _domain_signal(requested, returned)
    email = _email_signal(requested, returned)
    name = _name_signal(requested, returned)

    reasons: list[str] = []
    if domain == "match":
        reasons.append("employer domain matches the requested company")
    elif domain == "mismatch":
        reasons.append("employer domain does not match the requested company")
    if email == "match":
        reasons.append("returned email matches the requested email (not independent corroboration)")
    elif email == "mismatch":
        reasons.append("returned email does not match the requested email")
    if name == "match":
        reasons.append("returned name matches the expected name")
    elif name == "mismatch":
        reasons.append("returned name does not match the expected name")

    if "mismatch" in (domain, email, name):
        return IdentityValidation(MISMATCH, tuple(reasons))

    agreeing = sum(1 for signal in (domain, name) if signal == "match")
    if agreeing >= 2:
        return IdentityValidation(VERIFIED, tuple(reasons))
    if agreeing == 1:
        reasons.append("no independent second signal was available to corroborate")
        return IdentityValidation(PROBABLE, tuple(reasons))
    return IdentityValidation(UNVERIFIABLE, ("no comparable signal was available on both sides",))
