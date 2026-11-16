"""The Strategist agent: choose a small, explicit content plan for one day.

M3 keeps the Strategist intentionally simple: it picks slots and angles, not the whole
calendar. It never decides pipeline flow; the pipeline owns the steps.
"""

from __future__ import annotations

from pydantic import BaseModel

from heraldesk.agents.base import Agent
from heraldesk.agents.schemas import ContentPlan, StrategistInput


class Strategist(Agent):
    """Turn profile context and available platforms into a content plan."""

    role = "strategist"
    tools = ("read_metrics", "read_calendar")
    input_schema = StrategistInput
    output_schema = ContentPlan

    def build_user_prompt(self, payload: BaseModel) -> str:
        """Render the date, platforms and slot count as the user message."""
        data = StrategistInput.model_validate(payload)
        lines = [
            "Plan today's content slots for this profile.",
            f"Date: {data.date}",
            f"Available platforms: {', '.join(data.platforms)}",
            f"Slots to plan: {data.slots}",
            "",
            "Use the profile brief and pillars. Keep each slot concrete and non-duplicative.",
            "Return JSON only, matching the ContentPlan schema.",
        ]
        return "\n".join(lines) + "\n"
