"""The Writer agent: one idea in, one structured draft out.

M2 proof that the runtime works end to end. Compliance, lint and the approval queue are
deliberately absent; they arrive with their own milestones.
"""

from __future__ import annotations

from pydantic import BaseModel

from heraldesk.agents.base import Agent, AgentContext
from heraldesk.agents.errors import AgentOutputError
from heraldesk.agents.schemas import Draft, WriterInput


class Writer(Agent):
    """Turn a content idea into a platform-ready draft."""

    role = "writer"
    tools = ("read_voice_samples",)
    input_schema = WriterInput
    output_schema = Draft

    def build_user_prompt(self, payload: BaseModel) -> str:
        """Render the idea, angle, evidence and platform as the user message."""
        data = WriterInput.model_validate(payload)
        lines = [
            "Write one draft for the idea below.",
            "",
            f"Target platform: {data.platform}",
            f"Idea: {data.idea}",
        ]
        if data.angle:
            lines.append(f"Angle: {data.angle}")
        if data.evidence:
            lines.append("Evidence you may use:")
            lines.extend(f"- {item}" for item in data.evidence)
        if data.feedback:
            lines.append("A previous draft was rejected by deterministic lint:")
            lines.extend(f"- {item}" for item in data.feedback)
            lines.append("Rewrite the draft so every issue above is fixed.")
        lines.extend(
            [
                "",
                "Stay inside the platform limit from the system prompt. "
                "Return JSON only, matching the Draft schema.",
            ]
        )
        return "\n".join(lines) + "\n"

    def validate_output(self, output: BaseModel, context: AgentContext) -> None:
        """Reject drafts that exceed the platform length or hashtag limit."""
        if not isinstance(output, Draft):
            raise AgentOutputError("writer output must be a Draft")
        constraints = context.constraints
        if constraints is None:
            return
        if len(output.body) > constraints.max_characters:
            raise AgentOutputError(
                f"draft body is {len(output.body)} characters but "
                f"{constraints.platform} allows {constraints.max_characters}"
            )
        if len(output.hashtags) > constraints.max_hashtags:
            raise AgentOutputError(
                f"draft has {len(output.hashtags)} hashtags but "
                f"{constraints.platform} allows {constraints.max_hashtags}"
            )
