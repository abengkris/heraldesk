"""Tests for rotating file logging and redaction."""

from __future__ import annotations

import logging
import stat
import sys

import pytest

from heraldesk.core.bootstrap import initialize
from heraldesk.core.logging import configure_logging, log_path, reset_logging

pytestmark = pytest.mark.core


def test_file_logging_is_private_rotating_and_redacted(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = initialize(tmp_path / "home").layout
    secret = "sk-ABCDEFGHIJKLMNOP1234"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    monkeypatch.setenv("HERALDESK_ADMIN_TOKEN", "admin-secret-value")
    reset_logging()
    try:
        path = configure_logging(layout, extra_secrets=("admin-secret-value",))
        configure_logging(layout, extra_secrets=("admin-secret-value",))
        logger = logging.getLogger("heraldesk.test")
        logger.info("provider %s admin %s", secret, "admin-secret-value")
        for handler in logging.getLogger("heraldesk").handlers:
            handler.flush()
        content = path.read_text(encoding="utf-8")
        handlers = [
            handler
            for handler in logging.getLogger("heraldesk").handlers
            if getattr(handler, "_heraldesk_file_handler", False)
        ]
    finally:
        reset_logging()

    assert path == log_path(layout)
    assert len(handlers) == 1
    assert secret not in content
    assert "admin-secret-value" not in content
    assert "[REDACTED]" in content
    if sys.platform != "win32":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_logging_setup_does_not_raise_for_an_unwritable_log_folder(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = initialize(tmp_path / "home").layout
    reset_logging()

    def fail(*_args: object, **_kwargs: object) -> int:
        raise OSError("read-only for the test")

    monkeypatch.setattr("heraldesk.core.logging.os.open", fail)
    try:
        path = configure_logging(layout)
    finally:
        reset_logging()

    assert path == log_path(layout)
