"""Safe editing for the four profile text files.

Shared by the Web UI profile editor and the Draft Room patch flow. The editor validates a
staged copy with the same loader used by the CLI, then swaps one known file in. It never
accepts arbitrary paths: the slug is validated, the directory must be registered and the
four filenames are fixed.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.db.repositories import ProfileCatalog
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.autonomy import loosens_autonomy
from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.policy import parse_policy_yaml
from heraldesk.domain.profiles import (
    load_profile,
    reload_profile,
    validate_slug,
)

PROFILE_FILES = ("soul.md", "brief.md", "policy.yaml", "banned.yaml")
MAX_FILE_BYTES = 128 * 1024
_PROFILE_EDIT_LOCK = threading.Lock()


class ProfileEditError(Exception):
    """A profile edit cannot be applied safely."""


class StaleProfileEdit(ProfileEditError):
    """The file changed on disk after the editor loaded it."""


class AutonomyConfirmationRequired(ProfileEditError):
    """A loosening change needs an explicit confirmation before it can be written."""


@dataclass(frozen=True, slots=True)
class ProfileEditState:
    """Current text of the editable files and the profile slug."""

    slug: str
    files: dict[str, str]


@dataclass(frozen=True, slots=True)
class PolicyHelperValues:
    """The four ``policy.yaml`` values the helper form can change."""

    default_mode: str
    max_posts_per_day: int
    quiet_start: str
    quiet_end: str


def content_hash(text: str) -> str:
    """Return the SHA-256 hex digest of a file's text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_profile_files(layout: paths.Layout, slug: str) -> ProfileEditState:
    """Read the four editable files for a registered profile."""
    validate_slug(slug)
    directory = layout.profiles / slug
    if not directory.is_dir():
        raise ProfileEditError(f"profile directory {directory} does not exist")
    files: dict[str, str] = {}
    for name in PROFILE_FILES:
        path = directory / name
        try:
            files[name] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ProfileEditError(f"cannot read {path}: {exc}") from exc
    return ProfileEditState(slug=slug, files=files)


