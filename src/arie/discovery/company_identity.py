"""What a discovered company is actually *called* — Discovery Quality Fix 1.

A web search result's `title` is the title of a *page*, not the name of a
company. The contactable-opportunity proof surfaced exactly that failure:
"AI in Healthcare in Australia: Transforming Patient Care ..." was stored and
displayed as a company name, when the company was Appinventiv and the title
belonged to one blog post on its site.

So the page title is not in the resolution chain at all. Three sources, in
descending order of how directly they assert an identity:

1. **Explicit provider/company metadata** — `og:site_name` and friends, which
   a site publishes to say what the site itself is called, independent of
   which page was fetched.
2. **Verified site identity** — the name ARIE's own website verification read
   off the company's own homepage (`VerifiedCompanyFacts.company_name_on_site`).
3. **Canonical domain-derived name** — always available, never wrong about
   *which* company it refers to, and honest about being a fallback.

Every candidate name from (1) and (2) must survive `looks_like_a_company_name`
before it is used: an `og:site_name` set to a headline, or a model echoing a
page title, falls through to the domain rather than reintroducing the bug this
module exists to fix.
"""

from __future__ import annotations

import re

__all__ = [
    "domain_derived_name",
    "looks_like_a_company_name",
    "resolve_company_name",
]

_MAX_NAME_CHARS = 60
_MAX_NAME_WORDS = 6

_HEADLINE_PUNCTUATION_RE = re.compile(
    r"[|:?!•…]"  # pipe, colon, question, bang, bullet, ellipsis
    r"|—"  # em dash
    r"|\s[-\u2013]\s"  # spaced hyphen or en dash
    r"|\.\.\."
)
"""Punctuation a company name does not contain but a page title reliably does
("Logistics Software Development Australia | ISH 2026 Brisbane")."""

_HEADLINE_WORDS = frozenset(
    {
        "how",
        "why",
        "what",
        "when",
        "top",
        "best",
        "guide",
        "review",
        "reviews",
        "blog",
        "news",
        "tips",
        "vs",
        "welcome",
        "home",
        "homepage",
        "untitled",
    }
)
"""A first word that makes the string a headline or a placeholder, not a name."""

_PUBLIC_SUFFIX_LABELS = frozenset(
    {
        "com",
        "net",
        "org",
        "co",
        "io",
        "ai",
        "app",
        "dev",
        "biz",
        "info",
        "gov",
        "edu",
        "ac",
        "au",
        "nz",
        "uk",
        "us",
        "ca",
        "de",
        "fr",
        "nl",
        "sg",
        "in",
        "ie",
        "za",
    }
)
"""Enough of the public suffix list to split `exiq.com.au` into `exiq`.
Deliberately not the full PSL: an unknown suffix simply leaves one extra
label in the name, which is a cosmetic miss, never a wrong company."""

_WORD_SPLIT_RE = re.compile(r"[-_]+")
_WHITESPACE_RE = re.compile(r"\s+")


def looks_like_a_company_name(value: str | None) -> bool:
    """Conservative: reject anything that reads like a page title, a slogan,
    or a placeholder. A rejected candidate costs a fallback to the domain
    name, which is always safe; an accepted headline is the original bug."""
    if value is None:
        return False
    name = _WHITESPACE_RE.sub(" ", value).strip()
    if not name or len(name) > _MAX_NAME_CHARS:
        return False
    if _HEADLINE_PUNCTUATION_RE.search(name) is not None:
        return False
    words = name.split(" ")
    if len(words) > _MAX_NAME_WORDS:
        return False
    if words[0].lower().strip(".,") in _HEADLINE_WORDS:
        return False
    # Needs at least one letter — "2026", "###" and friends are not names.
    return any(char.isalpha() for char in name)


def domain_derived_name(domain: str) -> str:
    """`ishtechnologies.com.au` -> `Ishtechnologies`; `design-pluz.com.au` ->
    `Design Pluz`. Never empty for a domain that has any label at all, so
    `resolve_company_name` always has a final answer."""
    host = domain.strip().lower().strip(".")
    if not host:
        return "Unknown company"
    labels = [label for label in host.split(".") if label]
    while len(labels) > 1 and labels[-1] in _PUBLIC_SUFFIX_LABELS:
        labels.pop()
    label = labels[-1] if labels else host
    words = [word for word in _WORD_SPLIT_RE.split(label) if word]
    if not words:
        return "Unknown company"
    return " ".join(word[:1].upper() + word[1:] for word in words)


def resolve_company_name(
    *,
    provider_site_name: str | None,
    verified_site_name: str | None,
    domain: str,
) -> str:
    """The chain in the module docstring, applied once. Note what is *not* a
    parameter: the search result's page title."""
    for candidate in (provider_site_name, verified_site_name):
        if looks_like_a_company_name(candidate):
            assert candidate is not None  # looks_like_a_company_name rejects None
            return _WHITESPACE_RE.sub(" ", candidate).strip()
    return domain_derived_name(domain)
