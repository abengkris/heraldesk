"""CLI tests for ``heraldesk plugins``."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.plugins import PluginAdapter, PluginError

pytestmark = pytest.mark.core

runner = CliRunner()


class SampleAdapter:
    platform = "sample"

    async def validate_credentials(self, account: object) -> bool:
        _ = account
        return True

    async def publish(self, account: object, request: object) -> object:
        _ = account, request
        return object()


def test_list_shows_built_in_adapters() -> None:
    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0, result.output
    assert "fake" in result.output
    assert "nostr" in result.output
    assert "built-in" in result.output


def test_list_shows_plugins_without_importing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[bool] = []

    def factory() -> SampleAdapter:
        calls.append(True)
        return SampleAdapter()

    plugin = PluginAdapter(platform="sample", factory=factory, target="sample_pkg:Adapter")
    monkeypatch.setattr("heraldesk.cli.plugins.discover_plugins", lambda: {"sample": plugin})

    result = runner.invoke(app, ["plugins", "list"])

    assert result.exit_code == 0, result.output
    assert "sample" in result.output
    assert "sample_pkg:Adapter" in result.output
    assert "not checked" in result.output
    assert calls == []


def test_list_check_validates_the_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[bool] = []

    def factory() -> SampleAdapter:
        calls.append(True)
        return SampleAdapter()

    plugin = PluginAdapter(platform="sample", factory=factory, target="sample_pkg:Adapter")
    monkeypatch.setattr("heraldesk.cli.plugins.discover_plugins", lambda: {"sample": plugin})

    result = runner.invoke(app, ["plugins", "list", "--check"])

    assert result.exit_code == 0, result.output
    assert "ok (SampleAdapter)" in result.output
    assert calls == [True]


def test_list_check_exits_one_for_a_broken_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    def factory() -> SampleAdapter:
        raise PluginError("plugin 'sample' failed to import: boom")

    plugin = PluginAdapter(platform="sample", factory=factory, target="sample_pkg:Adapter")
    monkeypatch.setattr("heraldesk.cli.plugins.discover_plugins", lambda: {"sample": plugin})

    result = runner.invoke(app, ["plugins", "list", "--check"])

    assert result.exit_code == 1
    assert "boom" in result.output
