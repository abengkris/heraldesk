"""Third-party platform adapter discovery through entry points."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from heraldesk import plugins
from heraldesk.platforms import adapter_for, publish_adapter_for
from heraldesk.platforms.base import (
    AccountRef,
    NotSupportedError,
    PublishError,
    PublishRequest,
    PublishResult,
)
from heraldesk.platforms.fake import FakeAdapter
from heraldesk.plugins import (
    PluginError,
    discover_plugins,
    plugin_adapters,
    reset_plugin_cache,
)

pytestmark = pytest.mark.core


class SampleAdapter:
    """A minimal valid third-party adapter."""

    platform = "sample"

    async def validate_credentials(self, account: AccountRef) -> bool:
        return bool(account.credentials)

    async def publish(self, account: AccountRef, request: PublishRequest) -> PublishResult:
        return PublishResult(external_id=f"sample-{request.content_item_id}")


class WrongPlatformAdapter(SampleAdapter):
    platform = "other"


class MissingMethodsAdapter:
    platform = "sample"


class FakeEntryPoint:
    """The subset of ``importlib.metadata.EntryPoint`` used by the loader."""

    def __init__(
        self,
        name: str,
        target: Any,
        *,
        value: str = "sample_plugin:Adapter",
        error: Exception | None = None,
    ) -> None:
        self.name = name
        self.value = value
        self._target = target
        self._error = error
        self.load_calls = 0

    def load(self) -> Any:
        self.load_calls += 1
        if self._error is not None:
            raise self._error
        return self._target


@pytest.fixture(autouse=True)
def clear_plugin_cache() -> Iterator[None]:
    reset_plugin_cache()
    yield
    reset_plugin_cache()


def install(monkeypatch: pytest.MonkeyPatch, *entries: FakeEntryPoint) -> None:
    monkeypatch.setattr(plugins, "_entry_points", lambda: list(entries))


def test_discovery_reads_metadata_without_importing_plugin_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = FakeEntryPoint("sample", SampleAdapter)
    install(monkeypatch, entry)

    discovered = discover_plugins()

    assert set(discovered) == {"sample"}
    assert discovered["sample"].target == "sample_plugin:Adapter"
    assert entry.load_calls == 0


def test_publish_adapter_for_returns_the_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEntryPoint("sample", SampleAdapter))

    adapter = publish_adapter_for("sample")

    assert isinstance(adapter, SampleAdapter)


def test_entry_point_name_is_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEntryPoint(" Sample ", SampleAdapter))

    assert set(plugin_adapters()) == {"sample"}
    assert isinstance(publish_adapter_for("SAMPLE"), SampleAdapter)


def test_built_in_adapters_cannot_be_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_entry = FakeEntryPoint("fake", WrongPlatformAdapter)
    nostr_entry = FakeEntryPoint("nostr", WrongPlatformAdapter)
    install(monkeypatch, fake_entry, nostr_entry)

    assert discover_plugins() == {}
    assert isinstance(publish_adapter_for("fake"), FakeAdapter)
    assert fake_entry.load_calls == 0
    assert nostr_entry.load_calls == 0


def test_duplicate_entry_points_keep_the_first(monkeypatch: pytest.MonkeyPatch) -> None:
    first = FakeEntryPoint("sample", SampleAdapter)
    second = FakeEntryPoint("sample", WrongPlatformAdapter)
    install(monkeypatch, first, second)

    adapter = publish_adapter_for("sample")

    assert isinstance(adapter, SampleAdapter)
    assert first.load_calls == 1
    assert second.load_calls == 0


def test_unknown_platform_still_raises_not_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch)

    with pytest.raises(NotSupportedError, match="install a plugin"):
        publish_adapter_for("nonexistent")


def test_import_failure_is_a_publish_error(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = FakeEntryPoint("sample", SampleAdapter, error=ImportError("boom"))
    install(monkeypatch, entry)

    with pytest.raises(PluginError, match="failed to import") as raised:
        publish_adapter_for("sample")

    assert isinstance(raised.value, PublishError)


def test_factory_failure_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken() -> SampleAdapter:
        raise RuntimeError("cannot construct")

    install(monkeypatch, FakeEntryPoint("sample", broken))

    with pytest.raises(PluginError, match="factory failed"):
        publish_adapter_for("sample")


def test_non_callable_target_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEntryPoint("sample", object()))

    with pytest.raises(PluginError, match="not callable"):
        publish_adapter_for("sample")


def test_platform_mismatch_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEntryPoint("sample", WrongPlatformAdapter))

    with pytest.raises(PluginError, match="must match"):
        publish_adapter_for("sample")


def test_missing_methods_are_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, FakeEntryPoint("sample", MissingMethodsAdapter))

    with pytest.raises(PluginError, match="missing"):
        publish_adapter_for("sample")


def test_plugin_registry_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = FakeEntryPoint("sample", SampleAdapter)
    install(monkeypatch, entry)

    assert set(plugin_adapters()) == {"sample"}
    entry.name = "renamed"
    assert set(plugin_adapters()) == {"sample"}

    reset_plugin_cache()
    assert set(plugin_adapters()) == {"renamed"}


def test_account_checks_do_not_import_plugins(monkeypatch: pytest.MonkeyPatch) -> None:
    entry = FakeEntryPoint("sample", SampleAdapter)
    install(monkeypatch, entry)

    adapter = adapter_for("sample")

    assert isinstance(adapter, FakeAdapter)
    assert entry.load_calls == 0
