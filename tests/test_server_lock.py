"""Single-process server lock tests."""

from __future__ import annotations

import json

import pytest

from heraldesk.core.bootstrap import InitResult
from heraldesk.core.locks import (
    ServerLockError,
    acquire_server_lock,
    release_server_lock,
    server_is_running,
    server_lock_path,
)

pytestmark = pytest.mark.core


def test_lock_round_trip(initialized: InitResult) -> None:
    pid = acquire_server_lock(initialized.layout)
    try:
        assert pid > 0
        assert server_is_running(initialized.layout) == pid
        with pytest.raises(ServerLockError, match="already running"):
            acquire_server_lock(initialized.layout)
    finally:
        release_server_lock(initialized.layout)
    assert server_is_running(initialized.layout) is None


def test_stale_lock_is_replaced(initialized: InitResult) -> None:
    server_lock_path(initialized.layout).write_text(
        json.dumps({"pid": 2_147_483_647, "started_at": "2026-01-01T00:00:00+00:00"}),
        encoding="utf-8",
    )
    assert server_is_running(initialized.layout) is None

    pid = acquire_server_lock(initialized.layout)
    try:
        assert pid > 0
        assert server_is_running(initialized.layout) == pid
    finally:
        release_server_lock(initialized.layout)
