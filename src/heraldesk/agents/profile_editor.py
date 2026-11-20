"""The Profile Editor: propose one full-file replacement, never write it."""

from __future__ import annotations

from pydantic import BaseModel

from heraldesk.agents.base import Agent, AgentContext
from heraldesk.agents.errors import AgentOutputError
from heraldesk.agents.schemas import ProfileEditInput, ProfilePatchProposal


class ProfileEditor(Agent):
    """Propose a change to one profile file without applying it."""

    role = "profile"
    tools = ()
    input_schema = ProfileEditInput
    output_schema = ProfilePatchProposal

    def build_user_prompt(self, payload: BaseModel) -> str:
        """Render the current file and the instruction as the user message."""
        data = ProfileEditInput.model_validate(payload)
        return (
            f"Edit the profile file `{data.file}`.\n"
            "The current content follows between delimiters:\n"
            "<<<CURRENT>>>\n"
            f"{data.current_text}\n"
            "<<<END_CURRENT>>>\n\n"
            "Instruction from the operator:\n"
            f"{data.instruction}\n\n"
            "Return the complete new file content in `proposed_text`. Preserve everything "
            "you are not asked to change. Return JSON only, matching the ProfilePatchProposal "
            "schema."
        )

    def validate_output(self, output: BaseModel, context: AgentContext) -> None:
        """Require a non-empty proposal and a matching file name."""
        _ = context
        if not isinstance(output, ProfilePatchProposal):
            raise AgentOutputError("profile output must be a ProfilePatchProposal")
        if not output.proposed_text.strip():
            raise AgentOutputError("proposed_text must not be empty")
        if not output.rationale.strip():
            raise AgentOutputError("a rationale is required")
