"""Web search behind a small interface, with an offline fake.

The real provider is DuckDuckGo's HTML endpoint, which needs no API key. Every outbound
URL passes through :func:`validate_public_https_url`:

- HTTPS only;
- no username or password in the URL;
- localhost, private, loopback, link-local, reserved and metadata addresses are refused;
- the hostname is resolved first, so a public name pointing at a private address is refused;
- requests use a 10 second timeout by default.

The fake provider never touches the network and is what tests and ``search_provider: auto``
with the fake LLM use.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Final, Protocol
from urllib.parse import parse_qs, quote_plus, urljoin, urlsplit
from urllib.request import Request, urlopen

DEFAULT_TIMEOUT_SECONDS: Final = 10.0
MAX_RESPONSE_BYTES: Final = 1_000_000
BLOCKED_HOSTNAMES: Final[frozenset[str]] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "ip6-localhost",
        "ip6-loopback",
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
        "metadata",
    }
)


class SearchError(Exception):
    """Base class for search provider failures."""


class SearchNotSupported(SearchError):
    """Raised when a provider cannot serve a query without violating its terms."""


class UnsafeURL(SearchError):
    """Raised when a URL fails the SSRF guard."""


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One search result: title, URL and snippet."""

    title: str
    url: str
    snippet: str = ""


class WebSearch(Protocol):
    """The only search contract an agent or pipeline may depend on."""

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Return at most *max_results* results or raise :class:`SearchError`."""
        ...


def _blocked_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.strip("[]"))
    except ValueError:
        return False
    return any(
        (
            address.is_private,
            address.is_loopback,
            address.is_link_local,
            address.is_multicast,
            address.is_reserved,
            address.is_unspecified,
        )
    )


def _resolve(host: str, port: int) -> tuple[str, ...]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SearchError(f"cannot resolve search host {host}: {exc}") from exc
    addresses: list[str] = []
    for info in infos:
        sockaddr = info[4]
        if sockaddr and isinstance(sockaddr[0], str):
            addresses.append(sockaddr[0])
    return tuple(addresses)


def validate_public_https_url(url: str) -> str:
    """Return *url* unchanged when it is a public HTTPS URL, else raise :class:`UnsafeURL`."""
    candidate = url.strip()
    if not candidate:
        raise UnsafeURL("URL must not be empty")
    parts = urlsplit(candidate)
    if parts.scheme.lower() != "https":
        raise UnsafeURL("only https URLs are allowed")
    if parts.username is not None or parts.password is not None:
        raise UnsafeURL("URL must not contain credentials")
    host = (parts.hostname or "").strip().lower().rstrip(".")
    if not host:
        raise UnsafeURL("URL must contain a hostname")
    if host in BLOCKED_HOSTNAMES or host.endswith(".localhost"):
        raise UnsafeURL(f"host {host!r} is not allowed")
    try:
        port = parts.port or 443
    except ValueError as exc:
        raise UnsafeURL("URL contains an invalid port") from exc
    if _blocked_ip(host):
        raise UnsafeURL(f"address {host!r} is private or reserved")
    for address in _resolve(host, port):
        if _blocked_ip(address):
            raise UnsafeURL(f"host {host!r} resolves to a private or reserved address")
    return candidate


@dataclass
class FakeWebSearch:
    """Deterministic, network-free search used by tests and fake-provider runs."""

    results: tuple[SearchResult, ...] = ()
    queries: list[str] = field(default_factory=list)

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Record the query and return configured results or a stable synthetic list."""
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        self.queries.append(query)
        if self.results:
            return list(self.results[:max_results])
        label = " ".join(query.split())[:120] or "research"
        return [
            SearchResult(
                title=f"Background on {label}",
                url="https://example.com/research",
                snippet=f"Offline research note for {label}.",
            )
        ][:max_results]


class _DuckDuckGoParser(HTMLParser):
    """Extract result anchors and snippets from DuckDuckGo's HTML page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.results: list[SearchResult] = []
        self._title_parts: list[str] | None = None
        self._snippet_parts: list[str] | None = None
        self._current_title = ""
        self._current_url = ""
        self._current_snippet = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        attributes = {key: value or "" for key, value in attrs}
        classes = attributes.get("class", "").split()
        if "result__a" in classes:
            self._title_parts = []
            self._current_url = attributes.get("href", "")
        elif "result__snippet" in classes:
            self._snippet_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag != "a":
            return
        if self._title_parts is not None:
            self._current_title = "".join(self._title_parts).strip()
            if self._current_title and self._current_url:
                self.results.append(
                    SearchResult(
                        title=self._current_title,
                        url=self._clean_url(self._current_url),
                        snippet=self._current_snippet,
                    )
                )
            self._title_parts = None
        if self._snippet_parts is not None:
            self._current_snippet = "".join(self._snippet_parts).strip()
            self._snippet_parts = None

    def handle_data(self, data: str) -> None:
        if self._title_parts is not None:
            self._title_parts.append(data)
        if self._snippet_parts is not None:
            self._snippet_parts.append(data)

    def _clean_url(self, value: str) -> str:
        absolute = urljoin("https://duckduckgo.com", value)
        parts = urlsplit(absolute)
        query = parse_qs(parts.query)
        target = query.get("uddg")
        return target[0] if target else absolute


@dataclass(frozen=True, slots=True)
class DuckDuckGoHTMLSearch:
    """Keyless DuckDuckGo HTML search with the SSRF guard and a hard timeout."""

    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Search DuckDuckGo and return parsed result links."""
        if max_results < 1:
            raise ValueError("max_results must be at least 1")
        url = "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
        validate_public_https_url(url)
        request = Request(
            url,
            headers={
                "User-Agent": "Heraldesk/0.1 (self-hosted research agent)",
                "Accept": "text/html",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                status = getattr(response, "status", 200)
                if status != 200:
                    raise SearchNotSupported(f"DuckDuckGo returned HTTP {status}")
                payload = response.read(MAX_RESPONSE_BYTES)
        except SearchError:
            raise
        except OSError as exc:
            raise SearchError(f"search request failed: {exc}") from exc
        parser = _DuckDuckGoParser()
        parser.feed(payload.decode("utf-8", errors="replace"))
        if not parser.results:
            raise SearchNotSupported("DuckDuckGo returned no parsable results")
        return parser.results[:max_results]
