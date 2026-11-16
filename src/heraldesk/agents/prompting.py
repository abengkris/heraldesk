"""Prompt templates and system-prompt assembly.

Bundled templates live in ``heraldesk/agents/prompts/<role>.md``. A file with the same name
in the user's ``$HERALDESK_HOME/agents/`` folder takes precedence, so identities can be
edited without touching the package.

The system prompt is deterministic text built only from the role template, the profile
files and platform constraints. Environment variables and API keys are never read here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from heraldesk import paths
from heraldesk.agents.errors import AgentConfigError
from heraldesk.domain.profiles import ProfileDefinition
from heraldesk.platforms.base import PlatformConstraints

BUNDLED_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
ROLE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    """A loaded role prompt and where it came from."""

    role: str
    text: str
    source: Path
    overridden: bool


def validate_role(role: str) -> str:
    """Validate an agent role name used as a file stem."""
    if not ROLE_PATTERN.fullmatch(role):
        raise AgentConfigError(
            f"invalid agent role {role!r}: use lowercase letters, digits, '-' and '_'"
        )
    return role


class PromptLibrary:
    """Load role prompts with user overrides from the data folder."""

    def __init__(self, layout: paths.Layout, *, bundled_dir: Path | None = None) -> None:
        self._layout = layout
        self._bundled_dir = BUNDLED_PROMPTS_DIR if bundled_dir is None else bundled_dir

    @property
    def override_dir(self) -> Path:
        """Return the folder where user prompt overrides live."""
        return self._layout.agents

    def load(self, role: str) -> PromptTemplate:
        """Return the override for *role* if present, else the bundled template."""
        validate_role(role)
        override = self.override_dir / f"{role}.md"
        if override.is_file():
            return PromptTemplate(
                role=role,
                text=self._read(override),
                source=override,
                overridden=True,
            )
        bundled = self._bundled_dir / f"{role}.md"
        if not bundled.is_file():
            raise AgentConfigError(
                f"no prompt template for agent '{role}': expected {bundled} "
                f"or an override at {override}"
            )
        return PromptTemplate(
            role=role,
            text=self._read(bundled),
            source=bundled,
            overridden=False,
        )

    @staticmethod
    def _read(path: Path) -> str:
        try:
            text = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AgentConfigError(f"cannot read prompt {path}: {exc}") from exc
        if not text.strip():
            raise AgentConfigError(f"prompt {path} is empty")
        return text.strip()


def _bullet_block(title: str, values: list[str]) -> str:
    if not values:
        return f"### {title}\n\n(none)\n"
    lines = "\n".join(f"- {value}" for value in values)
    return f"### {title}\n\n{lines}\n"


def assemble_system_prompt(
    *,
    template: PromptTemplate,
    profile: ProfileDefinition,
    constraints: PlatformConstraints | None,
    output_schema: type[BaseModel],
    memories: tuple[str, ...] = (),
) -> str:
    """Build the full system prompt for one agent run.

    Sections are labelled so a user reading a trace can tell role instructions from
    profile data. The schema is appended last so the model sees the exact contract.
    """
    parts: list[str] = [
        f"# Role: {template.role}\n",
        template.text.strip(),
        f"\n# Profile: {profile.name}\n",
        "## Identity and tone (soul.md)\n",
        profile.soul.strip(),
        "\n## Banned words and topics (banned.yaml)\n",
        _bullet_block("Banned words", profile.banned.banned_words),
        _bullet_block("Banned topics", profile.banned.banned_topics),
        _bullet_block("Required disclaimers", profile.banned.required_disclaimers),
        "\n## Language\n",
        "Write in: " + ", ".join(profile.languages),
    ]
    if constraints is not None:
        parts.extend(
            [
                "\n## Platform constraints\n",
                f"- platform: {constraints.platform}",
                f"- maximum characters: {constraints.max_characters}",
                f"- maximum hashtags: {constraints.max_hashtags}",
            ]
        )
        if constraints.notes:
            parts.append(f"- notes: {constraints.notes}")
    if memories:
        parts.extend(["\n## Remembered preferences\n", "\n".join(f"- {item}" for item in memories)])
    schema = json.dumps(output_schema.model_json_schema(), indent=2, sort_keys=True)
    parts.extend(
        [
            "\n## Output contract\n",
            "Reply with exactly one JSON object and nothing else. No markdown fences, "
            "no commentary. It must match this JSON Schema:\n",
            "```json",
            schema,
            "```",
        ]
    )
    return "\n".join(part if part.endswith("\n") else part + "\n" for part in parts).strip() + "\n"
