"""Offline health checks for ``heraldesk doctor``, plus opt-in online probes.

Every check runs locally. ``doctor --online`` adds three explicit network probes: the
configured LLM provider, the Telegram Bot API when Telegram is enabled, and Nostr relays
when at least one Nostr account exists. A FAIL result makes the command exit non-zero;
WARN does not. Online probes never print an API key, bot token or nsec.
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from heraldesk import paths
from heraldesk.config import Config, ConfigError, is_loopback, load_config
from heraldesk.core.crypto import CryptoError, key_is_secure, validate_key
from heraldesk.db.engine import WAL_MODE, build_engine, inspect_database
from heraldesk.db.migrations import MigrationError, head_revision
from heraldesk.db.repositories import ProfileCatalog, SocialAccountRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import session_factory
from heraldesk.llm.errors import LLMError
from heraldesk.llm.factory import build_llm
from heraldesk.llm.types import ChatMessage, LLMRequest
from heraldesk.telegram.base import TelegramError
from heraldesk.telegram.bot_api import BotApiTelegramAdapter
from heraldesk.telegram.credentials import (
    TelegramConfigError,
    resolve_bot_token,
    token_source,
)
from heraldesk.web.auth import explicit_admin_token

REQUIRED_PYTHON = (3, 12)


class CheckStatus(StrEnum):
    """Outcome of a single doctor check."""

    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One doctor check and its outcome."""

    name: str
    status: CheckStatus
    detail: str


def run_checks(home: Path | None = None, *, online: bool = False) -> list[CheckResult]:
    """Run all offline checks for a data folder."""
    layout = paths.layout_for(home if home is not None else paths.resolve_home())
    results = [
        _check_python(),
        _check_home(layout),
        _check_layout(layout),
        _check_key(layout),
        _check_database(layout),
        _check_migrations(layout),
        _check_config(layout),
        _check_admin_token(layout),
        _check_telegram(layout),
        _check_reports(layout),
        _check_env_file(layout),
        _check_server_host(layout),
    ]
    if online:
        try:
            config = load_config(layout.home)
        except ConfigError:
            results.append(
                CheckResult(
                    name="online checks",
                    status=CheckStatus.WARN,
                    detail="skipped because the configuration is invalid",
                )
            )
        else:
            results.extend(
                (
                    _check_online_llm(layout, config),
                    _check_online_telegram(layout, config),
                    _check_online_nostr(layout, config),
                )
            )
    return results


def has_failures(results: Sequence[CheckResult]) -> bool:
    """Return ``True`` when any check failed."""
    return any(result.status is CheckStatus.FAIL for result in results)


def _check_python() -> CheckResult:
    version = sys.version_info
    found = f"{version.major}.{version.minor}.{version.micro}"
    if (version.major, version.minor) != REQUIRED_PYTHON:
        return CheckResult(
            "python",
            CheckStatus.FAIL,
            f"{found} found, Python 3.12 required",
        )
    return CheckResult("python", CheckStatus.PASS, found)


