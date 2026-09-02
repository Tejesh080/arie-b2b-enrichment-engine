"""Discovery providers — turning one search query into candidate companies.

Search APIs, not blind crawling: a provider returns a company name, a URL,
and a snippet, and that is the entire surface `arie.discovery.orchestrator`
trusts it for. Nothing here fetches a company's own website — see the Pivot
handoff's "Known limitations" for that deliberate scope cut.

Two implementations. `FakeDiscoveryProvider` is deterministic (seeded from
the query text) and makes no network call — the only provider the test suite
or a keyless developer machine ever exercises. `FirecrawlDiscoveryProvider`
is the first real one, wired to Firecrawl's `POST /v1/search` (verified live
against the vendor 2026-09-03: bearer auth, `{"query","limit"}` in,
`{"success","data":[{"url","title","description"}]}` out).
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Protocol

import httpx

from arie.config import FIRECRAWL, FirecrawlConfig
from arie.discovery.models import RawDiscoveryCandidate

__all__ = [
    "DiscoveryProvider",
    "DiscoveryProviderError",
    "FakeDiscoveryProvider",
    "FirecrawlDiscoveryProvider",
    "build_discovery_provider",
]

_LOGGER = logging.getLogger("arie.discovery.providers")

MAX_LIMIT_PER_QUERY = 25
"""A search call is bounded regardless of what a caller asks for — the same
defence-in-depth every provider adapter in this codebase applies to its own
inputs."""


class DiscoveryProviderError(RuntimeError):
    """A provider call failed outright (transport error, malformed
    response). Callers isolate this per-query — one bad query must not fail
    an entire discovery run; see `arie.discovery.orchestrator`."""


class DiscoveryProvider(Protocol):
    @property
    def name(self) -> str: ...

    def search(self, query: str, limit: int) -> list[RawDiscoveryCandidate]: ...


_FAKE_COMPANY_WORDS = (
    "Northwind",
    "Summit",
    "Bluepeak",
    "Ironbark",
    "Harborline",
    "Redwood",
    "Fieldstone",
    "Meridian",
    "Coastal",
    "Anchor",
    "Solstice",
    "Vantage",
    "Ridgeline",
    "Lighthouse",
    "Beacon",
    "Cedar",
    "Outback",
    "Pinnacle",
    "Riverside",
    "Sundial",
)
_FAKE_COMPANY_SUFFIXES = ("Group", "Co", "Holdings", "Partners", "Ventures", "Industries")


def _seed_from(*parts: str) -> int:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


@dataclass(frozen=True)
class FakeDiscoveryProvider:
    """Deterministic, bounded, no network I/O — same `(query, limit)` always
    produces the same candidates, so a test asserting funnel counts never
    flakes. Every candidate carries real (if synthetic) provenance: a
    reproducible `.test` domain and a snippet that says plainly what it is,
    never something that could be mistaken for a real company."""

    name: str = "fake_discovery"

    def search(self, query: str, limit: int) -> list[RawDiscoveryCandidate]:
        bounded = max(0, min(limit, MAX_LIMIT_PER_QUERY))
        candidates: list[RawDiscoveryCandidate] = []
        for index in range(bounded):
            seed = _seed_from("fake-discovery", query, str(index))
            word = _FAKE_COMPANY_WORDS[seed % len(_FAKE_COMPANY_WORDS)]
            suffix = _FAKE_COMPANY_SUFFIXES[
                (seed // len(_FAKE_COMPANY_WORDS)) % len(_FAKE_COMPANY_SUFFIXES)
            ]
            slug = f"{word.lower()}-{suffix.lower()}-{seed % 10_000}"
            company_name = f"{word} {suffix}"
            candidates.append(
                RawDiscoveryCandidate(
                    company_name=company_name,
                    url=f"https://{slug}.example-test.invalid",
                    snippet=f"Simulated search result for '{query}' — deterministic test fixture.",
                    source_provider=self.name,
                    search_query=query,
                )
            )
        return candidates


@dataclass(frozen=True)
class FirecrawlDiscoveryProvider:
    """The first real discovery provider — a plain search API call, no
    crawling. Raises `DiscoveryProviderError` for any transport or shape
    failure; never leaks `config.api_key` into that message."""

    config: FirecrawlConfig = FIRECRAWL
    name: str = "firecrawl_search"
    timeout_seconds: float | None = None

    def search(self, query: str, limit: int) -> list[RawDiscoveryCandidate]:
        if not self.config.configured:
            raise DiscoveryProviderError("Firecrawl is not configured (FIRECRAWL_API_KEY unset)")
        bounded = max(1, min(limit, MAX_LIMIT_PER_QUERY))
        try:
            response = httpx.post(
                self.config.base_url,
                headers={
                    "Authorization": f"Bearer {self.config.api_key}",
                    "Content-Type": "application/json",
                },
                json={"query": query, "limit": bounded},
                timeout=self.timeout_seconds or self.config.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise DiscoveryProviderError(f"Firecrawl search transport error: {exc}") from exc

        if response.status_code != 200:
            raise DiscoveryProviderError(f"Firecrawl search returned HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError as exc:
            raise DiscoveryProviderError("Firecrawl search returned a non-JSON response") from exc

        if not isinstance(body, dict) or not body.get("success", False):
            raise DiscoveryProviderError("Firecrawl search reported failure")
        rows = body.get("data")
        if not isinstance(rows, list):
            raise DiscoveryProviderError("Firecrawl search response missing 'data'")

        candidates: list[RawDiscoveryCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            url = str(row.get("url") or "").strip()
            if not url:
                continue
            title = str(row.get("title") or url).strip()[:300]
            description = str(row.get("description") or "").strip()[:500]
            candidates.append(
                RawDiscoveryCandidate(
                    company_name=title,
                    url=url,
                    snippet=description,
                    source_provider=self.name,
                    search_query=query,
                )
            )
        return candidates


def build_discovery_provider() -> DiscoveryProvider:
    """`FirecrawlDiscoveryProvider` when a real key is configured, else the
    fake — the same "real if configured, deterministic fallback otherwise"
    rule `arie.llm.factory.build_llm_provider` already applies to models."""
    if FIRECRAWL.configured:
        return FirecrawlDiscoveryProvider()
    _LOGGER.info("FIRECRAWL_API_KEY not set — discovery will use the fake provider")
    return FakeDiscoveryProvider()
