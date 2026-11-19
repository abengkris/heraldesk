"""Global and per-profile off switches.

The per-profile kill switch is a column on ``profiles``. The global kill switch is a
marker file inside the data folder so that ``heraldesk stop-all`` works even while the
database is busy or being migrated.

Precedence, highest first: global kill switch, per-profile kill switch, profile status
``paused``, profile status ``draft``. Everything else is allowed to publish.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from heraldesk import paths

SERVER_LOCK_FILENAME = "server.lock"


class KillSwitchError(Exception):
    """Raised when the global kill-switch marker cannot be changed."""


class ServerLockError(Exception):
    """Raised when the single-process server lock cannot be acquired."""


class BlockReason(StrEnum):
    """Why publishing or replying is currently blocked."""

    GLOBAL_KILL_SWITCH = "global_kill_switch"
    PROFILE_KILL_SWITCH = "profile_kill_switch"
    PROFILE_PAUSED = "profile_paused"
    PROFILE_DRAFT = "profile_draft"
    UNKNOWN_STATUS = "unknown_status"


@dataclass(frozen=True, slots=True)
class OffSwitchState:
    """The off-switch inputs for one profile."""

    global_kill_switch: bool
    profile_kill_switch: bool
    profile_status: str


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Whether the off switches allow publishing for one profile."""

    allowed: bool
    reason: BlockReason | None = None


def evaluate_off_switches(state: OffSwitchState) -> GateDecision:
    """Apply off-switch precedence and return the first blocking reason."""
    if state.global_kill_switch:
        return GateDecision(allowed=False, reason=BlockReason.GLOBAL_KILL_SWITCH)
    if state.profile_kill_switch:
        return GateDecision(allowed=False, reason=BlockReason.PROFILE_KILL_SWITCH)
    if state.profile_status == "paused":
        return GateDecision(allowed=False, reason=BlockReason.PROFILE_PAUSED)
    if state.profile_status == "draft":
        return GateDecision(allowed=False, reason=BlockReason.PROFILE_DRAFT)
    if state.profile_status == "active":
        return GateDecision(allowed=True)
    return GateDecision(allowed=False, reason=BlockReason.UNKNOWN_STATUS)


def is_global_kill_switch_enabled(layout: paths.Layout) -> bool:
    """Return ``True`` when the global marker exists."""
    return layout.killswitch_path.is_file()


def engage_global_kill_switch(layout: paths.Layout) -> bool:
    """Create the global marker. Returns ``False`` when it already existed."""
    path = layout.killswitch_path
    if path.exists():
        if not path.is_file():
            raise KillSwitchError(f"{path} exists but is not a file")
        return False
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise KillSwitchError(f"cannot create {path}: {exc}") from exc
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"halted_at={datetime.now(UTC).isoformat()}\n")
    except OSError as exc:
        raise KillSwitchError(f"cannot write {path}: {exc}") from exc
    return True


def release_global_kill_switch(layout: paths.Layout) -> bool:
    """Remove the global marker. Returns ``False`` when it was already absent."""
    path = layout.killswitch_path
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise KillSwitchError(f"cannot remove {path}: {exc}") from exc
    return True


def global_kill_switch_path(layout: paths.Layout) -> Path:
    """Expose the marker path for diagnostics."""
    return layout.killswitch_path


def server_lock_path(layout: paths.Layout) -> Path:
    """Return the server-lock file path inside the data folder."""
    return layout.home / SERVER_LOCK_FILENAME


def _pid_alive(pid: int) -> bool:
    """Return ``True`` when *pid* appears to be a live process."""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_server_lock(layout: paths.Layout) -> int | None:
    """Return the recorded server pid, or ``None`` when the lock is absent or unreadable."""
    path = server_lock_path(layout)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pid = int(payload["pid"])
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return pid if pid > 0 else None


def server_is_running(layout: paths.Layout) -> int | None:
    """Return the live server pid, or ``None`` when no server holds the lock."""
    pid = read_server_lock(layout)
    if pid is None:
        return None
    return pid if _pid_alive(pid) else None


def acquire_server_lock(layout: paths.Layout) -> int:
    """Write the server lock, refusing when another live process owns it."""
    existing = server_is_running(layout)
    if existing is not None:
        raise ServerLockError(
            f"heraldesk serve is already running (pid {existing}); "
            "stop it before starting another server or restoring a backup"
        )
    path = server_lock_path(layout)
    temporary = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(
        {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()},
        sort_keys=True,
    )
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload + "\n")
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ServerLockError(f"cannot write {path}: {exc}") from exc
    return os.getpid()


def release_server_lock(layout: paths.Layout) -> None:
    """Remove the lock only when this process owns it."""
    if read_server_lock(layout) != os.getpid():
        return
    with contextlib.suppress(OSError):
        server_lock_path(layout).unlink(missing_ok=True)
