"""Deterministic normalization for company and person identity fields.

Deliberately not fuzzy. Every function here is a pure string transform with no
similarity scoring, no thresholds, and no learned parameters — the M1 handoff
is explicit that identity resolution stays "deterministic domain/email
normalisation only" for now. Splink (or anything probabilistic) is deferred
until the ambiguous-identity measurement in
tests/integration/test_identity_resolution_integration.py shows deterministic
matching failing often enough to justify it.
"""

from __future__ import annotations

import re

# Common B2B free-mail providers. A person's email domain is a strong signal
# for their *company's* domain (see domain_from_email) — except for these,
# where it would instead merge every Gmail user on earth into one "company."
FREE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "gmail.com",
        "yahoo.com",
        "outlook.com",
        "hotmail.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "protonmail.com",
        "proton.me",
        "live.com",
        "msn.com",
        "gmx.com",
        "mail.com",
        "yandex.com",
        "zoho.com",
    }
)

# Legal-entity suffixes stripped from the end of a company name before
# comparison, longest-and-most-specific first so "incorporated" doesn't leave
# a dangling "in" if a shorter prefix were tried first. Built independently of
# any single dataset's vocabulary — this is a general business-name list, not
# reverse-engineered from a specific test fixture.
LEGAL_SUFFIX_WORDS: tuple[str, ...] = (
    "incorporated",
    "corporation",
    "limited",
    "company",
    "inc",
    "corp",
    "co",
    "ltd",
    "llc",
    "llp",
    "lp",
    "plc",
    "gmbh",
    "ag",
    "sa",
    "nv",
    "bv",
)

_URL_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]")
_WHITESPACE_RE = re.compile(r"\s+")


def _require_nonblank(raw: str, what: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError(f"{what} must not be blank")
    return value


def normalize_domain(raw: str) -> str:
    """Strip scheme, path, port, and a leading ``www.``; lowercase the rest.

    ``https://www.Acme.com/pricing?ref=abc``, ``ACME.COM``, and
    ``acme.com:443`` all normalize to ``acme.com``.
    """
    value = _require_nonblank(raw, "domain").lower()
    value = _URL_SCHEME_RE.sub("", value)
    value = value.split("/", 1)[0]
    value = value.split("?", 1)[0]
    value = value.split(":", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    return _require_nonblank(value, "domain")


def normalize_email(raw: str) -> str:
    """Lowercase, trim, and drop ``+tag`` sub-addressing from the local part.

    Dot-folding (``john.doe`` == ``johndoe``) is deliberately *not* applied —
    that's a Gmail-specific convention, not a general email rule, and applying
    it universally would incorrectly merge distinct mailboxes on mail systems
    where dots are significant.
    """
    value = _require_nonblank(raw, "email").lower()
    local, sep, domain = value.partition("@")
    if not sep:
        raise ValueError(f"not an email address: {raw!r}")
    local = local.split("+", 1)[0]
    if not local:
        raise ValueError(f"not an email address: {raw!r}")
    return f"{local}@{normalize_domain(domain)}"


def domain_from_email(email: str) -> str | None:
    """The email's domain, unless it's a free-mail provider.

    ``None`` signals "this email carries no usable company signal" — the
    caller should fall back to an explicit domain or a bare name instead of
    treating (say) every ``gmail.com`` sender as one company.
    """
    normalized = normalize_email(email)
    domain = normalized.rsplit("@", 1)[1]
    return None if domain in FREE_EMAIL_DOMAINS else domain


def normalize_company_name(raw: str) -> str:
    """Fold case/punctuation and strip trailing legal-entity suffixes.

    ``"Acme Inc."``, ``"ACME"``, and ``"Acme Corporation"`` all normalize to
    ``"acme"``. This is the best-effort key used *only* when no domain is
    available — see 0003_identity_resolution.sql for why it's not a unique
    constraint.
    """
    value = _require_nonblank(raw, "company name").lower()
    value = value.replace("&", " and ")
    value = _NON_ALNUM_RE.sub(" ", value)
    words = _WHITESPACE_RE.split(value.strip())

    while words and words[-1] in LEGAL_SUFFIX_WORDS:
        words.pop()

    normalized = " ".join(words)
    return _require_nonblank(normalized, "company name")
