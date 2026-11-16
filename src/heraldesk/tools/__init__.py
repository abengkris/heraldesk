"""External tools used by agents, behind explicit interfaces."""

from __future__ import annotations

from heraldesk.tools.web_search import (
    DuckDuckGoHTMLSearch,
    FakeWebSearch,
    SearchError,
    SearchNotSupported,
    SearchResult,
    UnsafeURL,
    WebSearch,
    validate_public_https_url,
)

__all__ = [
    "DuckDuckGoHTMLSearch",
    "FakeWebSearch",
    "SearchError",
    "SearchNotSupported",
    "SearchResult",
    "UnsafeURL",
    "WebSearch",
    "validate_public_https_url",
]
