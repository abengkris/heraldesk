"""Profile files: loading, validation, hashing and registration.

A profile is defined by ``profiles/<slug>/`` containing ``soul.md``, ``brief.md``,
``policy.yaml``, ``banned.yaml`` and an optional ``voice/`` directory. The files are the
source of truth; SQLite only stores metadata and the hash of the validated files.

Profile metadata (name, type, timezone, languages) lives in optional YAML front matter at
the top of ``soul.md``. Without it the slug becomes the name and safe defaults apply.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.db.repositories import (
    ProfileCatalog,
    ProfileRepository,
    ProfileSummary,
    RecordNotFound,
    summarize,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import ProfileNotFoundError, ProfileValidationError
from heraldesk.domain.policy import (
    AutonomyPolicy,
    BannedLists,
    parse_banned_yaml,
    parse_policy_yaml,
)

SLUG_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LANGUAGE_PATTERN = re.compile(r"^[A-Za-z]{2,3}([_-][A-Za-z0-9]{2,8})*$")
VOICE_SUFFIXES = frozenset({".md", ".txt"})

DEFAULT_POLICY_YAML = """\
# Autonomy policy. Safe by default: every action needs a human.
# Rule matching: most specific (action + risk + intents) first, then file order,
# then default_mode. M4 modes: draft_only, require_approval, auto_publish.
# auto and auto_after_delay are reserved for the M5 scheduler.
default_mode: draft_only
rules: []
quiet_hours:
  start: "21:00"
  end: "07:00"
