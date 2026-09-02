import httpx
import pytest

from arie.config import FirecrawlConfig
from arie.discovery.providers import (
    MAX_LIMIT_PER_QUERY,
    DiscoveryProviderError,
    FakeDiscoveryProvider,
    FirecrawlDiscoveryProvider,
)


def test_fake_provider_is_deterministic() -> None:
    provider = FakeDiscoveryProvider()
    first = provider.search("multi-location gyms Australia", 5)
    second = provider.search("multi-location gyms Australia", 5)
    assert [c.url for c in first] == [c.url for c in second]
    assert len(first) == 5


def test_fake_provider_bounds_limit() -> None:
    provider = FakeDiscoveryProvider()
    assert len(provider.search("q", 1000)) == MAX_LIMIT_PER_QUERY
    assert len(provider.search("q", 0)) == 0


def test_fake_provider_different_queries_produce_different_results() -> None:
    provider = FakeDiscoveryProvider()
    a = provider.search("gyms", 3)
    b = provider.search("supplement distributors", 3)
    assert [c.url for c in a] != [c.url for c in b]


def test_firecrawl_provider_refuses_when_unconfigured() -> None:
    provider = FirecrawlDiscoveryProvider(config=FirecrawlConfig(api_key=""))
    with pytest.raises(DiscoveryProviderError):
        provider.search("gyms", 5)


def test_firecrawl_provider_isolates_transport_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    def _raise(*args: object, **kwargs: object) -> None:
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(httpx, "post", _raise)
    provider = FirecrawlDiscoveryProvider(config=FirecrawlConfig(api_key="k"))
    with pytest.raises(DiscoveryProviderError):
        provider.search("gyms", 5)


def test_firecrawl_provider_parses_a_real_shaped_response(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {
                "success": True,
                "data": [
                    {"url": "https://example.com", "title": "Example Co", "description": "d"},
                    {"url": "", "title": "no url, dropped"},
                ],
                "id": "abc",
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    provider = FirecrawlDiscoveryProvider(config=FirecrawlConfig(api_key="k"))
    results = provider.search("gyms", 5)
    assert len(results) == 1
    assert results[0].url == "https://example.com"
    # Discovery Quality Fix 1: with no site metadata on the row, the name is
    # derived from the domain — never from the row's `title`.
    assert results[0].company_name == "Example"


def test_firecrawl_provider_never_uses_a_page_title_as_a_company_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact regression the contactable-opportunity proof found: a blog
    post's headline stored and displayed as if it were the company."""

    class _Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {
                "success": True,
                "data": [
                    {
                        "url": "https://appinventiv.com/blog/ai-in-healthcare-in-australia/",
                        "title": "AI in Healthcare in Australia: Transforming Patient Care ...",
                        "description": "d",
                    }
                ],
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    provider = FirecrawlDiscoveryProvider(config=FirecrawlConfig(api_key="k"))
    (result,) = provider.search("healthcare operators australia", 5)
    assert result.company_name == "Appinventiv"
    assert "AI in Healthcare" not in result.company_name


def test_firecrawl_provider_prefers_explicit_site_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {
                "success": True,
                "data": [
                    {
                        "url": "https://exiq.com.au/industries/manufacturing/",
                        "title": "Manufacturing Digital Transformation & AI Consulting | ExIQ",
                        "description": "d",
                        "metadata": {"ogSiteName": "ExIQ"},
                    }
                ],
            }

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    provider = FirecrawlDiscoveryProvider(config=FirecrawlConfig(api_key="k"))
    (result,) = provider.search("manufacturers australia", 5)
    assert result.company_name == "ExIQ"
    assert result.site_name == "ExIQ"


def test_firecrawl_provider_raises_on_reported_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Resp:
        status_code = 200

        def json(self) -> dict:  # type: ignore[type-arg]
            return {"success": False}

    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp())
    provider = FirecrawlDiscoveryProvider(config=FirecrawlConfig(api_key="k"))
    with pytest.raises(DiscoveryProviderError):
        provider.search("gyms", 5)
