"""Threads adapter tests. Every HTTP call is mocked; the suite never uses the network."""

from __future__ import annotations

import asyncio
import json

import pytest

from heraldesk.platforms import adapter_for, publish_adapter_for
from heraldesk.platforms.base import (
    AccountRef,
    PublishError,
    PublishRequest,
)
from heraldesk.platforms.threads import (
    MAX_TEXT_BYTES,
    ThreadsAdapter,
    ThreadsResponse,
)

pytestmark = pytest.mark.core

TOKEN = "th-long-lived-access-token"
USER_ID = "17841400000000001"
ACCOUNT = AccountRef(
    platform="threads",
    handle="@acme",
    credentials={"access_token": TOKEN, "user_id": USER_ID},
)
REQUEST = PublishRequest(
    content_item_id=7,
    platform="threads",
    body="A smaller dashboard that loads faster.",
    idempotency_key="heraldesk:content:7",
)


def response(status: int, payload: object) -> ThreadsResponse:
    return ThreadsResponse(status_code=status, body=json.dumps(payload).encode("utf-8"))


class FakeClient:
    def __init__(
        self,
        *,
        get: list[ThreadsResponse] | None = None,
        post: list[ThreadsResponse] | None = None,
    ) -> None:
        self.gets: list[tuple[str, dict[str, str], float]] = []
        self.posts: list[tuple[str, dict[str, str], float]] = []
        self._get = list(get or [])
        self._post = list(post or [])

    async def get(self, url: str, params: dict[str, str], *, timeout: float) -> ThreadsResponse:
        self.gets.append((url, dict(params), timeout))
        return self._get.pop(0)

    async def post(self, url: str, data: dict[str, str], *, timeout: float) -> ThreadsResponse:
        self.posts.append((url, dict(data), timeout))
        return self._post.pop(0)


def adapter(client: FakeClient) -> ThreadsAdapter:
    return ThreadsAdapter(
        client=client,
        timeout_seconds=1.0,
        poll_interval_seconds=0.0,
        poll_timeout_seconds=1.0,
    )


def happy_client() -> FakeClient:
    return FakeClient(
        post=[
            response(200, {"id": "container-1"}),
            response(200, {"id": "media-1"}),
        ],
        get=[
            response(200, {"status": "FINISHED"}),
            response(200, {"permalink": "https://www.threads.com/@acme/post/abc"}),
        ],
    )


def test_validate_credentials_accepts_matching_user() -> None:
    client = FakeClient(get=[response(200, {"id": USER_ID, "username": "acme"})])

    assert asyncio.run(adapter(client).validate_credentials(ACCOUNT)) is True
    url, params, _ = client.gets[0]
    assert url.endswith("/v1.0/me")
    assert params["fields"] == "id,username"
    assert params["access_token"] == TOKEN


def test_validate_credentials_rejects_a_mismatched_user() -> None:
    client = FakeClient(get=[response(200, {"id": "999"})])
    assert asyncio.run(adapter(client).validate_credentials(ACCOUNT)) is False


def test_validate_credentials_rejects_an_invalid_token() -> None:
    client = FakeClient(
        get=[
            response(
                401,
                {
                    "error": {
                        "message": "Invalid OAuth access token",
                        "type": "OAuthException",
                        "code": 190,
                    }
                },
            )
        ]
    )
    assert asyncio.run(adapter(client).validate_credentials(ACCOUNT)) is False


def test_validate_credentials_raises_on_a_server_error() -> None:
    client = FakeClient(get=[response(500, {"error": {"message": "temporary", "code": 2}})])
    with pytest.raises(PublishError, match="temporary"):
        asyncio.run(adapter(client).validate_credentials(ACCOUNT))


def test_publish_runs_the_two_step_flow_and_reads_the_permalink() -> None:
    client = happy_client()

    result = asyncio.run(adapter(client).publish(ACCOUNT, REQUEST))

    assert result.external_id == "media-1"
    assert result.url == "https://www.threads.com/@acme/post/abc"
    assert client.posts[0][0].endswith(f"/v1.0/{USER_ID}/threads")
    assert client.posts[0][1]["media_type"] == "TEXT"
    assert client.posts[0][1]["text"] == REQUEST.body
    assert client.posts[0][1]["access_token"] == TOKEN
    assert client.posts[1][0].endswith(f"/v1.0/{USER_ID}/threads_publish")
    assert client.posts[1][1]["creation_id"] == "container-1"
    assert client.gets[0][1]["fields"] == "status,error_message"


