"""Tests for configuration loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from heraldesk.config import ConfigError, is_loopback, load_config

pytestmark = pytest.mark.core


def write_yaml(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "heraldesk.yaml").write_text(body, encoding="utf-8")


def write_env(home: Path, body: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / ".env").write_text(body, encoding="utf-8")


def test_defaults_without_a_config_file(home: Path) -> None:
    config = load_config(home)
    assert config.version == 1
    assert config.server.host == "127.0.0.1"
    assert config.server.port == 8000
    assert config.llm.max_daily_cost_usd == 5.0
    assert config.telegram.enabled is False
    assert config.telegram.mode == "polling"
    assert config.telegram.allowed_chat_ids == []


def test_yaml_overrides_defaults(home: Path) -> None:
    write_yaml(
        home,
        """
version: 1
llm:
  default_model: deepseek/deepseek-chat
  max_daily_cost_usd: 2.5
server:
  port: 9000
telegram:
  enabled: true
  mode: webhook
  allowed_chat_ids: [42]
""",
    )
    config = load_config(home)
    assert config.llm.default_model == "deepseek/deepseek-chat"
    assert config.llm.max_daily_cost_usd == 2.5
    assert config.server.port == 9000
    assert config.telegram.enabled is True
    assert config.telegram.mode == "webhook"
    assert config.telegram.allowed_chat_ids == [42]


def test_environment_overrides_yaml(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_yaml(home, "server:\n  port: 9000\n")
    monkeypatch.setenv("HERALDESK_SERVER__PORT", "9001")
    assert load_config(home).server.port == 9001


def test_dotenv_overrides_yaml(home: Path) -> None:
    write_yaml(home, "server:\n  port: 9000\n")
    write_env(home, "HERALDESK_SERVER__PORT=9002\n")
    assert load_config(home).server.port == 9002


def test_environment_overrides_dotenv(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_env(home, "HERALDESK_SERVER__PORT=9002\n")
    monkeypatch.setenv("HERALDESK_SERVER__PORT", "9003")
    assert load_config(home).server.port == 9003


def test_dotenv_outside_the_data_folder_is_ignored(
    home: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / ".env").write_text("HERALDESK_SERVER__PORT=9111\n", encoding="utf-8")
    monkeypatch.chdir(elsewhere)
    assert load_config(home).server.port == 8000


def test_unknown_yaml_key_is_rejected(home: Path) -> None:
    write_yaml(home, "server:\n  hostname: 127.0.0.1\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(home)


def test_invalid_yaml_is_reported(home: Path) -> None:
    write_yaml(home, "server: [unclosed\n")
    with pytest.raises(ConfigError):
        load_config(home)


def test_invalid_port_is_rejected(home: Path) -> None:
    write_yaml(home, "server:\n  port: 70000\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(home)


def test_unknown_config_version_is_rejected(home: Path) -> None:
    write_yaml(home, "version: 2\n")
    with pytest.raises(ConfigError, match="invalid configuration"):
        load_config(home)


def test_non_loopback_host_loads(home: Path) -> None:
    write_yaml(home, "server:\n  host: 0.0.0.0\n")
    assert load_config(home).server.host == "0.0.0.0"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "LOCALHOST"])
def test_is_loopback_accepts_loopback_names(host: str) -> None:
    assert is_loopback(host)


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.10", "example.com", ""])
def test_is_loopback_rejects_other_hosts(host: str) -> None:
    assert not is_loopback(host)
