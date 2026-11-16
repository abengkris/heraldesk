"""AgentRunner: prompt assembly, provider call, validation, retries and run traces.

One :meth:`AgentRunner.run` call produces exactly one ``agent_runs`` row. Token and cost
columns are totals across every attempt of that run. The row and its audit entry are
committed immediately, so a failure is visible even when the caller's transaction is
rolled back afterwards.

Failure policy (M2): an invalid output is retried at most twice; a provider error is
retried once; then the run is marked ``failed`` and :class:`AgentRunError` is raised. No
step is ever silently skipped.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, NoReturn

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.agents.base import Agent, AgentContext
from heraldesk.agents.errors import AgentOutputError, AgentRunError
from heraldesk.agents.prompting import PromptLibrary, assemble_system_prompt
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import AgentRunRepository
from heraldesk.llm.budget import CostTracker
from heraldesk.llm.errors import DailyCostCapExceeded, LLMError
from heraldesk.llm.types import ChatMessage, LLMProvider, LLMRequest

MAX_OUTPUT_RETRIES = 2
MAX_PROVIDER_RETRIES = 1
MAX_ERROR_CHARS = 2000
MAX_ECHO_CHARS = 2000


@dataclass(frozen=True, slots=True)
class AgentRunResult:
    """A successful agent run with usage totals."""

    run_id: int
    agent: str
    output: BaseModel
    model: str
    tokens_in: int
    tokens_out: int
    cost_usd: float
    attempts: int


def _truncate(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "...[truncated]"


def _strip_code_fences(text: str) -> str:
    candidate = text.strip()
    if not candidate.startswith("```"):
        return candidate
    lines = candidate.splitlines()
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _compact_validation_error(exc: ValidationError) -> str:
    parts: list[str] = []
    for error in exc.errors()[:5]:
        location = ".".join(str(item) for item in error.get("loc", ())) or "(root)"
        parts.append(f"{location}: {error.get('msg', 'invalid value')}")
    return "; ".join(parts)


class AgentRunner:
    """Run one agent once, with structured output and hard failure semantics."""

    def __init__(
        self,
        session: Session,
        provider: LLMProvider,
        *,
        layout: paths.Layout,
        model: str,
        cap_usd: float,
        max_output_retries: int = MAX_OUTPUT_RETRIES,
        max_provider_retries: int = MAX_PROVIDER_RETRIES,
        temperature: float = 0.0,
        redactor: SecretRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
        pipeline_run_id: int | None = None,
        persist: bool = True,
    ) -> None:
        if max_output_retries < 0 or max_provider_retries < 0:
            raise ValueError("retry limits must not be negative")
        self._session = session
        self._provider = provider
        self._prompts = PromptLibrary(layout)
        self._runs = AgentRunRepository(session)
        self._audit = AuditService(session)
        self._budget = CostTracker(session, cap_usd)
        self._model = model
        self._max_output_retries = max_output_retries
        self._max_provider_retries = max_provider_retries
        self._temperature = temperature
        self._redactor = SecretRegistry.from_environment() if redactor is None else redactor
        self._clock: Callable[[], datetime] = (
            clock if clock is not None else lambda: datetime.now(UTC)
        )
        self._pipeline_run_id = pipeline_run_id
        self._persist = persist

    def run(
        self,
        agent: Agent,
        context: AgentContext,
        payload: BaseModel | Mapping[str, Any],
    ) -> AgentRunResult:
        """Assemble the prompt, call the provider and return a validated output.

        Raises :class:`AgentRunError` after recording a failed run when the provider or the
        schema cannot produce a valid output within the retry budget.
        """
        input_model = agent.input_schema.model_validate(payload)
        template = self._prompts.load(agent.role)
        system_prompt = assemble_system_prompt(
            template=template,
            profile=context.profile,
            constraints=context.constraints,
            output_schema=agent.output_schema,
            memories=context.memories,
        )
        messages: list[ChatMessage] = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=agent.build_user_prompt(input_model)),
        ]
        stored_input = self._redactor.redact_json(input_model.model_dump(mode="json"))
        if not isinstance(stored_input, dict):
            stored_input = {}

        started_at = self._clock()
        tokens_in = 0
        tokens_out = 0
        cost_usd = 0.0
        attempts = 0
        output_retries = 0
        provider_retries = 0
        last_model = self._model
        last_error = "no attempt was made"

        while True:
            try:
                self._budget.ensure_within_cap(now=self._clock(), pending_usd=cost_usd)
            except DailyCostCapExceeded as exc:
                self._fail(
                    agent=agent,
                    context=context,
                    input_payload=stored_input,
                    started_at=started_at,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    model=last_model,
                    attempts=attempts,
                    error=str(exc),
                )

            attempts += 1
            try:
                result = self._provider.complete(
                    LLMRequest(
                        messages=tuple(messages),
                        model=self._model,
                        agent=agent.role,
                        temperature=self._temperature,
                    )
                )
            except LLMError as exc:
                provider_retries += 1
                last_error = f"{type(exc).__name__}: {exc}"
                if provider_retries > self._max_provider_retries:
                    self._fail(
                        agent=agent,
                        context=context,
                        input_payload=stored_input,
                        started_at=started_at,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=cost_usd,
                        model=last_model,
                        attempts=attempts,
                        error=f"LLM call failed: {last_error}",
                    )
                continue
            except Exception as exc:
                self._fail(
                    agent=agent,
                    context=context,
                    input_payload=stored_input,
                    started_at=started_at,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    model=last_model,
                    attempts=attempts,
                    error=f"unexpected provider error {type(exc).__name__}: {exc}",
                )

            tokens_in += result.tokens_in
            tokens_out += result.tokens_out
            cost_usd += result.cost_usd
            last_model = result.model or last_model

            try:
                output = self._parse_output(agent, context, result.text)
            except AgentOutputError as exc:
                output_retries += 1
                last_error = str(exc)
                if output_retries > self._max_output_retries:
                    self._fail(
                        agent=agent,
                        context=context,
                        input_payload=stored_input,
                        started_at=started_at,
                        tokens_in=tokens_in,
                        tokens_out=tokens_out,
                        cost_usd=cost_usd,
                        model=last_model,
                        attempts=attempts,
                        error=f"invalid output: {last_error}",
                    )
                messages.append(
                    ChatMessage(role="assistant", content=_truncate(result.text, MAX_ECHO_CHARS))
                )
                messages.append(
                    ChatMessage(
                        role="user",
                        content=(
                            f"Your previous reply was rejected: {last_error}. "
                            "Reply again with exactly one JSON object matching the schema, "
                            "with no prose and no code fences."
                        ),
                    )
                )
                continue

            stored_output = self._redactor.redact_json(output.model_dump(mode="json"))
            run_id = 0
            if self._persist:
                run = self._runs.add(
                    context.scope,
                    agent=agent.role,
                    status="succeeded",
                    model=last_model,
                    input=stored_input,
                    output=stored_output if isinstance(stored_output, dict) else {},
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=round(cost_usd, 6),
                    started_at=started_at,
                    finished_at=self._clock(),
                    pipeline_run_id=self._pipeline_run_id,
                )
                run_id = run.id
                self._audit.record(
                    context.scope,
                    actor=f"agent:{agent.role}",
                    action="agent.run_succeeded",
                    entity="agent_run",
                    entity_id=run_id,
                    payload={
                        "agent": agent.role,
                        "status": "succeeded",
                        "model": last_model,
                        "attempts": attempts,
                        "cost_usd": round(cost_usd, 6),
                    },
                )
                self._session.commit()
            return AgentRunResult(
                run_id=run_id,
                agent=agent.role,
                output=output,
                model=last_model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=round(cost_usd, 6),
                attempts=attempts,
            )

    def _parse_output(self, agent: Agent, context: AgentContext, text: str) -> BaseModel:
        candidate = _strip_code_fences(text)
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError as exc:
            raise AgentOutputError(
                f"reply is not valid JSON ({exc.msg} at character {exc.pos})"
            ) from exc
        if not isinstance(data, dict):
            raise AgentOutputError("reply must be a JSON object")
        try:
            output = agent.output_schema.model_validate(data)
        except ValidationError as exc:
            raise AgentOutputError(_compact_validation_error(exc)) from exc
        agent.validate_output(output, context)
        return output

    def _fail(
        self,
        *,
        agent: Agent,
        context: AgentContext,
        input_payload: dict[str, Any],
        started_at: datetime,
        tokens_in: int,
        tokens_out: int,
        cost_usd: float,
        model: str,
        attempts: int,
        error: str,
    ) -> NoReturn:
        message = _truncate(self._redactor.redact(error), MAX_ERROR_CHARS)
        run_id: int | None = None
        if self._persist:
            run = self._runs.add(
                context.scope,
                agent=agent.role,
                status="failed",
                model=model,
                input=input_payload,
                output=None,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=round(cost_usd, 6),
                error=message,
                started_at=started_at,
                finished_at=self._clock(),
                pipeline_run_id=self._pipeline_run_id,
            )
            run_id = run.id
            self._audit.record(
                context.scope,
                actor=f"agent:{agent.role}",
                action="agent.run_failed",
                entity="agent_run",
                entity_id=run_id,
                payload={
                    "agent": agent.role,
                    "status": "failed",
                    "model": model,
                    "attempts": attempts,
                    "cost_usd": round(cost_usd, 6),
                },
            )
            self._session.commit()
        detail = "no LLM call was made" if attempts == 0 else f"failed after {attempts} LLM call(s)"
        label = f"run {run_id}" if run_id is not None else "dry run"
        raise AgentRunError(
            f"{agent.role} {label} {detail}: {message}",
            run_id=run_id,
        )
