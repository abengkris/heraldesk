"""Structured-output retries, fail-closed behaviour and token accounting."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from heraldesk.agents.base import AgentContext
from heraldesk.agents.errors import AgentRunError
from heraldesk.agents.runner import AgentRunner
from heraldesk.agents.schemas import Draft, WriterInput
from heraldesk.agents.writer import Writer
from heraldesk.db.models import AgentRun
from heraldesk.llm.errors import LLMError
from heraldesk.llm.fake import FakeLLM, json_fixture
from heraldesk.platforms.base import constraints_for
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core


def make_context(profile_setup: ProfileSetup) -> AgentContext:
    return AgentContext(
        scope=profile_setup.scope,
        profile=profile_setup.definition,
        platform="fake",
        constraints=constraints_for("fake"),
    )


def make_runner(db_session, profile_setup: ProfileSetup, provider: FakeLLM) -> AgentRunner:
    return AgentRunner(
        db_session,
        provider,
        layout=profile_setup.layout,
        model=provider.default_model,
        cap_usd=5.0,
    )


def test_invalid_json_is_retried_once_then_succeeds(profile_setup, db_session) -> None:
    provider = FakeLLM(
        responses=[
            "this is not json",
            json_fixture(
                {
                    "body": "A short valid draft after one rejected reply.",
                    "variants": [],
                    "hashtags": [],
                }
            ),
        ]
    )
    result = make_runner(db_session, profile_setup, provider).run(
        Writer(), make_context(profile_setup), WriterInput(idea="Retry me", platform="fake")
    )

    assert result.attempts == 2
    assert len(provider.calls) == 2
    assert isinstance(result.output, Draft)
    # The correction prompt was appended after the rejected reply.
    assert len(provider.calls[1].messages) > len(provider.calls[0].messages)

    run = db_session.scalar(select(AgentRun).where(AgentRun.id == result.run_id))
    assert run is not None
    assert run.status == "succeeded"
    assert run.tokens_in == result.tokens_in


def test_retry_budget_is_two_retries_per_runner_call(profile_setup, db_session) -> None:
    provider = FakeLLM(responses=["still not json"])
    with pytest.raises(AgentRunError) as caught:
        make_runner(db_session, profile_setup, provider).run(
            Writer(), make_context(profile_setup), WriterInput(idea="Fail", platform="fake")
        )

    assert len(provider.calls) == 3
    run = db_session.scalar(select(AgentRun).where(AgentRun.id == caught.value.run_id))
    assert run is not None
    assert run.status == "failed"
    assert run.output is None
    assert run.error is not None
    assert "invalid output" in run.error
    assert "not valid JSON" in run.error


def test_schema_violation_is_retried_and_can_recover(profile_setup, db_session) -> None:
    provider = FakeLLM(
        responses=[
            json_fixture({"body": ""}),
            json_fixture(
                {"body": "Recovered after the schema error.", "variants": [], "hashtags": []}
            ),
        ]
    )
    result = make_runner(db_session, profile_setup, provider).run(
        Writer(), make_context(profile_setup), WriterInput(idea="Schema", platform="fake")
    )

    assert result.attempts == 2
    assert result.output.body == "Recovered after the schema error."


def test_provider_failure_is_retried_once_then_fails_closed(profile_setup, db_session) -> None:
    provider = FakeLLM(fail_with=LLMError("provider down"))
    with pytest.raises(AgentRunError) as caught:
        make_runner(db_session, profile_setup, provider).run(
            Writer(), make_context(profile_setup), WriterInput(idea="Boom", platform="fake")
        )

    assert len(provider.calls) == 2
    run = db_session.scalar(select(AgentRun).where(AgentRun.id == caught.value.run_id))
    assert run is not None
    assert run.status == "failed"
    assert run.error is not None
    assert "provider down" in run.error


def test_overlong_draft_counts_as_invalid_output(profile_setup, db_session) -> None:
    long_body = "x" * 6000
    provider = FakeLLM(
        responses=[json_fixture({"body": long_body, "variants": [], "hashtags": []})]
    )
    with pytest.raises(AgentRunError) as caught:
        make_runner(db_session, profile_setup, provider).run(
            Writer(), make_context(profile_setup), WriterInput(idea="Long", platform="fake")
        )

    assert len(provider.calls) == 3
    run = db_session.scalar(select(AgentRun).where(AgentRun.id == caught.value.run_id))
    assert run is not None
    assert run.error is not None
    assert "characters" in run.error


def test_json_inside_code_fences_is_accepted(profile_setup, db_session) -> None:
    fenced = (
        "```json\n"
        + json.dumps({"body": "Fenced but valid.", "variants": [], "hashtags": []})
        + "\n```"
    )
    provider = FakeLLM(responses=[fenced])
    result = make_runner(db_session, profile_setup, provider).run(
        Writer(), make_context(profile_setup), WriterInput(idea="Fences", platform="fake")
    )

    assert result.attempts == 1
    assert result.output.body == "Fenced but valid."
