"""The Compliance agent: assess risk and never approve by accident.

The pipeline treats a Compliance failure (provider down, invalid output after the retry
budget) as fail-closed: the draft becomes ``blocked`` with a reason, never
``pending_approval``. This module only returns the structured assessment.
"""

from __future__ import annotations

from pydantic import BaseModel

from heraldesk.agents.base import Agent, AgentContext
from heraldesk.agents.errors import AgentOutputError
from heraldesk.agents.schemas import ComplianceInput, RiskAssessment


class Compliance(Agent):
    """Assess one draft against profile rules and platform constraints."""

    role = "compliance"
    tools = ("banned_lists", "lint")
    input_schema = ComplianceInput
    output_schema = RiskAssessment

    def build_user_prompt(self, payload: BaseModel) -> str:
        """Render the draft body and platform as the user message."""
        data = ComplianceInput.model_validate(payload)
        return (
            "Assess this draft before a human sees it.\n"
            f"Platform: {data.platform}\n"
            "Draft body follows between delimiters:\n"
            "<<<DRAFT>>>\n"
            f"{data.body}\n"
            "<<<END_DRAFT>>>\n\n"
            "Flag legal, reputational, factual and platform-rule risk. "
            "Return JSON only, matching the RiskAssessment schema."
        )

    def validate_output(self, output: BaseModel, context: AgentContext) -> None:
        """Require a reason for every blocked verdict."""
        _ = context
        if not isinstance(output, RiskAssessment):
            raise AgentOutputError("compliance output must be a RiskAssessment")
        if output.risk_level == "blocked" and not output.reasons:
            raise AgentOutputError("a blocked verdict must include at least one reason")
