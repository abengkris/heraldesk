"""The shipped examples must stay valid."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from heraldesk.domain.profiles import load_profile

pytestmark = pytest.mark.core

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"


def test_example_profile_loads() -> None:
    definition = load_profile(EXAMPLES / "acme")
    assert definition.slug == "acme"
    assert definition.name == "Acme"
    assert definition.policy.default_mode.value == "draft_only"
    assert definition.voice_samples


def test_example_plugin_adapter_contract() -> None:
    sys.path.insert(0, str(EXAMPLES / "plugin"))
    try:
        from hd_example_plugin import ExampleAdapter

        from heraldesk.platforms.base import AccountRef, PublishRequest
    finally:
        sys.path.remove(str(EXAMPLES / "plugin"))

    adapter = ExampleAdapter()
    account = AccountRef(platform="example", handle="@demo", credentials={"token": "demo"})
    request = PublishRequest(
        content_item_id=7,
        platform="example",
        body="hello",
        idempotency_key="heraldesk:content:7",
    )

    import asyncio

    assert asyncio.run(adapter.validate_credentials(account)) is True
    assert asyncio.run(adapter.publish(account, request)).external_id == "example-7"