def save_profile_files(
    session: Session,
    layout: paths.Layout,
    slug: str,
    files: dict[str, str],
) -> ProfileEditState:
    """Validate a staged profile, swap the files in, reload and audit."""
    validate_slug(slug)
    profile_id = ProfileCatalog(session).resolve(slug)
    if profile_id is None:
        raise ProfileEditError(f"profile '{slug}' is not registered")
    if set(files) != set(PROFILE_FILES):
        raise ProfileEditError("all four profile files are required")
    for name, text in files.items():
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            raise ProfileEditError(f"{name} is larger than {MAX_FILE_BYTES // 1024} KiB")

    directory = layout.profiles / slug
    if not directory.is_dir():
        raise ProfileEditError(f"profile directory {directory} does not exist")

    staging = Path(tempfile.mkdtemp(prefix=".profile-edit-", dir=layout.profiles))
    try:
        candidate = staging / slug
        try:
            shutil.copytree(directory, candidate)
        except OSError as exc:
            raise ProfileEditError(f"cannot stage the profile: {exc}") from exc
        for name, text in files.items():
            (candidate / name).write_text(text, encoding="utf-8")
        try:
            load_profile(candidate)
        except ProfileValidationError as exc:
            raise ProfileEditError(str(exc)) from exc

        changed = [name for name in PROFILE_FILES if files[name] != _read(directory / name)]
        for name, text in files.items():
            _replace_text(directory / name, text)

        try:
            summary = reload_profile(session, layout, slug, actor="user")
        except (ProfileValidationError, OSError) as exc:
            raise ProfileEditError(f"files were written but reload failed: {exc}") from exc
        AuditService(session).record(
            ProfileScope(profile_id),
            actor="user",
            action="profile.edited",
            entity="profile",
            entity_id=profile_id,
            payload={
                "slug": slug,
                "files": changed,
                "config_hash": summary.config_hash,
                "source": "web",
            },
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    return ProfileEditState(
        slug=slug,
        files={name: _read(directory / name) for name in PROFILE_FILES},
    )


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ProfileEditError(f"cannot read {path}: {exc}") from exc


def _replace_text(path: Path, text: str) -> None:
    temporary = path.with_name(f".{path.name}.edit")
    try:
        temporary.write_text(text, encoding="utf-8")
        temporary.replace(path)
    except OSError as exc:
        raise ProfileEditError(f"cannot write {path}: {exc}") from exc
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


def validate_profile_files(
    layout: paths.Layout,
    slug: str,
    files: dict[str, str],
) -> str | None:
    """Validate a staged profile without writing anything; return a clean error or None."""
    validate_slug(slug)
    if set(files) != set(PROFILE_FILES):
        return "all four profile files are required"
    directory = layout.profiles / slug
    if not directory.is_dir():
        return f"profile directory {slug} does not exist"
    for name, text in files.items():
        if len(text.encode("utf-8")) > MAX_FILE_BYTES:
            return f"{name} is larger than {MAX_FILE_BYTES // 1024} KiB"

    staging = Path(tempfile.mkdtemp(prefix=".profile-validate-", dir=layout.profiles))
    try:
        candidate = staging / slug
        try:
            shutil.copytree(directory, candidate)
        except OSError as exc:
            return f"cannot stage the profile: {exc}"
        for name, text in files.items():
            (candidate / name).write_text(text, encoding="utf-8")
        try:
            load_profile(candidate)
        except ProfileValidationError as exc:
            return _clean_error(str(exc), candidate)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return None


def save_profile_file(
    session: Session,
    layout: paths.Layout,
    slug: str,
    name: str,
    text: str,
    *,
    base_hash: str,
    actor: str = "user",
    confirm_loosening: bool = False,
) -> ProfileEditState:
    """Validate and save one profile file, rejecting a stale base hash.

    A change that loosens autonomy is refused unless ``confirm_loosening`` is true. The
    hash re-check, validation, loosening check, atomic write, reload and audit all run
    under the shared process lock.
    """
    validate_slug(slug)
    if name not in PROFILE_FILES:
        raise ProfileEditError(f"'{name}' is not an editable profile file")
    profile_id = ProfileCatalog(session).resolve(slug)
    if profile_id is None:
        raise ProfileEditError(f"profile '{slug}' is not registered")
    if len(text.encode("utf-8")) > MAX_FILE_BYTES:
        raise ProfileEditError(f"{name} is larger than {MAX_FILE_BYTES // 1024} KiB")

    directory = layout.profiles / slug
    if not directory.is_dir():
        raise ProfileEditError(f"profile directory {directory} does not exist")
    with _PROFILE_EDIT_LOCK:
        path = directory / name
        current = _read(path)
        if content_hash(current) != base_hash:
            raise StaleProfileEdit(f"{name} changed on disk since the editor loaded it")

        files = {
            item: (_read(directory / item) if item != name else text) for item in PROFILE_FILES
        }
        error = validate_profile_files(layout, slug, files)
        if error is not None:
            raise ProfileEditError(error)
        if loosens_autonomy(name, current, text) and not confirm_loosening:
            raise AutonomyConfirmationRequired(
                "this change loosens autonomy; confirm it explicitly before saving"
            )

        _replace_text(path, text)

        try:
            summary = reload_profile(session, layout, slug, actor=actor)
        except (ProfileValidationError, OSError) as exc:
            raise ProfileEditError(f"{name} was written but reload failed: {exc}") from exc
        changed = [name] if text != current else []
        AuditService(session).record(
            ProfileScope(profile_id),
            actor=actor,
            action="profile.edited",
            entity="profile",
            entity_id=profile_id,
            payload={
                "slug": slug,
                "files": changed,
                "config_hash": summary.config_hash,
                "source": "web",
            },
        )
        return ProfileEditState(
            slug=slug, files={item: _read(directory / item) for item in PROFILE_FILES}
        )


def policy_helper_values(text: str) -> PolicyHelperValues | None:
    """Return the helper form values, or ``None`` when the YAML cannot be parsed."""
    try:
        policy = parse_policy_yaml(text, source="policy.yaml")
    except ProfileValidationError:
        return None
    quiet = policy.quiet_hours
    return PolicyHelperValues(
        default_mode=policy.default_mode.value,
        max_posts_per_day=policy.max_posts_per_day,
        quiet_start=quiet.start if quiet else "",
        quiet_end=quiet.end if quiet else "",
    )


def apply_policy_helper(
    text: str,
    *,
    default_mode: str,
    max_posts_per_day: str,
    quiet_start: str,
    quiet_end: str,
) -> str | None:
    """Update four values with a minimal text edit, preserving comments and key order.

    Returns ``None`` when the file's shape is not safely editable this way, so callers
    can tell the user to edit the YAML directly instead of rewriting the file.
    """
    try:
        max_posts = int(max_posts_per_day)
    except (TypeError, ValueError):
        return None
    if max_posts < 0:
        return None
    start = quiet_start.strip()
    end = quiet_end.strip()
    if bool(start) != bool(end):
        return None

    lines = text.splitlines(keepends=True)
    top: dict[str, int] = {}
    duplicates: set[str] = set()
    for index, line in enumerate(lines):
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):", line)
        if match:
            key = match.group(1)
            if key in top:
                duplicates.add(key)
            else:
                top[key] = index
    if duplicates & {"default_mode", "max_posts_per_day", "quiet_hours"}:
        return None

    scalars = {"default_mode": default_mode, "max_posts_per_day": str(max_posts)}
    for key, value in scalars.items():
        if key in top:
            lines[top[key]] = _replace_scalar(lines[top[key]], key, value)
        else:
            if lines and not lines[-1].endswith(("\n", "\r")):
                lines[-1] += "\n"
            lines.append(f"{key}: {value}\n")

    quiet_index = top.get("quiet_hours")
    if not start and not end:
        if quiet_index is not None:
            lines = _remove_quiet_block(lines, quiet_index)
        return "".join(lines)

    quoted = (f'"{start}"', f'"{end}"')
    if quiet_index is None:
        if lines and not lines[-1].endswith(("\n", "\r")):
            lines[-1] += "\n"
        lines.extend(["quiet_hours:\n", f"  start: {quoted[0]}\n", f"  end: {quoted[1]}\n"])
        return "".join(lines)

    line = lines[quiet_index]
    body = line.rstrip("\r\n")
    newline = line[len(body) :]
    rest = body.split(":", 1)[1].strip()
    if rest and not rest.startswith("#"):
        if not (rest.startswith("{") and rest.endswith("}")):
            return None
        updated = _replace_flow_quiet(rest, quoted[0], quoted[1])
        if updated is None:
            return None
        lines[quiet_index] = f"quiet_hours: {updated}{newline}"
        return "".join(lines)

    updated_block = _replace_quiet_block(lines, quiet_index, quoted)
    if updated_block is None:
        return None
    return "".join(updated_block)


