"""Threads adapter for the official Meta Threads API.

Text posts use the documented two-step flow: create a ``TEXT`` container, wait for it to
finish, publish it, then read the permalink. Tokens travel in the ``access_token``
form/query field exactly as the official examples show and never enter an error message.

The adapter is intentionally conservative:

* it rejects text over 500 UTF-8 bytes before any network call, because the platform
  counts emoji by byte length and a deterministic rejection would waste retries;
* it treats only documented auth/permission errors as "credentials rejected"; transport
  and server failures are raised as :class:`PublishError`;
* idempotency inside one process is best effort. Durable at-most-once comes from the
  publisher persisting ``external_id``; the Threads API documents no idempotency key.

Research notes, limits and the manual acceptance procedure are in
``docs/platforms/threads.md``.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from heraldesk import __version__
from heraldesk.platforms.base import (
    AccountRef,
    PublishError,
    PublishRequest,
    PublishResult,
)

DEFAULT_BASE_URL: Final = "https://graph.threads.com/v1.0"
DEFAULT_TIMEOUT_SECONDS: Final = 15.0
DEFAULT_POLL_INTERVAL_SECONDS: Final = 1.0
DEFAULT_POLL_TIMEOUT_SECONDS: Final = 30.0
MAX_TEXT_BYTES: Final = 500
MAX_ERROR_LENGTH: Final = 500

# Documented Graph API auth/permission codes. Only these mean "the credential is bad";
# everything else is an adapter error so callers can distinguish them.
AUTH_ERROR_CODES: Final = frozenset({190, 102, 463, 467})
PERMISSION_ERROR_CODES: Final = frozenset({10, 200, 272})
FINISHED_STATES: Final = frozenset({"FINISHED", "PUBLISHED"})
FAILED_STATES: Final = frozenset({"ERROR", "EXPIRED"})


@dataclass(frozen=True, slots=True)
class ThreadsResponse:
    """One HTTP response. Errors keep the body so the Graph envelope can be parsed."""

    status_code: int
    body: bytes


class ThreadsClient(Protocol):
    """HTTP transport used by the adapter. Tests inject a fake."""

    async def get(
        self,
        url: str,
        params: Mapping[str, str],
        *,
        timeout: float,
    ) -> ThreadsResponse: ...

    async def post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        timeout: float,
    ) -> ThreadsResponse: ...


class UrllibThreadsClient:
    """Standard-library HTTP client, kept async-friendly with ``asyncio.to_thread``."""

    async def get(
        self,
        url: str,
        params: Mapping[str, str],
        *,
        timeout: float,
    ) -> ThreadsResponse:
        return await asyncio.to_thread(self._request, "GET", url, params, None, timeout)

    async def post(
        self,
        url: str,
        data: Mapping[str, str],
        *,
        timeout: float,
    ) -> ThreadsResponse:
        return await asyncio.to_thread(self._request, "POST", url, {}, data, timeout)

    def _request(
        self,
        method: str,
        url: str,
        params: Mapping[str, str],
        data: Mapping[str, str] | None,
        timeout: float,
    ) -> ThreadsResponse:
        query = f"?{urlencode(params)}" if params else ""
        encoded = urlencode(data).encode("utf-8") if data is not None else None
        request = Request(
            f"{url}{query}",
            data=encoded,
            method=method,
            headers={"User-Agent": f"heraldesk/{__version__}"},
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                return ThreadsResponse(status_code=response.status, body=response.read())
        except HTTPError as exc:
            return ThreadsResponse(status_code=exc.code, body=exc.read())
        except (URLError, OSError, ValueError) as exc:
            raise PublishError(f"Threads request failed ({type(exc).__name__})") from exc


class ThreadsAdapter:
    """Publish one text thread through the official Threads Graph API."""

    platform = "threads"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_BASE_URL,
        client: ThreadsClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        poll_timeout_seconds: float = DEFAULT_POLL_TIMEOUT_SECONDS,
    ) -> None:
        cleaned = base_url.strip().rstrip("/")
        if not cleaned.startswith("https://"):
            raise PublishError("Threads base URL must use https")
        if timeout_seconds <= 0:
            raise PublishError("Threads timeout must be positive")
        if poll_interval_seconds < 0:
            raise PublishError("Threads poll interval must not be negative")
        if poll_timeout_seconds <= 0:
            raise PublishError("Threads poll timeout must be positive")
        self._base_url = cleaned
        self._client: ThreadsClient = client or UrllibThreadsClient()
        self._timeout = timeout_seconds
        self._poll_interval = poll_interval_seconds
        self._poll_timeout = poll_timeout_seconds
        self._results: dict[str, PublishResult] = {}

    async def validate_credentials(self, account: AccountRef) -> bool:
        """Return ``True`` when ``/me`` accepts the token and matches ``user_id``."""
        token = account.credentials.get("access_token", "").strip()
        if not token:
            return False
        response = await self._client.get(
            self._url("/me"),
            {"fields": "id,username", "access_token": token},
            timeout=self._timeout,
        )
        if response.status_code == 200:
            payload = self._json(response)
            current = str(payload.get("id", "")).strip()
            expected = account.credentials.get("user_id", "").strip()
            return bool(current) and (not expected or current == expected)
        if self._error_code(response) in AUTH_ERROR_CODES | PERMISSION_ERROR_CODES:
            return False
        raise PublishError(self._error_message(response))

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult:
        """Create, wait for, publish and resolve one text thread."""
        token = account.credentials.get("access_token", "").strip()
        if not token:
            raise PublishError("Threads account has no access_token credential")
        cached = self._results.get(request.idempotency_key)
        if cached is not None:
            return cached

        body = request.body
        if not body.strip():
            raise PublishError("Threads post body must not be empty")
        size = len(body.encode("utf-8"))
        if size > MAX_TEXT_BYTES:
            raise PublishError(
                f"Threads text is {size} bytes; the documented limit is {MAX_TEXT_BYTES} bytes"
            )

        user_id = account.credentials.get("user_id", "").strip()
        if not user_id:
            user_id = await self._resolve_user_id(token)

        fields = {"media_type": "TEXT", "text": body, "access_token": token}
        link = request.media.get("link_attachment")
        if isinstance(link, str) and link.strip():
            fields["link_attachment"] = link.strip()
        topic = request.media.get("topic_tag")
        if isinstance(topic, str) and topic.strip():
            fields["topic_tag"] = topic.strip()

        container = await self._post_json(
            self._url(f"/{user_id}/threads"),
            fields,
        )
        creation_id = self._required_id(container, "Threads did not return a container id")
        await self._wait_for_container(creation_id, token)
        published = await self._post_json(
            self._url(f"/{user_id}/threads_publish"),
            {"creation_id": creation_id, "access_token": token},
        )
        media_id = self._required_id(published, "Threads did not return a media id")
        result = PublishResult(
            external_id=media_id,
            url=await self._permalink(media_id, token),
        )
        self._results[request.idempotency_key] = result
        return result

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}"

    async def _resolve_user_id(self, token: str) -> str:
        response = await self._client.get(
            self._url("/me"),
            {"fields": "id", "access_token": token},
            timeout=self._timeout,
        )
        payload = self._checked_json(response)
        return self._required_id(payload, "Threads did not return the authenticated user id")

    async def _wait_for_container(self, creation_id: str, token: str) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._poll_timeout
        while True:
            response = await self._client.get(
                self._url(f"/{creation_id}"),
                {"fields": "status,error_message", "access_token": token},
                timeout=self._timeout,
            )
            payload = self._checked_json(response)
            status = str(payload.get("status", "")).upper()
            if status in FINISHED_STATES:
                return
            if status in FAILED_STATES:
                message = str(payload.get("error_message") or status)
                raise PublishError(
                    f"Threads container {status.lower()}: {message[:MAX_ERROR_LENGTH]}"
                )
            if loop.time() >= deadline:
                raise PublishError("Threads container did not finish before the timeout")
            await asyncio.sleep(self._poll_interval)

    async def _post_json(self, url: str, data: Mapping[str, str]) -> dict[str, Any]:
        response = await self._client.post(url, data, timeout=self._timeout)
        return self._checked_json(response)

    def _checked_json(self, response: ThreadsResponse) -> dict[str, Any]:
        if response.status_code >= 400:
            raise PublishError(self._error_message(response))
        return self._json(response)

    def _json(self, response: ThreadsResponse) -> dict[str, Any]:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PublishError("Threads returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PublishError("Threads returned an unexpected payload")
        return payload

    def _error_parts(self, response: ThreadsResponse) -> tuple[str, str | None, str | None]:
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return f"HTTP {response.status_code}", None, None
        if not isinstance(payload, dict):
            return f"HTTP {response.status_code}", None, None
        error = payload.get("error")
        if isinstance(error, dict):
            code = error.get("code")
            message = error.get("message") or error.get("error_user_msg") or "request rejected"
            trace = error.get("fbtrace_id")
            return (
                str(code) if code is not None else "unknown",
                str(message),
                str(trace) if trace is not None else None,
            )
        # The OAuth token endpoints use a flat envelope.
        code = payload.get("code")
        message = payload.get("error_message") or payload.get("error_type") or "request rejected"
        return (
            str(code) if code is not None else "unknown",
            str(message),
            None,
        )

    def _error_code(self, response: ThreadsResponse) -> int | None:
        code, _, _ = self._error_parts(response)
        try:
            return int(code)
        except ValueError:
            return None

    def _error_message(self, response: ThreadsResponse) -> str:
        code, message, trace = self._error_parts(response)
        text = f"Threads API error {code}: {message}"
        if trace:
            text = f"{text} (fbtrace {trace})"
        return text[:MAX_ERROR_LENGTH]

    @staticmethod
    def _required_id(payload: Mapping[str, Any], message: str) -> str:
        value = payload.get("id")
        if not isinstance(value, str) or not value.strip():
            raise PublishError(message)
        return value.strip()

    async def _permalink(self, media_id: str, token: str) -> str | None:
        """Best effort: the permalink can be omitted for flagged posts."""
        try:
            response = await self._client.get(
                self._url(f"/{media_id}"),
                {"fields": "permalink", "access_token": token},
                timeout=self._timeout,
            )
        except PublishError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = self._json(response)
        except PublishError:
            return None
        permalink = payload.get("permalink")
        if isinstance(permalink, str) and permalink.startswith("https://"):
            return permalink
        return None
