"""Researcher web search: fake provider and SSRF guard."""

from __future__ import annotations

import pytest

from heraldesk.tools.web_search import (
    DEFAULT_TIMEOUT_SECONDS,
    DuckDuckGoHTMLSearch,
    FakeWebSearch,
    SearchResult,
    UnsafeURL,
    validate_public_https_url,
)

pytestmark = pytest.mark.core


def test_fake_search_is_deterministic_and_records_queries() -> None:
    search = FakeWebSearch(
        results=(SearchResult(title="Source", url="https://example.com/a", snippet="Fact"),)
    )
    first = search.search("dashboard latency")
    second = search.search("dashboard latency")
    assert first == second
    assert first[0].url == "https://example.com/a"
    assert search.queries == ["dashboard latency", "dashboard latency"]


def test_fake_search_has_a_generated_result_when_none_are_configured() -> None:
    results = FakeWebSearch().search("pricing changes")
    assert len(results) == 1
    assert results[0].url.startswith("https://")


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/",
        "https://localhost/",
        "https://127.0.0.1/",
        "https://10.0.0.5/",
        "https://192.168.1.10/",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/",
        "https://user:pass@93.184.216.34/",
        "ftp://example.com/",
    ],
)
def test_ssrf_guard_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeURL):
        validate_public_https_url(url)


def test_ssrf_guard_accepts_a_public_https_url() -> None:
    assert validate_public_https_url("https://93.184.216.34/research") == (
        "https://93.184.216.34/research"
    )


def test_ssrf_guard_rejects_a_public_name_that_resolves_private(monkeypatch) -> None:
    monkeypatch.setattr(
        "heraldesk.tools.web_search._resolve",
        lambda host, port: ("10.0.0.5",),
    )
    with pytest.raises(UnsafeURL):
        validate_public_https_url("https://example.com/research")


def test_real_search_uses_a_ten_second_default_timeout() -> None:
    assert DEFAULT_TIMEOUT_SECONDS == 10.0
    assert DuckDuckGoHTMLSearch().timeout_seconds == 10.0
