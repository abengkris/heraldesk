"""Profile file loading and schema validation tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from heraldesk.core.bootstrap import InitResult
from heraldesk.domain.errors import ProfileValidationError
from heraldesk.domain.policy import PolicyMode
from heraldesk.domain.profiles import (
    DEFAULT_BANNED_YAML,
    DEFAULT_POLICY_YAML,
    ProfileType,
    load_profile,
    write_profile_skeleton,
)

pytestmark = pytest.mark.core


def write_profile_dir(
    root: Path,
    slug: str = "acme",
    *,
    soul: str | None = None,
    brief: str = "# Brief\n\nAudience and goals.\n",
    policy: str = DEFAULT_POLICY_YAML,
    banned: str = DEFAULT_BANNED_YAML,
) -> Path:
    directory = root / slug
    directory.mkdir(parents=True)
    (directory / "soul.md").write_text(
        soul if soul is not None else "# Acme\n\nBe direct.\n", encoding="utf-8"
    )
    (directory / "brief.md").write_text(brief, encoding="utf-8")
    (directory / "policy.yaml").write_text(policy, encoding="utf-8")
    (directory / "banned.yaml").write_text(banned, encoding="utf-8")
    return directory


def test_skeleton_loads_with_front_matter_metadata(initialized: InitResult) -> None:
    directory = write_profile_skeleton(
        initialized.layout.profiles,
        "acme",
        name="Acme Corp",
        profile_type=ProfileType.COMPANY,
        timezone="UTC",
        languages=["en", "id"],
    )
    definition = load_profile(directory)
    assert definition.slug == "acme"
    assert definition.name == "Acme Corp"
    assert definition.type is ProfileType.COMPANY
    assert definition.timezone == "UTC"
    assert definition.languages == ("en", "id")
    assert definition.policy.default_mode is PolicyMode.DRAFT_ONLY
    assert definition.policy.rules == []
    assert definition.banned.banned_words == []
    assert definition.voice_samples == ()
    assert len(definition.config_hash) == 64


def test_skeleton_writes_the_documented_profile_files(initialized: InitResult) -> None:
    directory = write_profile_skeleton(initialized.layout.profiles, "acme")
    assert (directory / "soul.md").is_file()
    assert (directory / "brief.md").is_file()
    assert (directory / "policy.yaml").is_file()
    assert (directory / "banned.yaml").is_file()
    assert (directory / "voice").is_dir()
    assert (directory / "voice" / ".gitkeep").is_file()


def test_metadata_defaults_without_front_matter(initialized: InitResult) -> None:
    directory = write_profile_dir(initialized.layout.profiles, "some-brand")
    definition = load_profile(directory)
    assert definition.name == "Some Brand"
    assert definition.type is ProfileType.OTHER
    assert definition.timezone == "UTC"
    assert definition.languages == ("en",)


def test_front_matter_rejects_unknown_keys(initialized: InitResult) -> None:
    soul = "---\nname: Acme\nautopost: true\n---\n\n# Acme\n"
    directory = write_profile_dir(initialized.layout.profiles, soul=soul)
    with pytest.raises(ProfileValidationError, match="autopost"):
        load_profile(directory)


def test_missing_required_file_names_the_path(initialized: InitResult) -> None:
    directory = write_profile_skeleton(initialized.layout.profiles, "acme")
    (directory / "policy.yaml").unlink()
    with pytest.raises(ProfileValidationError) as error:
        load_profile(directory)
    assert "policy.yaml" in str(error.value)
    assert "missing" in str(error.value)


def test_empty_brief_is_rejected(initialized: InitResult) -> None:
    directory = write_profile_dir(initialized.layout.profiles, brief="   \n")
    with pytest.raises(ProfileValidationError, match=r"brief\.md is empty"):
        load_profile(directory)


def test_malformed_policy_yaml_points_at_the_file(initialized: InitResult) -> None:
    directory = write_profile_dir(initialized.layout.profiles, policy="rules: [\n")
    with pytest.raises(ProfileValidationError) as error:
        load_profile(directory)
    message = str(error.value)
    assert "policy.yaml" in message
    assert "invalid YAML" in message


def test_invalid_utf8_is_rejected(initialized: InitResult) -> None:
    directory = write_profile_dir(initialized.layout.profiles)
    (directory / "brief.md").write_bytes(b"\xff\xfe\x00")
    with pytest.raises(ProfileValidationError, match="not valid UTF-8"):
        load_profile(directory)


def test_invalid_slug_directory_is_rejected(initialized: InitResult) -> None:
    directory = write_profile_dir(initialized.layout.profiles, slug="Not A Slug")
    with pytest.raises(ProfileValidationError, match="profile slug"):
        load_profile(directory)


def test_invalid_timezone_in_skeleton_is_rejected(initialized: InitResult) -> None:
    with pytest.raises(ProfileValidationError, match="timezone"):
        write_profile_skeleton(initialized.layout.profiles, "acme", timezone="Mars/Olympus_Mons")


def test_config_hash_is_stable_and_changes_with_files(initialized: InitResult) -> None:
    directory = write_profile_skeleton(initialized.layout.profiles, "acme")
    first = load_profile(directory)
    second = load_profile(directory)
    assert first.config_hash == second.config_hash

    (directory / "brief.md").write_text("# Brief\n\nChanged.\n", encoding="utf-8")
    third = load_profile(directory)
    assert third.config_hash != first.config_hash


def test_voice_samples_are_sorted_and_filtered(initialized: InitResult) -> None:
    directory = write_profile_skeleton(initialized.layout.profiles, "acme")
    voice = directory / "voice"
    (voice / "b.md").write_text("Second sample.", encoding="utf-8")
    (voice / "a.txt").write_text("First sample.", encoding="utf-8")
    (voice / ".hidden.md").write_text("Hidden.", encoding="utf-8")
    (voice / "ignore.json").write_text("{}", encoding="utf-8")
    (voice / "empty.md").write_text("   \n", encoding="utf-8")

    definition = load_profile(directory)
    assert [sample.name for sample in definition.voice_samples] == ["a.txt", "b.md"]
    assert [sample.text for sample in definition.voice_samples] == [
        "First sample.",
        "Second sample.",
    ]


def test_voice_changes_affect_the_config_hash(initialized: InitResult) -> None:
    directory = write_profile_skeleton(initialized.layout.profiles, "acme")
    before = load_profile(directory)
    (directory / "voice" / "sample.md").write_text("New sample.", encoding="utf-8")
    after = load_profile(directory)
    assert after.config_hash != before.config_hash


def test_missing_profile_directory_is_reported(initialized: InitResult) -> None:
    with pytest.raises(ProfileValidationError, match="does not exist"):
        load_profile(initialized.layout.profiles / "missing")


def test_unterminated_front_matter_is_rejected(initialized: InitResult) -> None:
    directory = write_profile_dir(initialized.layout.profiles, soul="---\nname: Acme\n\n# Acme\n")
    with pytest.raises(ProfileValidationError, match="never closes"):
        load_profile(directory)
