"""Turning a URL into the identity a discovery candidate is deduplicated on.

Domain is the strongest identity a search result carries — two results for
`example.com` and `www.example.com/?utm_source=x` are the same company, and
`example.com` and `example.co` are not. This module draws that line once so
`arie.discovery.orchestrator` never has to.
"""

from __future__ import annotations

from urllib.parse import urlparse

from arie.discovery.models import DiscoveryCandidate, RawDiscoveryCandidate

__all__ = ["canonical_domain", "dedupe_candidates", "dedupe_raw_candidates"]

_DROP_PREFIXES = ("www.", "m.", "web.")


def canonical_domain(url_or_domain: str) -> str | None:
    """`https://WWW.Example.com/about?x=1` -> `example.com`. `None` for
    anything that doesn't resolve to a host — a candidate with no domain
    cannot be deduplicated or promoted, and callers must treat it that way
    rather than inventing a placeholder key."""
    raw = url_or_domain.strip()
    if not raw:
        return None
    parsed = urlparse(raw if "//" in raw else f"//{raw}")
    host = (parsed.hostname or "").lower().strip(".")
    if not host or "." not in host:
        return None
    for prefix in _DROP_PREFIXES:
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix) :]
            break
    return host or None


def dedupe_raw_candidates(
    candidates: list[RawDiscoveryCandidate],
) -> list[tuple[str, RawDiscoveryCandidate]]:
    """First-seen-wins domain dedupe over freshly discovered results, before
    any of them have a `candidate_id` yet. Order-preserving, so the first
    query that surfaced a domain is the one credited with `search_query`."""
    seen: dict[str, RawDiscoveryCandidate] = {}
    for candidate in candidates:
        domain = canonical_domain(candidate.url)
        if domain is None or domain in seen:
            continue
        seen[domain] = candidate
    return list(seen.items())


def dedupe_candidates(candidates: list[DiscoveryCandidate]) -> list[DiscoveryCandidate]:
    """Same rule, for already-materialised candidates — used by tests and by
    any caller re-deduplicating a persisted set."""
    seen: dict[str, DiscoveryCandidate] = {}
    for candidate in candidates:
        if candidate.domain not in seen:
            seen[candidate.domain] = candidate
    return list(seen.values())
