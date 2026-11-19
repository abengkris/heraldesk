"""Safe Web UI editing for the four profile text files.

The editor validates a staged copy with the same loader used by the CLI, then swaps the
files in. It never accepts arbitrary paths: the slug is validated, the directory must be
registered and the four filenames are fixed.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.db.repositories import ProfileCatalog
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.profiles import (
    load_profile,
    reload_profile,
    validate_slug,
)

PROFILE_FILES = ("soul.md", "brief.md", "policy.yaml", "banned.yaml")
MAX_FILE_BYTES = 128 * 1024


class ProfileEditError(Exception):
    """A profile edit cannot be applied safely."""


@dataclass(frozen=True, slots=True)
class ProfileEditState:
    """Current text of the editable files and the profile slug."""

    slug: str
    files: dict[str, str]


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
