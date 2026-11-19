"""M11 authentication tests: token resolution, sessions, CSRF, limits and remote guards."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from heraldesk.cli.main import app as cli_app
from heraldesk.core.bootstrap import InitResult
from heraldesk.server.run import ServerError, run_server
from heraldesk.web.auth import (
    ADMIN_TOKEN_ENV,
    AdminAuth,
    AuthConfigError,
    LoginRateLimiter,
    derive_admin_token,
    explicit_admin_token,
    mask_token,
    resolve_admin_token,
    token_in_query,
    trust_proxy_enabled,
)

pytestmark = pytest.mark.core

runner = CliRunner()
TOKEN = "test-admin-token"
LONG_TOKEN = "test-admin-token-0123456789abcdef"
SECRET_KEY_BYTES = b"fixed-secret-key-for-tests"


class FakeClock:
    def __init__(self) -> None:
        self.value = 1000.0

    def __call__(self) -> float:
        return self.value


def test_explicit_token_precedence_environment_then_dotenv(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, "environment-token")
    assert explicit_admin_token(initialized.layout) is not None
    assert resolve_admin_token(initialized.layout).token == "environment-token"
    assert resolve_admin_token(initialized.layout).source == "environment"

    monkeypatch.delenv(ADMIN_TOKEN_ENV)
    initialized.layout.env_path.write_text(f"{ADMIN_TOKEN_ENV}=dotenv-token\n", encoding="utf-8")
    resolution = resolve_admin_token(initialized.layout)
    assert resolution.token == "dotenv-token"
    assert resolution.source == ".env"


def test_derived_token_is_stable_and_masked(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)
    first = resolve_admin_token(initialized.layout)
    second = resolve_admin_token(initialized.layout)
    assert first.source == "secret.key"
    assert first.token == second.token
    assert first.token == derive_admin_token(initialized.layout)
    assert len(first.token) == 64
    assert mask_token(first.token) == f"****{first.token[-4:]}"
    assert mask_token(None) == "not configured"


def test_missing_key_and_token_is_a_clear_error(home: Path) -> None:
    with pytest.raises(AuthConfigError, match="HERALDESK_ADMIN_TOKEN"):
        resolve_admin_token(__import__("heraldesk").paths.layout_for(home))


def test_bearer_header_is_exact_and_query_tokens_are_rejected() -> None:
    auth = AdminAuth(TOKEN)
    assert auth.verify_bearer(f"Bearer {TOKEN}")
    assert auth.verify_bearer(f"bearer {TOKEN}")
    assert not auth.verify_bearer(f"Bearer {TOKEN}x")
    assert not auth.verify_bearer(TOKEN)
    assert not auth.verify_bearer(None)
    assert token_in_query("token=abc")
    assert token_in_query("access_token=abc&x=1")
    assert token_in_query("admin_token=abc")
    assert not token_in_query("status=pending")


def test_session_cookie_is_signed_expires_and_carries_csrf() -> None:
    auth = AdminAuth(TOKEN)
    now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
    session, cookie = auth.issue_session(now=now)
    read = auth.read_session(cookie, now=now)
    assert read is not None
    assert read.csrf == session.csrf
    assert auth.verify_csrf(read, session.csrf)
    assert not auth.verify_csrf(read, "wrong")

    tampered = cookie[:-1] + ("A" if cookie[-1] != "A" else "B")
    assert auth.read_session(tampered, now=now) is None
    assert auth.read_session(cookie, now=now + timedelta(hours=13)) is None


def test_login_csrf_round_trip() -> None:
    auth = AdminAuth(TOKEN)
    hidden, cookie = auth.issue_login_csrf()
    assert auth.verify_login_csrf(cookie, hidden)
    assert not auth.verify_login_csrf(cookie, "other")
    assert not auth.verify_login_csrf(None, hidden)


def test_rate_limiter_blocks_five_failures_for_a_minute() -> None:
    clock = FakeClock()
    limiter = LoginRateLimiter(clock=clock)
    key = "127.0.0.1"
    for _ in range(5):
        assert limiter.allowed(key)
        limiter.record_failure(key)
    assert not limiter.allowed(key)
    assert limiter.retry_after(key) >= 1

    clock.value += 61
    assert limiter.allowed(key)
    limiter.reset(key)
    assert limiter.allowed(key)


def test_remote_bind_requires_flag_token_and_https_proxy(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr(
        "heraldesk.server.run.uvicorn.run", lambda app, **kw: calls.append((app, kw))
    )
    monkeypatch.delenv("HERALDESK_TRUST_PROXY", raising=False)
    monkeypatch.delenv(ADMIN_TOKEN_ENV, raising=False)

    with pytest.raises(ServerError, match="loopback"):
        run_server(host="0.0.0.0")

    with pytest.raises(ServerError, match="HERALDESK_ADMIN_TOKEN"):
        run_server(host="0.0.0.0", allow_remote=True)

    monkeypatch.setenv(ADMIN_TOKEN_ENV, LONG_TOKEN)
    with pytest.raises(ServerError, match="HTTPS via reverse proxy"):
        run_server(host="0.0.0.0", allow_remote=True)

    monkeypatch.setenv(ADMIN_TOKEN_ENV, "short")
    monkeypatch.setenv("HERALDESK_TRUST_PROXY", "1")
    with pytest.raises(ServerError, match="at least 32"):
        run_server(host="0.0.0.0", allow_remote=True)

    monkeypatch.setenv(ADMIN_TOKEN_ENV, LONG_TOKEN)
    run_server(host="0.0.0.0", allow_remote=True)
    assert calls and calls[0][0].state.auth.cookie_secure is True


def test_loopback_with_trust_proxy_uses_secure_cookie(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []
    monkeypatch.setattr("heraldesk.server.run.uvicorn.run", lambda app, **kw: calls.append(app))
    monkeypatch.setenv(ADMIN_TOKEN_ENV, TOKEN)
    monkeypatch.setenv("HERALDESK_TRUST_PROXY", "1")

    run_server(host="127.0.0.1")
    assert calls[0].state.auth.cookie_secure is True


def test_query_redaction_filter_strips_access_query() -> None:
    from heraldesk.server.run import _QueryRedactionFilter

    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/login?token=secret", "1.1", 200),
        None,
    )
    assert _QueryRedactionFilter().filter(record)
    assert "secret" not in str(record.args)


def test_admin_token_cli_hides_raw_token_by_default(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ADMIN_TOKEN_ENV, TOKEN)
    hidden = runner.invoke(cli_app, ["admin", "token"])
    assert hidden.exit_code == 0, hidden.output
    assert TOKEN not in hidden.output
    assert "****" in hidden.output

    shown = runner.invoke(cli_app, ["admin", "token", "--show"])
    assert shown.exit_code == 0
    assert TOKEN in shown.output
    assert "secret" in shown.output.lower()


def test_trust_proxy_can_come_from_dotenv(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HERALDESK_TRUST_PROXY", raising=False)
    initialized.layout.env_path.write_text("HERALDESK_TRUST_PROXY=1\n", encoding="utf-8")
    assert trust_proxy_enabled(initialized.layout) is True


def test_explicit_false_trust_proxy_overrides_dotenv(
    initialized: InitResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    initialized.layout.env_path.write_text("HERALDESK_TRUST_PROXY=1\n", encoding="utf-8")
    monkeypatch.setenv("HERALDESK_TRUST_PROXY", "0")
    assert trust_proxy_enabled(initialized.layout) is False

    monkeypatch.setenv("HERALDESK_TRUST_PROXY", "1")
    assert trust_proxy_enabled(initialized.layout) is True