def test_publish_resolves_the_user_id_when_absent() -> None:
    client = FakeClient(
        get=[
            response(200, {"id": USER_ID}),
            response(200, {"status": "FINISHED"}),
            response(200, {"permalink": "https://www.threads.com/@acme/post/abc"}),
        ],
        post=[response(200, {"id": "container-1"}), response(200, {"id": "media-1"})],
    )
    account = AccountRef(platform="threads", handle="@acme", credentials={"access_token": TOKEN})

    result = asyncio.run(adapter(client).publish(account, REQUEST))

    assert result.external_id == "media-1"
    assert client.posts[0][0].endswith(f"/v1.0/{USER_ID}/threads")


def test_publish_includes_optional_link_and_topic() -> None:
    client = happy_client()
    request = PublishRequest(
        content_item_id=8,
        platform="threads",
        body="Read more",
        idempotency_key="heraldesk:content:8",
        media={
            "link_attachment": "https://example.com/post",
            "topic_tag": "BuildInPublic",
        },
    )

    asyncio.run(adapter(client).publish(ACCOUNT, request))

    assert client.posts[0][1]["link_attachment"] == "https://example.com/post"
    assert client.posts[0][1]["topic_tag"] == "BuildInPublic"


def test_publish_rejects_text_over_the_byte_limit_before_the_network() -> None:
    client = FakeClient()
    body = "😀" * (MAX_TEXT_BYTES // 4 + 1)
    request = PublishRequest(
        content_item_id=9,
        platform="threads",
        body=body,
        idempotency_key="heraldesk:content:9",
    )

    with pytest.raises(PublishError, match="bytes"):
        asyncio.run(adapter(client).publish(ACCOUNT, request))

    assert client.posts == []
    assert client.gets == []


def test_publish_reports_a_failed_container_without_publishing() -> None:
    client = FakeClient(
        post=[response(200, {"id": "container-1"})],
        get=[response(200, {"status": "ERROR", "error_message": "text rejected"})],
    )

    with pytest.raises(PublishError, match="container error: text rejected"):
        asyncio.run(adapter(client).publish(ACCOUNT, REQUEST))

    assert len(client.posts) == 1


def test_publish_survives_a_missing_permalink() -> None:
    client = FakeClient(
        post=[response(200, {"id": "container-1"}), response(200, {"id": "media-1"})],
        get=[response(200, {"status": "FINISHED"}), response(404, {"error": {"code": 100}})],
    )

    result = asyncio.run(adapter(client).publish(ACCOUNT, REQUEST))

    assert result.external_id == "media-1"
    assert result.url is None


def test_publish_is_idempotent_within_one_adapter_process() -> None:
    client = happy_client()
    instance = adapter(client)

    first = asyncio.run(instance.publish(ACCOUNT, REQUEST))
    second = asyncio.run(instance.publish(ACCOUNT, REQUEST))

    assert first == second
    assert len(client.posts) == 2
    assert len(client.gets) == 2


def test_errors_never_include_the_access_token() -> None:
    client = FakeClient(
        get=[
            response(
                500,
                {
                    "error": {
                        "message": "temporary failure",
                        "type": "OAuthException",
                        "code": 2,
                        "fbtrace_id": "trace-1",
                    }
                },
            )
        ]
    )
    account = AccountRef(
        platform="threads",
        handle="@acme",
        credentials={"access_token": TOKEN},
    )

    with pytest.raises(PublishError) as raised:
        asyncio.run(adapter(client).validate_credentials(account))

    assert TOKEN not in str(raised.value)


def test_registry_exposes_threads_as_a_built_in() -> None:
    assert isinstance(publish_adapter_for("threads"), ThreadsAdapter)
    assert isinstance(adapter_for("threads"), ThreadsAdapter)


def test_refresh_token_uses_the_root_endpoint() -> None:
    client = FakeClient(
        get=[
            response(
                200,
                {
                    "access_token": "th-refreshed-token",
                    "token_type": "bearer",
                    "expires_in": 5_184_000,
                },
            )
        ]
    )
    instance = adapter(client)

    result = asyncio.run(instance.refresh_token(TOKEN))

    assert result.access_token == "th-refreshed-token"
    assert result.expires_in == 5_184_000
    url, params, _ = client.gets[0]
    assert url == "https://graph.threads.com/refresh_access_token"
    assert params["grant_type"] == "th_refresh_token"
    assert params["access_token"] == TOKEN
