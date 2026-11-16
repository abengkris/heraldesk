"""Prompt loading, override precedence and deterministic assembly."""

from __future__ import annotations

import pytest

from heraldesk.agents.errors import AgentConfigError
from heraldesk.agents.prompting import PromptLibrary, assemble_system_prompt
from heraldesk.agents.schemas import Draft
from heraldesk.domain.profiles import load_profile
from heraldesk.platforms.base import constraints_for
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core


def test_bundled_writer_prompt_is_used_by_default(profile_setup: ProfileSetup) -> None:
    template = PromptLibrary(profile_setup.layout).load("writer")
    assert template.overridden is False
    assert "Writer" in template.text
    assert template.source.name == "writer.md"


def test_user_override_takes_precedence(profile_setup: ProfileSetup) -> None:
    override = profile_setup.layout.agents / "writer.md"
    override.write_text("# Writer\n\nUse this custom house style.\n", encoding="utf-8")

    template = PromptLibrary(profile_setup.layout).load("writer")

    assert template.overridden is True
    assert template.source == override
    assert "custom house style" in template.text
    assert "You write social media drafts" not in template.text


def test_override_can_be_removed_again(profile_setup: ProfileSetup) -> None:
    override = profile_setup.layout.agents / "writer.md"
    override.write_text("temporary\n", encoding="utf-8")
    library = PromptLibrary(profile_setup.layout)
    assert library.load("writer").overridden is True

    override.unlink()
    assert library.load("writer").overridden is False


def test_invalid_role_cannot_escape_the_prompt_folder(profile_setup: ProfileSetup) -> None:
    library = PromptLibrary(profile_setup.layout)
    for role in ("../writer", "Writer", "", "writer/../x"):
        with pytest.raises(AgentConfigError):
            library.load(role)


def test_missing_prompt_is_a_config_error(profile_setup: ProfileSetup) -> None:
    with pytest.raises(AgentConfigError) as caught:
        PromptLibrary(profile_setup.layout).load("analyst")
    assert "analyst" in str(caught.value)


def test_system_prompt_contains_profile_identity_banned_list_and_schema(
    profile_setup: ProfileSetup,
) -> None:
    (profile_setup.layout.profiles / "acme" / "banned.yaml").write_text(
        "banned_words: [hustle]\nbanned_topics: [politics]\n"
        "required_disclaimers: [Not financial advice]\n",
        encoding="utf-8",
    )
    definition = load_profile(profile_setup.layout.profiles / "acme")
    template = PromptLibrary(profile_setup.layout).load("writer")

    prompt = assemble_system_prompt(
        template=template,
        profile=definition,
        constraints=constraints_for("fake"),
        output_schema=Draft,
    )

    assert "Describe who this profile is" in prompt
    assert "- hustle" in prompt
    assert "- politics" in prompt
    assert "- Not financial advice" in prompt
    assert "- platform: fake" in prompt
    assert "- maximum characters: 5000" in prompt
    assert '"body"' in prompt
    assert '"variants"' in prompt
    assert '"hashtags"' in prompt
    assert "Reply with exactly one JSON object" in prompt


def test_system_prompt_uses_the_user_override(profile_setup: ProfileSetup) -> None:
    override = profile_setup.layout.agents / "writer.md"
    override.write_text("# Writer\n\nHouse style marker XYZ.\n", encoding="utf-8")
    template = PromptLibrary(profile_setup.layout).load("writer")

    prompt = assemble_system_prompt(
        template=template,
        profile=profile_setup.definition,
        constraints=None,
        output_schema=Draft,
    )

    assert "House style marker XYZ." in prompt
    assert "You write social media drafts" not in prompt