def _replace_scalar(line: str, key: str, value: str) -> str:
    body = line.rstrip("\r\n")
    newline = line[len(body) :]
    match = re.match(rf"^(\s*{re.escape(key)}:\s*)(.*)$", body)
    if match is None:
        return line
    rest = match.group(2)
    comment = ""
    marker = rest.find(" #")
    if marker >= 0:
        rest, comment = rest[:marker], rest[marker:]
    elif rest.startswith("#"):
        rest, comment = "", rest
    return f"{match.group(1)}{value}{comment}{newline}"


def _replace_flow_quiet(rest: str, start: str, end: str) -> str | None:
    inner = rest.strip()[1:-1]
    updated = inner
    for key, value in (("start", start), ("end", end)):
        pattern = re.compile(rf"(\b{key}\s*:\s*)([^,}}]+)")
        match = pattern.search(updated)
        if match is None:
            separator = ", " if updated.strip() else ""
            updated = f"{updated}{separator}{key}: {value}"
        else:
            updated = updated[: match.start()] + match.group(1) + value + updated[match.end() :]
    return "{" + updated + "}"


def _replace_quiet_block(
    lines: list[str], start_index: int, quoted: tuple[str, str]
) -> list[str] | None:
    end_index = _block_end(lines, start_index)
    indent = "  "
    positions: dict[str, int] = {}
    for index in range(start_index + 1, end_index):
        match = re.match(r"^(\s+)(start|end):", lines[index])
        if match:
            indent = match.group(1)
            positions[match.group(2)] = index
    if not positions:
        return None
    for key, value in (("start", quoted[0]), ("end", quoted[1])):
        if key in positions:
            lines[positions[key]] = _replace_scalar(lines[positions[key]], key, value)
        else:
            lines.insert(end_index, f"{indent}{key}: {value}\n")
            end_index += 1
    return lines


def _remove_quiet_block(lines: list[str], start_index: int) -> list[str]:
    del lines[start_index : _block_end(lines, start_index)]
    return lines


def _block_end(lines: list[str], start_index: int) -> int:
    """Return the index just past the last indented child of a top-level key."""
    end = start_index + 1
    for index in range(start_index + 1, len(lines)):
        line = lines[index]
        if line[:1].isspace() and line.strip():
            end = index + 1
        elif not line.strip():
            continue
        else:
            break
    return end


def _clean_error(message: str, candidate: Path) -> str:
    return message.replace(str(candidate), candidate.name)