max_posts_per_day: 3
disclose_ai: false
# Telegram notifications for this profile: all, approvals_only or off.
notify: all
"""

DEFAULT_BANNED_YAML = """\
# Words and topics this profile never publishes. Disclaimers are required verbatim.
banned_words: []
banned_topics: []
required_disclaimers: []
# Optional lint overrides. Omit banned_buzzwords to use the built-in anti-slop list;
# set it to [] to turn the buzzword rule off for this profile.
# banned_buzzwords: []
# allow_em_dash: false
"""


class ProfileType(StrEnum):
    """Profile kinds accepted by the data model."""

    COMPANY = "company"
    PERSON = "person"
    PUBLIC_FIGURE = "public_figure"
    OTHER = "other"


class ProfileStatus(StrEnum):
    """Profile lifecycle status."""

    ACTIVE = "active"
    PAUSED = "paused"
    DRAFT = "draft"


class SoulMetadata(BaseModel):
    """Optional YAML front matter in ``soul.md``."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1)
    type: ProfileType = ProfileType.OTHER
    timezone: str = "UTC"
    languages: list[str] = Field(default_factory=lambda: ["en"])

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("timezone must not be empty")
        if value in {"UTC", "Etc/UTC"}:
            return value
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown IANA timezone {value!r}") from exc
        return value

    @field_validator("languages")
    @classmethod
    def _check_languages(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if not cleaned:
            raise ValueError("at least one language is required")
        for value in cleaned:
            if not LANGUAGE_PATTERN.fullmatch(value):
                raise ValueError(f"invalid language tag {value!r}")
        return list(dict.fromkeys(cleaned))


class VoiceSample(BaseModel):
    """One real writing sample used for few-shot style guidance."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    text: str


class ProfileDefinition(BaseModel):
    """A fully validated profile loaded from files."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    slug: str
    name: str
    type: ProfileType
    timezone: str
    languages: tuple[str, ...]
    soul: str
    brief: str
    policy: AutonomyPolicy
    banned: BannedLists
    voice_samples: tuple[VoiceSample, ...]
    config_hash: str
    path: Path


@dataclass(frozen=True, slots=True)
class ReloadOutcome:
    """Result of reloading one profile directory."""

    slug: str
    summary: ProfileSummary | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Return ``True`` when the profile loaded and registered."""
        return self.summary is not None and self.error is None


def validate_slug(slug: str) -> str:
    """Validate a profile slug used as a directory name."""
    if not slug or len(slug) > 64 or not SLUG_PATTERN.fullmatch(slug):
        raise ProfileValidationError(
            "profile slug must be lowercase letters, digits and single hyphens "
            "(for example 'acme' or 'my-brand'), at most 64 characters"
        )
    return slug


def normalize_languages(languages: Iterable[str]) -> tuple[str, ...]:
    """Normalize a language list and reject empty or malformed tags."""
    cleaned = [value.strip() for value in languages if value.strip()]
    if not cleaned:
        raise ProfileValidationError("at least one language is required")
    result: list[str] = []
    for value in cleaned:
        if not LANGUAGE_PATTERN.fullmatch(value):
            raise ProfileValidationError(f"invalid language tag {value!r}")
        if value not in result:
            result.append(value)
    return tuple(result)


def display_name_for_slug(slug: str) -> str:
    """Turn ``my-brand`` into ``My Brand`` for a generated profile."""
    return slug.replace("-", " ").title()


def compute_config_hash(entries: Iterable[tuple[str, bytes]]) -> str:
    """Hash the exact bytes of every profile file, independent of filesystem order."""
    hasher = hashlib.sha256()
    for name, data in sorted(entries, key=lambda item: item[0]):
        encoded = name.encode("utf-8")
        hasher.update(len(encoded).to_bytes(4, "big"))
        hasher.update(encoded)
        hasher.update(len(data).to_bytes(8, "big"))
        hasher.update(data)
    return hasher.hexdigest()


def _read_bytes(path: Path) -> bytes:
    if not path.is_file():
        raise ProfileValidationError(f"{path} is missing")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ProfileValidationError(f"cannot read {path}: {exc}") from exc


def _decode_text(path: Path, data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProfileValidationError(f"{path} is not valid UTF-8: {exc}") from exc


def _parse_front_matter(path: Path, text: str) -> tuple[SoulMetadata, str]:
    if not text.startswith("---"):
        return SoulMetadata(), text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return SoulMetadata(), text
    closing: int | None = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            closing = index
            break
    if closing is None:
        raise ProfileValidationError(f"{path}: front matter starts with '---' but never closes")
    front = "".join(lines[1:closing])
    body = "".join(lines[closing + 1 :])
    try:
        loaded = yaml.safe_load(front)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        location = f" at line {mark.line + 2}" if mark else ""
        problem = getattr(exc, "problem", None) or str(exc)
        raise ProfileValidationError(
            f"{path}: invalid front matter YAML{location}: {problem}"
        ) from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ProfileValidationError(f"{path}: front matter must be a YAML mapping")
    try:
        return SoulMetadata.model_validate(loaded), body
    except ValidationError as exc:
        raise ProfileValidationError(f"{path}: invalid profile metadata:\n{exc}") from exc


def _load_voice(directory: Path, raw: dict[str, bytes]) -> tuple[VoiceSample, ...]:
    if not directory.exists():
        return ()
    if not directory.is_dir():
        raise ProfileValidationError(f"{directory} exists but is not a directory")
    samples: list[VoiceSample] = []
    for path in sorted(directory.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() not in VOICE_SUFFIXES:
            continue
        data = _read_bytes(path)
        text = _decode_text(path, data)
        if not text.strip():
            continue
        raw[f"voice/{path.name}"] = data
        samples.append(VoiceSample(name=path.name, text=text))
    return tuple(samples)


def load_profile(profile_dir: Path) -> ProfileDefinition:
    """Load and validate one profile directory.

    Missing files, malformed YAML and Pydantic failures raise
    :class:`ProfileValidationError` with the offending path and field.
    """
    directory = profile_dir.expanduser()
    if not directory.is_dir():
        raise ProfileValidationError(f"profile directory {directory} does not exist")
    slug = validate_slug(directory.name)

    raw: dict[str, bytes] = {}
    soul_path = directory / "soul.md"
    soul_raw = _read_bytes(soul_path)
    raw["soul.md"] = soul_raw
    soul_text = _decode_text(soul_path, soul_raw)
    metadata, soul_body = _parse_front_matter(soul_path, soul_text)
    if not soul_body.strip():
        raise ProfileValidationError(f"{soul_path} has no content after the front matter")

    brief_path = directory / "brief.md"
    brief_raw = _read_bytes(brief_path)
    raw["brief.md"] = brief_raw
    brief = _decode_text(brief_path, brief_raw)
    if not brief.strip():
        raise ProfileValidationError(f"{brief_path} is empty")

    policy_path = directory / "policy.yaml"
    policy_raw = _read_bytes(policy_path)
    raw["policy.yaml"] = policy_raw
    policy = parse_policy_yaml(_decode_text(policy_path, policy_raw), source=str(policy_path))

    banned_path = directory / "banned.yaml"
    banned_raw = _read_bytes(banned_path)
    raw["banned.yaml"] = banned_raw
    banned = parse_banned_yaml(_decode_text(banned_path, banned_raw), source=str(banned_path))

    voice_samples = _load_voice(directory / "voice", raw)

    return ProfileDefinition(
        slug=slug,
        name=metadata.name or display_name_for_slug(slug),
        type=metadata.type,
        timezone=metadata.timezone,
        languages=tuple(metadata.languages),
        soul=soul_body.strip(),
        brief=brief.strip(),
        policy=policy,
        banned=banned,
        voice_samples=voice_samples,
        config_hash=compute_config_hash(raw.items()),
        path=directory,
    )


def write_profile_skeleton(
    profiles_root: Path,
    slug: str,
    *,
    name: str | None = None,
    profile_type: ProfileType = ProfileType.OTHER,
    timezone: str = "UTC",
    languages: Sequence[str] = ("en",),
) -> Path:
    """Create a non-interactive profile skeleton and return its directory.

    The profile starts in ``draft`` status with ``default_mode: draft_only``. The user
    edits the files, runs ``heraldesk profile reload <slug>`` and then ``profile resume``.
    """
    validate_slug(slug)
    display_name = (name or display_name_for_slug(slug)).strip()
    try:
        metadata = SoulMetadata(
            name=display_name,
            type=profile_type,
            timezone=timezone,
            languages=list(normalize_languages(languages)),
        )
    except ValidationError as exc:
        raise ProfileValidationError(f"invalid profile metadata:\n{exc}") from exc

    directory = profiles_root / slug
    profiles_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.mkdir(mode=0o700)
    (directory / "voice").mkdir(mode=0o700)

    front = yaml.safe_dump(
        metadata.model_dump(mode="json"),
        sort_keys=False,
        allow_unicode=True,
    ).strip()
    soul = f"""\
---
{front}
---

# {display_name}

Describe who this profile is, the tone of voice, and the always/never rules.
The Compliance agent reads this file before every draft.
"""
    brief = f"""\
# Brand brief: {display_name}

## Audience

Who are we speaking to?

## Goals

What should this account achieve?

## Content pillars

- Pillar one
- Pillar two

## Competitors and references

List a few accounts or sources to learn from.
"""
    (directory / "soul.md").write_text(soul, encoding="utf-8")
    (directory / "brief.md").write_text(brief, encoding="utf-8")
    (directory / "policy.yaml").write_text(DEFAULT_POLICY_YAML, encoding="utf-8")
    (directory / "banned.yaml").write_text(DEFAULT_BANNED_YAML, encoding="utf-8")
    (directory / "voice" / ".gitkeep").write_text("", encoding="utf-8")
    return directory


def resolve_scope(session: Session, slug: str) -> ProfileScope:
    """Resolve a slug to a :class:`ProfileScope` or raise a readable domain error."""
    validate_slug(slug)
    profile_id = ProfileCatalog(session).resolve(slug)
    if profile_id is None:
        raise ProfileNotFoundError(
            f"profile '{slug}' is not loaded; run 'heraldesk profile reload {slug}'"
        )
    return ProfileScope(profile_id)


def list_profiles(session: Session) -> list[ProfileSummary]:
    """Return registered profile metadata ordered by slug."""
    return ProfileCatalog(session).list()


def register_definition(
    session: Session, definition: ProfileDefinition
) -> tuple[ProfileSummary, bool]:
    """Insert or refresh a profile row. Returns the summary and whether it was created."""
    catalog = ProfileCatalog(session)
    existing_id = catalog.resolve(definition.slug)
    if existing_id is None:
        return summarize(catalog.create(definition)), True
    repository = ProfileRepository(session)
    updated = repository.update_from_definition(ProfileScope(existing_id), definition)
    return summarize(updated), False


def create_profile(
    session: Session,
    layout: paths.Layout,
    slug: str,
    *,
    name: str | None = None,
    profile_type: ProfileType = ProfileType.OTHER,
    timezone: str = "UTC",
    languages: Sequence[str] = ("en",),
    actor: str = "user",
) -> ProfileSummary:
    """Write a profile skeleton, validate it and register it in ``draft`` status."""
    validate_slug(slug)
    if ProfileCatalog(session).resolve(slug) is not None:
        raise ProfileValidationError(
            f"profile '{slug}' is already registered; use 'heraldesk profile reload {slug}'"
        )
    directory = write_profile_skeleton(
        layout.profiles,
        slug,
        name=name,
        profile_type=profile_type,
        timezone=timezone,
        languages=languages,
    )
    definition = load_profile(directory)
    summary, _ = register_definition(session, definition)
    AuditService(session).record(
        ProfileScope(summary.id),
        actor=actor,
        action="profile.created",
        entity="profile",
        entity_id=summary.id,
        payload={
            "slug": summary.slug,
            "status": summary.status,
            "config_hash": summary.config_hash,
        },
    )
    return summary


def reload_profile(
    session: Session, layout: paths.Layout, slug: str, *, actor: str = "user"
) -> ProfileSummary:
    """Validate one profile directory and refresh its metadata and hash."""
    validate_slug(slug)
    directory = layout.profiles / slug
    if not directory.is_dir():
        raise ProfileNotFoundError(f"profile directory {directory} does not exist")
    definition = load_profile(directory)
    summary, created = register_definition(session, definition)
    AuditService(session).record(
        ProfileScope(summary.id),
        actor=actor,
        action="profile.created" if created else "profile.reloaded",
        entity="profile",
        entity_id=summary.id,
        payload={
            "slug": summary.slug,
            "status": summary.status,
            "config_hash": summary.config_hash,
        },
    )
    return summary


def reload_all_profiles(
    session: Session, layout: paths.Layout, *, actor: str = "user"
) -> list[ReloadOutcome]:
    """Reload every profile directory, reporting one outcome per known profile."""
    directories: set[str] = set()
    if layout.profiles.is_dir():
        directories = {
            path.name
            for path in layout.profiles.iterdir()
            if path.is_dir() and not path.name.startswith(".")
        }
    registered = {summary.slug for summary in ProfileCatalog(session).list()}
    outcomes: list[ReloadOutcome] = []
    for slug in sorted(directories | registered):
        if slug not in directories:
            outcomes.append(
                ReloadOutcome(
                    slug=slug,
                    error=f"profile directory {layout.profiles / slug} is missing",
                )
            )
            continue
        try:
            summary = reload_profile(session, layout, slug, actor=actor)
            outcomes.append(ReloadOutcome(slug=slug, summary=summary))
        except (ProfileValidationError, ProfileNotFoundError, OSError) as exc:
            outcomes.append(ReloadOutcome(slug=slug, error=str(exc)))
    return outcomes


def _require_summary(session: Session, scope: ProfileScope) -> ProfileSummary:
    try:
        profile = ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ProfileNotFoundError(str(exc)) from exc
    return summarize(profile)


def set_profile_status(
    session: Session,
    scope: ProfileScope,
    status: ProfileStatus,
    *,
    actor: str = "user",
) -> tuple[ProfileSummary, bool]:
    """Change profile status. Returns the summary and whether it changed."""
    repository = ProfileRepository(session)
    try:
        profile = repository.require(scope)
    except RecordNotFound as exc:
        raise ProfileNotFoundError(str(exc)) from exc
    if profile.status == status.value:
        return summarize(profile), False
    updated = repository.set_status(scope, status.value)
    action = {
        ProfileStatus.ACTIVE: "profile.resumed",
        ProfileStatus.PAUSED: "profile.paused",
        ProfileStatus.DRAFT: "profile.drafted",
    }[status]
    AuditService(session).record(
        scope,
        actor=actor,
        action=action,
        entity="profile",
        entity_id=scope.profile_id,
        payload={"status": status.value},
    )
    return summarize(updated), True


def set_profile_kill_switch(
    session: Session,
    scope: ProfileScope,
    enabled: bool,
    *,
    actor: str = "user",
) -> tuple[ProfileSummary, bool]:
    """Set the per-profile kill switch. Returns the summary and whether it changed."""
    repository = ProfileRepository(session)
    try:
        profile = repository.require(scope)
    except RecordNotFound as exc:
        raise ProfileNotFoundError(str(exc)) from exc
    if profile.kill_switch == enabled:
        return summarize(profile), False
    updated = repository.set_kill_switch(scope, enabled)
    AuditService(session).record(
        scope,
        actor=actor,
        action="killswitch.engaged" if enabled else "killswitch.disengaged",
        entity="killswitch",
        entity_id=scope.profile_id,
        payload={"scope": "profile", "enabled": enabled},
    )
    return summarize(updated), True


def profile_summary(session: Session, scope: ProfileScope) -> ProfileSummary:
    """Return metadata for one scoped profile."""
    return _require_summary(session, scope)
