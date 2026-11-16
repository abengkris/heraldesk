"""Global and per-profile off switches.

The per-profile kill switch is a column on ``profiles``. The global kill switch is a
marker file inside the data folder so that ``heraldesk stop-all`` works even while the
database is busy or being migrated.

Precedence, highest first: global kill switch, per-profile kill switch, profile status
``paused``, profile status ``draft``. Everything else is allowed to publish.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from heraldesk import paths


class KillSwitchError(Exception):
    """Raised when the global kill-switch marker cannot be changed."""


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
