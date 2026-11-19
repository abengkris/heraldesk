"""Create and restore instance backups without external tools.

A backup is a ZIP archive containing a SQLite online-backup snapshot, the global config,
profile files and user prompt overrides. ``secret.key`` is included only when the caller
explicitly asks for credentials. ``.env``, logs, generated reports and previous backups
are never copied.

Restore validates the archive before touching the target. A backup without ``secret.key``
cannot restore the database: callers must either provide a credential backup or pass
``without_credentials=True`` to restore only config, profiles and agents.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from heraldesk import __version__, paths
from heraldesk.config import ConfigError, load_config
from heraldesk.core.locks import server_is_running
from heraldesk.db.engine import inspect_database

BACKUP_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
DB_NAME = "heraldesk.db"
CONFIG_NAME = "heraldesk.yaml"
KEY_NAME = "secret.key"
TELEGRAM_TOKEN_NAME = "telegram.token"
PROFILES_DIRNAME = "profiles"
AGENTS_DIRNAME = "agents"
BACKUP_MODE = 0o600
TREE_DIRS = (PROFILES_DIRNAME, AGENTS_DIRNAME)
ALLOWED_TOP_LEVEL = {
    MANIFEST_NAME,
    DB_NAME,
    CONFIG_NAME,
    KEY_NAME,
    TELEGRAM_TOKEN_NAME,
    *TREE_DIRS,
}
NO_KEY_MESSAGE = (
    "Backup has no secret.key, cannot decrypt social credentials. "
    "Restore profiles and config only with --without-credentials, or provide secret.key"
)


class BackupError(Exception):
    """A backup or restore operation cannot be completed safely."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    """Summary of one created archive."""

    path: Path
    includes_credentials: bool
    entries: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RestoreResult:
    """Summary of one completed restore."""

    layout: paths.Layout
    archive: Path
    restored_db: bool
    restored_credentials: bool
    entries: tuple[str, ...]
    safety_backup: Path | None = None


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _copy_tree_entries(root: Path, prefix: str) -> list[tuple[Path, str]]:
    if not root.is_dir():
        return []
    entries: list[tuple[Path, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root).as_posix()
        entries.append((path, f"{prefix}/{relative}"))
    return entries


def _snapshot_database(db_path: Path, destination: Path) -> None:
    source = sqlite3.connect(str(db_path))
    target = sqlite3.connect(str(destination))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


def _chmod_private(path: Path) -> None:
    if os.name != "posix":
        return
    try:
        path.chmod(BACKUP_MODE)
    except OSError as exc:
        raise BackupError(f"cannot set permissions on {path}: {exc}") from exc


def _manifest(
    *,
    created_at: datetime,
    includes_credentials: bool,
    has_config: bool,
    has_telegram_token: bool,
    profile_count: int,
    agent_count: int,
) -> dict[str, object]:
    return {
        "format_version": BACKUP_FORMAT_VERSION,
        "heraldesk_version": __version__,
        "created_at": created_at.isoformat(),
        "includes_credentials": includes_credentials,
        "has_config": has_config,
        "has_telegram_token": has_telegram_token,
        "profile_files": profile_count,
        "agent_files": agent_count,
    }


def create_backup(
    layout: paths.Layout,
    *,
    output: Path | None = None,
    include_credentials: bool = False,
    now: datetime | None = None,
) -> BackupResult:
    """Write one ZIP backup and return its path and contents summary."""
    if not layout.home.is_dir():
        raise BackupError(f"data folder {layout.home} does not exist; run 'heraldesk init' first")
    if not layout.db_path.is_file():
        raise BackupError(f"database {layout.db_path} does not exist; run 'heraldesk init' first")
    if include_credentials and not layout.key_path.is_file():
        raise BackupError("secret.key is missing; cannot create a credential backup")

    current = _utc_now(now)
    if output is None:
        try:
            layout.backups.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise BackupError(f"cannot create {layout.backups}: {exc}") from exc
        destination = layout.backups / (
            f"heraldesk-backup-{current.strftime('%Y%m%dT%H%M%S%fZ')}.zip"
        )
    else:
        destination = output.expanduser()
        if not destination.parent.is_dir():
            raise BackupError(f"output folder {destination.parent} does not exist")
        if destination.is_dir():
            raise BackupError(f"output path {destination} is a directory")

    profile_entries = _copy_tree_entries(layout.profiles, PROFILES_DIRNAME)
    agent_entries = _copy_tree_entries(layout.agents, AGENTS_DIRNAME)
    include_token = include_credentials and layout.telegram_token_path.is_file()
    entries = [
        MANIFEST_NAME,
        DB_NAME,
        *([CONFIG_NAME] if layout.config_path.is_file() else []),
        *([KEY_NAME] if include_credentials else []),
        *([TELEGRAM_TOKEN_NAME] if include_token else []),
        *(name for _path, name in profile_entries),
        *(name for _path, name in agent_entries),
    ]

    temporary_archive = destination.with_name(f".{destination.name}.tmp")
    try:
        with tempfile.TemporaryDirectory(prefix="heraldesk-backup-") as temporary:
            snapshot = Path(temporary) / DB_NAME
            _snapshot_database(layout.db_path, snapshot)
            manifest = _manifest(
                created_at=current,
                includes_credentials=include_credentials,
                has_config=layout.config_path.is_file(),
                has_telegram_token=include_token,
                profile_count=len(profile_entries),
                agent_count=len(agent_entries),
            )
            with zipfile.ZipFile(
                temporary_archive,
                mode="w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(
                    MANIFEST_NAME,
                    json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                )
                archive.write(snapshot, DB_NAME)
                if layout.config_path.is_file():
                    archive.write(layout.config_path, CONFIG_NAME)
                if include_credentials:
                    archive.write(layout.key_path, KEY_NAME)
                    if include_token:
                        archive.write(layout.telegram_token_path, TELEGRAM_TOKEN_NAME)
                for path, name in profile_entries:
                    archive.write(path, name)
                for path, name in agent_entries:
                    archive.write(path, name)
        os.replace(temporary_archive, destination)
    except (OSError, sqlite3.Error, zipfile.BadZipFile) as exc:
        raise BackupError(f"cannot write backup {destination}: {exc}") from exc
    finally:
        if temporary_archive.exists():
            temporary_archive.unlink(missing_ok=True)

    _chmod_private(destination)
    return BackupResult(
        path=destination,
        includes_credentials=include_credentials,
        entries=tuple(entries),
    )


def prune_backups(
    layout: paths.Layout,
    *,
    keep: int,
    dry_run: bool = False,
) -> list[Path]:
    """Delete old ``heraldesk-backup-*.zip`` files, keeping the newest *keep*."""
    if keep < 1:
        raise BackupError("keep must be at least 1")
    if not layout.backups.is_dir():
        return []
    try:
        candidates = sorted(
            layout.backups.glob("heraldesk-backup-*.zip"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
    except OSError as exc:
        raise BackupError(f"cannot list {layout.backups}: {exc}") from exc
    selected = candidates[keep:]
    if dry_run:
        return selected
    removed: list[Path] = []
    for path in selected:
        try:
            path.unlink()
        except OSError as exc:
            raise BackupError(f"cannot remove {path}: {exc}") from exc
        removed.append(path)
    return removed


def _safe_members(archive: zipfile.ZipFile) -> list[str]:
    names = archive.namelist()
    if len(names) != len(set(names)):
        raise BackupError("backup archive contains duplicate entries")
    for name in names:
        if "\\" in name:
            raise BackupError(f"backup archive contains an unsafe path: {name}")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise BackupError(f"backup archive contains an unsafe path: {name}")
        if not relative.parts or relative.parts[0] not in ALLOWED_TOP_LEVEL:
            raise BackupError(f"backup archive contains an unexpected entry: {name}")
    return names


def _extract_members(
    archive: zipfile.ZipFile,
    names: list[str],
    root: Path,
) -> None:
    for name in names:
        if name.endswith("/"):
            continue
        relative = PurePosixPath(name)
        target = root.joinpath(*relative.parts)
        try:
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with archive.open(name) as source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination)
        except OSError as exc:
            raise BackupError(f"cannot extract {name}: {exc}") from exc


def _validate_extracted(root: Path) -> None:
    config_path = root / CONFIG_NAME
    if config_path.is_file():
        try:
            load_config(root)
        except ConfigError as exc:
            raise BackupError(f"backup config is invalid: {exc}") from exc

    database_path = root / DB_NAME
    if not database_path.is_file():
        return
    status = inspect_database(database_path)
    if status.error is not None:
        raise BackupError(f"backup database is unreadable: {status.error}")
    if not status.revision:
        raise BackupError("backup database has no schema revision")


def _target_has_data(layout: paths.Layout) -> bool:
    for path in (layout.db_path, layout.config_path, layout.key_path):
        if path.exists():
            return True
    for directory in (layout.profiles, layout.agents):
        if directory.exists() and (not directory.is_dir() or any(directory.iterdir())):
            return True
    return False


def _remove_path(path: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
    except OSError as exc:
        raise BackupError(f"cannot remove {path}: {exc}") from exc


def _copy_file(source: Path, destination: Path) -> None:
    try:
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    except OSError as exc:
        raise BackupError(f"cannot write {destination}: {exc}") from exc


def _apply_restore(
    layout: paths.Layout,
    root: Path,
    *,
    without_credentials: bool,
    force: bool,
) -> None:
    """Stage the selected files, then swap them into the data folder.

    Extraction and copying happen in a staging directory on the same filesystem, so a
    failed copy leaves the live instance untouched. Replacing the final paths is quick;
    callers still keep a pre-restore safety backup for the remaining window.
    """
    try:
        paths.ensure_layout(layout.home)
    except (OSError, paths.LayoutError) as exc:
        raise BackupError(str(exc)) from exc

    staging = Path(tempfile.mkdtemp(prefix=".heraldesk-restore-", dir=layout.home))
    replacements: list[tuple[Path, Path]] = []
    try:
        staged_db = staging / DB_NAME
        if not without_credentials:
            database_source = root / DB_NAME
            if not database_source.is_file():
                raise BackupError("backup archive has no heraldesk.db")
            _copy_file(database_source, staged_db)
            replacements.append((staged_db, layout.db_path))

        config_source = root / CONFIG_NAME
        if config_source.is_file():
            staged_config = staging / CONFIG_NAME
            _copy_file(config_source, staged_config)
            replacements.append((staged_config, layout.config_path))

        if not without_credentials:
            key_source = root / KEY_NAME
            if key_source.is_file():
                staged_key = staging / KEY_NAME
                _copy_file(key_source, staged_key)
                if os.name == "posix":
                    staged_key.chmod(0o600)
                replacements.append((staged_key, layout.key_path))
            token_source = root / TELEGRAM_TOKEN_NAME
            if token_source.is_file():
                staged_token = staging / TELEGRAM_TOKEN_NAME
                _copy_file(token_source, staged_token)
                if os.name == "posix":
                    staged_token.chmod(0o600)
                replacements.append((staged_token, layout.telegram_token_path))

        for directory_name, destination in (
            (PROFILES_DIRNAME, layout.profiles),
            (AGENTS_DIRNAME, layout.agents),
        ):
            source = root / directory_name
            staged_directory = staging / directory_name
            if source.is_dir():
                try:
                    shutil.copytree(source, staged_directory)
                except OSError as exc:
                    raise BackupError(f"cannot stage {destination}: {exc}") from exc
            else:
                staged_directory.mkdir(mode=0o700)
            replacements.append((staged_directory, destination))

        for staged, target in replacements:
            if target == layout.db_path:
                _remove_path(Path(f"{layout.db_path}-wal"))
                _remove_path(Path(f"{layout.db_path}-shm"))
            _remove_path(target)
            try:
                os.replace(staged, target)
            except OSError as exc:
                raise BackupError(f"cannot replace {target}: {exc}") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _selected_members(names: list[str], *, without_credentials: bool) -> list[str]:
    selected: list[str] = []
    for name in names:
        if name in (MANIFEST_NAME, CONFIG_NAME):
            selected.append(name)
            continue
        if name.startswith(f"{PROFILES_DIRNAME}/"):
            selected.append(name)
            continue
        if name.startswith(f"{AGENTS_DIRNAME}/"):
            selected.append(name)
            continue
        if not without_credentials and name in {DB_NAME, KEY_NAME, TELEGRAM_TOKEN_NAME}:
            selected.append(name)
    return selected


def restore_backup(
    layout: paths.Layout,
    archive_path: Path,
    *,
    without_credentials: bool = False,
    force: bool = False,
) -> RestoreResult:
    """Restore one archive, validating it before replacing target files."""
    archive_path = archive_path.expanduser()
    if not archive_path.is_file():
        raise BackupError(f"backup archive {archive_path} does not exist")
    running = server_is_running(layout)
    if running is not None:
        raise BackupError(
            f"heraldesk serve is running (pid {running}); stop it before restoring a backup"
        )

    if not force and _target_has_data(layout):
        raise BackupError(f"{layout.home} already contains data; pass --force to replace it")

    try:
        with zipfile.ZipFile(archive_path) as archive:
            names = _safe_members(archive)
            has_key = KEY_NAME in names
            has_database = DB_NAME in names
            if not has_key and not without_credentials:
                raise BackupError(NO_KEY_MESSAGE)
            if not without_credentials and not has_database:
                raise BackupError("backup archive has no heraldesk.db")
            if MANIFEST_NAME not in names:
                raise BackupError("backup archive has no manifest.json")

            selected = _selected_members(names, without_credentials=without_credentials)
            safety_backup: Path | None = None
            if force and _target_has_data(layout) and layout.db_path.is_file():
                try:
                    safety_backup = create_backup(
                        layout,
                        include_credentials=True,
                    ).path
                except BackupError as exc:
                    raise BackupError(f"cannot create a pre-restore safety backup: {exc}") from exc
            with tempfile.TemporaryDirectory(prefix="heraldesk-restore-") as temporary:
                root = Path(temporary)
                _extract_members(archive, selected, root)
                manifest_path = root / MANIFEST_NAME
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise BackupError("backup manifest is not valid JSON") from exc
                if not isinstance(manifest, dict):
                    raise BackupError("backup manifest is not a JSON object")
                if manifest.get("format_version") != BACKUP_FORMAT_VERSION:
                    raise BackupError("backup format version is not supported")
                _validate_extracted(root)
                _apply_restore(
                    layout,
                    root,
                    without_credentials=without_credentials,
                    force=force,
                )
    except zipfile.BadZipFile as exc:
        raise BackupError(f"{archive_path} is not a valid backup archive") from exc
    except OSError as exc:
        raise BackupError(f"cannot restore {archive_path}: {exc}") from exc

    return RestoreResult(
        layout=layout,
        archive=archive_path,
        restored_db=not without_credentials,
        restored_credentials=has_key and not without_credentials,
        entries=tuple(selected),
        safety_backup=safety_backup,
    )
