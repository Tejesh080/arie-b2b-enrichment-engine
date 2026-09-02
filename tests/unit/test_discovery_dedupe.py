from arie.discovery.dedupe import canonical_domain, dedupe_raw_candidates
from arie.discovery.models import RawDiscoveryCandidate


def test_canonical_domain_strips_scheme_www_path_and_query() -> None:
    assert canonical_domain("https://www.example.com/about?x=1") == "example.com"
    assert canonical_domain("http://example.com/") == "example.com"
    assert canonical_domain("example.com") == "example.com"
    assert canonical_domain("https://m.example.com") == "example.com"


def test_canonical_domain_distinguishes_different_real_domains() -> None:
    assert canonical_domain("https://example.com") != canonical_domain("https://example.co")


def test_canonical_domain_none_for_unparseable_input() -> None:
    assert canonical_domain("") is None
    assert canonical_domain("not a url or domain") is None


def _raw(url: str, name: str = "Acme") -> RawDiscoveryCandidate:
    return RawDiscoveryCandidate(
        company_name=name, url=url, snippet="", source_provider="fake", search_query="q"
    )


def test_dedupe_raw_candidates_collapses_tracking_and_www_variants() -> None:
    items = [
        _raw("https://example.com"),
        _raw("https://www.example.com/?utm_source=x"),
        _raw("https://example.com/about"),
        _raw("https://other.com"),
    ]
    deduped = dedupe_raw_candidates(items)
    domains = [d for d, _ in deduped]
    assert domains == ["example.com", "other.com"]


def test_dedupe_raw_candidates_keeps_first_seen() -> None:
    items = [_raw("https://example.com", name="First"), _raw("https://example.com", name="Second")]
    deduped = dedupe_raw_candidates(items)
    assert deduped[0][1].company_name == "First"


def test_dedupe_raw_candidates_drops_candidates_with_no_domain() -> None:
    items = [_raw(""), _raw("https://example.com")]
    deduped = dedupe_raw_candidates(items)
    assert [d for d, _ in deduped] == ["example.com"]