def _check_home(layout: paths.Layout) -> CheckResult:
    if not layout.home.exists():
        return CheckResult(
            "data folder",
            CheckStatus.FAIL,
            f"{layout.home} does not exist; run heraldesk init",
        )
    if not layout.home.is_dir():
        return CheckResult("data folder", CheckStatus.FAIL, f"{layout.home} is not a directory")
    try:
        with tempfile.NamedTemporaryFile(dir=layout.home, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return CheckResult("data folder", CheckStatus.FAIL, f"not writable: {exc}")
    return CheckResult("data folder", CheckStatus.PASS, str(layout.home))


def _check_layout(layout: paths.Layout) -> CheckResult:
    missing = [directory.name for directory in layout.subdirs if not directory.is_dir()]
    if missing:
        return CheckResult(
            "folders",
            CheckStatus.WARN,
            f"missing: {', '.join(missing)}; run heraldesk init",
        )
    return CheckResult("folders", CheckStatus.PASS, ", ".join(d.name for d in layout.subdirs))


def _check_key(layout: paths.Layout) -> CheckResult:
    key_path = layout.key_path
    if not key_path.is_file():
        return CheckResult("secret.key", CheckStatus.FAIL, "missing; run heraldesk init")
    try:
        validate_key(key_path.read_bytes())
    except (CryptoError, OSError) as exc:
        return CheckResult("secret.key", CheckStatus.FAIL, str(exc))
    if os.name == "posix":
        try:
            secure = key_is_secure(key_path)
            mode = stat.S_IMODE(key_path.stat().st_mode)
        except OSError as exc:
            return CheckResult("secret.key", CheckStatus.FAIL, f"cannot read permissions: {exc}")
        if not secure:
            return CheckResult(
                "secret.key",
                CheckStatus.FAIL,
                f"mode {mode:04o}, expected 600; run chmod 600 {key_path}",
            )
        return CheckResult("secret.key", CheckStatus.PASS, f"mode {mode:04o}")
    return CheckResult(
        "secret.key", CheckStatus.PASS, "present (permissions not checkable on Windows)"
    )


def _check_database(layout: paths.Layout) -> CheckResult:
    status = inspect_database(layout.db_path)
    if status.error is not None:
        return CheckResult(
            "database", CheckStatus.FAIL, f"cannot open {layout.db_path}: {status.error}"
        )
    if not status.exists:
        return CheckResult("database", CheckStatus.WARN, "not found; run heraldesk init")
    if status.journal_mode != WAL_MODE:
        return CheckResult(
            "database",
            CheckStatus.WARN,
            f"journal_mode is {status.journal_mode or 'unknown'}, expected {WAL_MODE}",
        )
    if not status.revision:
        return CheckResult("database", CheckStatus.WARN, "no schema revision; run heraldesk init")
    return CheckResult(
        "database", CheckStatus.PASS, f"SQLite {WAL_MODE.upper()}, revision {status.revision}"
    )


def _check_migrations(layout: paths.Layout) -> CheckResult:
    status = inspect_database(layout.db_path)
    if status.error is not None or not status.exists:
        return CheckResult(
            "migrations",
            CheckStatus.WARN,
            "skipped because the database is not readable",
        )
    if not status.revision:
        return CheckResult("migrations", CheckStatus.WARN, "no schema revision; run heraldesk init")
    try:
        head = head_revision(layout.db_path)
    except MigrationError as exc:
        return CheckResult("migrations", CheckStatus.FAIL, str(exc))
    if status.revision != head:
        return CheckResult(
            "migrations",
            CheckStatus.FAIL,
            f"database revision {status.revision}, expected {head}; run heraldesk init",
        )
    return CheckResult("migrations", CheckStatus.PASS, f"revision {head}")


def _check_config(layout: paths.Layout) -> CheckResult:
    if not layout.config_path.is_file():
        return CheckResult(
            "heraldesk.yaml",
            CheckStatus.WARN,
            "not found; built-in defaults are in use",
        )
    try:
        load_config(layout.home)
    except ConfigError as exc:
        return CheckResult("heraldesk.yaml", CheckStatus.FAIL, str(exc))
    return CheckResult("heraldesk.yaml", CheckStatus.PASS, str(layout.config_path))


def _check_telegram(layout: paths.Layout) -> CheckResult:
    try:
        config = load_config(layout.home)
    except ConfigError:
        return CheckResult(
            "telegram token",
            CheckStatus.WARN,
            "skipped because the configuration is invalid",
        )
    if not config.telegram.enabled:
        return CheckResult("telegram token", CheckStatus.PASS, "telegram is disabled")
    try:
        token = resolve_bot_token(layout)
    except TelegramConfigError as exc:
        return CheckResult("telegram token", CheckStatus.FAIL, str(exc))
    if token is None:
        return CheckResult(
            "telegram token",
            CheckStatus.FAIL,
            "telegram.enabled is true but no bot token is configured; "
            "run 'heraldesk telegram token set'",
        )
    return CheckResult(
        "telegram token",
        CheckStatus.PASS,
        f"configured (source: {token_source(layout)})",
    )


def _check_reports(layout: paths.Layout) -> CheckResult:
    reports = layout.reports
    if reports.exists():
        if not reports.is_dir():
            return CheckResult("reports", CheckStatus.FAIL, f"{reports} is not a directory")
        try:
            with tempfile.NamedTemporaryFile(dir=reports, prefix=".doctor-", delete=True):
                pass
        except OSError as exc:
            return CheckResult("reports", CheckStatus.FAIL, f"not writable: {exc}")
        return CheckResult("reports", CheckStatus.PASS, str(reports))
    try:
        with tempfile.NamedTemporaryFile(dir=layout.home, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return CheckResult(
            "reports",
            CheckStatus.FAIL,
            f"{reports} is missing and cannot be created: {exc}",
        )
    return CheckResult(
        "reports",
        CheckStatus.PASS,
        f"{reports} not created yet; the data folder is writable",
    )


def _check_env_file(layout: paths.Layout) -> CheckResult:
    env_path = layout.env_path
    if not env_path.is_file():
        return CheckResult(
            ".env", CheckStatus.PASS, "not present; LLM keys come from the environment"
        )
    if os.name == "posix":
        try:
            mode = stat.S_IMODE(env_path.stat().st_mode)
        except OSError as exc:
            return CheckResult(".env", CheckStatus.WARN, f"cannot read permissions: {exc}")
        if mode & 0o077:
            return CheckResult(
                ".env",
                CheckStatus.WARN,
                f"mode {mode:04o} exposes LLM keys to other users; run chmod 600 {env_path}",
            )
    return CheckResult(".env", CheckStatus.PASS, str(env_path))


def _check_server_host(layout: paths.Layout) -> CheckResult:
    try:
        config = load_config(layout.home)
    except ConfigError:
        return CheckResult(
            "server binding", CheckStatus.WARN, "skipped because the configuration is invalid"
        )
    if not is_loopback(config.server.host):
        return CheckResult(
            "server binding",
            CheckStatus.WARN,
            f"{config.server.host}:{config.server.port} is not loopback; "
            "authentication and an HTTPS reverse proxy are required",
        )
    return CheckResult(
        "server binding",
        CheckStatus.PASS,
        f"{config.server.host}:{config.server.port} (loopback)",
    )


def _check_online_llm(layout: paths.Layout, config: Config) -> CheckResult:
    try:
        detail = _probe_llm(layout, config)
    except LLMError as exc:
        return CheckResult("llm provider", CheckStatus.FAIL, str(exc))
    return CheckResult("llm provider", CheckStatus.PASS, detail)


def _probe_llm(layout: paths.Layout, config: Config) -> str:
    """Make one minimal, explicitly requested LLM call."""
    provider = build_llm(config, layout)
    if provider.name == "fake":
        return "fake provider selected; no network call"
    request = LLMRequest(
        messages=(ChatMessage(role="user", content="Reply with OK."),),
        model=config.llm.default_model.strip(),
        max_tokens=1,
        temperature=0.0,
    )
    result = provider.complete(request)
    return f"{result.model} responded"


def _check_online_telegram(layout: paths.Layout, config: Config) -> CheckResult:
    try:
        detail = _probe_telegram(layout, config)
    except TelegramError as exc:
        return CheckResult("telegram api", CheckStatus.FAIL, str(exc))
    return CheckResult("telegram api", CheckStatus.PASS, detail)


def _probe_telegram(layout: paths.Layout, config: Config) -> str:
    """Call ``getMe`` only when Telegram is enabled and a token is configured."""
    if not config.telegram.enabled:
        return "telegram is disabled"
    token = resolve_bot_token(layout)
    if token is None:
        raise TelegramConfigError(
            "telegram.enabled is true but no bot token is configured; "
            "run 'heraldesk telegram token set'"
        )
    adapter = BotApiTelegramAdapter(token)
    username = adapter.get_me()
    return f"bot @{username} reachable" if username else "bot reachable"


def _check_online_nostr(layout: paths.Layout, config: Config) -> CheckResult:
    try:
        detail = _probe_nostr(layout, config)
    except Exception as exc:  # websockets and TLS errors vary by platform
        return CheckResult("nostr relays", CheckStatus.WARN, f"no relay reachable: {exc}")
    return CheckResult("nostr relays", CheckStatus.PASS, detail)


def _probe_nostr(layout: paths.Layout, config: Config) -> str:
    """Open one relay connection when at least one Nostr account is configured."""
    if not _has_nostr_account(layout):
        return "no Nostr accounts configured"
    failures: list[str] = []
    timeout = min(config.nostr.timeout_seconds, 5.0)
    for relay in config.nostr.relays:
        try:
            _probe_relay(relay, timeout=timeout)
        except Exception as exc:
            failures.append(f"{relay} ({type(exc).__name__})")
        else:
            return f"{relay} reachable"
    raise RuntimeError("; ".join(failures))


def _probe_relay(relay: str, *, timeout: float) -> None:
    """Complete a WebSocket handshake with one relay, then close immediately."""
    import websockets

    async def check() -> None:
        async with websockets.connect(relay, open_timeout=timeout, close_timeout=timeout):
            return

    asyncio.run(check())


def _has_nostr_account(layout: paths.Layout) -> bool:
    """Return ``True`` when any registered profile has a Nostr account."""
    status = inspect_database(layout.db_path)
    if not status.exists or status.error is not None:
        return False
    engine = build_engine(layout.db_path)
    try:
        session = session_factory(engine)()
        try:
            accounts = SocialAccountRepository(session)
            for summary in ProfileCatalog(session).list():
                scoped = accounts.list(ProfileScope(summary.id))
                if any(account.platform == "nostr" for account in scoped):
                    return True
            return False
        finally:
            session.close()
    finally:
        engine.dispose()


def _check_admin_token(layout: paths.Layout) -> CheckResult:
    """Warn when an explicit admin token is too short for remote mode."""
    resolution = explicit_admin_token(layout)
    if resolution is None:
        return CheckResult("admin token", CheckStatus.PASS, "derived from secret.key")
    if len(resolution.token) < 32:
        return CheckResult(
            "admin token",
            CheckStatus.WARN,
            f"explicit token from {resolution.source} is shorter than 32 characters; "
            "remote mode refuses it",
        )
    return CheckResult("admin token", CheckStatus.PASS, f"explicit token ({resolution.source})")
