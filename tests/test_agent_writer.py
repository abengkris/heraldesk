"""Writer agent end to end with the fake provider."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from heraldesk.agents.base import AgentContext
from heraldesk.agents.runner import AgentRunner
from heraldesk.agents.schemas import Draft, WriterInput
from heraldesk.agents.writer import Writer
from heraldesk.core.audit import AuditService
from heraldesk.db.models import AgentRun
from heraldesk.db.repositories import AgentRunRepository
from heraldesk.llm.fake import FakeLLM
from heraldesk.platforms.base import constraints_for
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core


def test_writer_produces_a_validated_draft_and_records_the_run(
    profile_setup: ProfileSetup, db_session
) -> None:
    provider = FakeLLM()
    runner = AgentRunner(
        db_session,
        provider,
        layout=profile_setup.layout,
        model=provider.default_model,
        cap_usd=5.0,
    )
    context = AgentContext(
        scope=profile_setup.scope,
        profile=profile_setup.definition,
        platform="fake",
        constraints=constraints_for("fake"),
    )

    result = runner.run(
        Writer(),
        context,
        WriterInput(idea="Announce the smaller dashboard", platform="fake"),
    )

    assert isinstance(result.output, Draft)
    assert result.output.body
    assert result.attempts == 1
    assert result.tokens_in > 0
    assert result.tokens_out > 0
    assert result.cost_usd > 0
    assert result.model == "fake/fake"

    run = db_session.scalar(select(AgentRun).where(AgentRun.id == result.run_id))
    assert run is not None
    assert run.status == "succeeded"
    assert run.agent == "writer"
    assert run.tokens_in == result.tokens_in
    assert run.tokens_out == result.tokens_out
    assert run.cost_usd == result.cost_usd
    assert run.output is not None
    assert run.output["body"] == result.output.body

    actions = [entry.action for entry in AuditService(db_session).history(profile_setup.scope)]
    assert "agent.run_succeeded" in actions


def test_run_traces_are_scoped_per_profile(profile_setup: ProfileSetup, db_session) -> None:
    provider = FakeLLM()
    runner = AgentRunner(
        db_session,
        provider,
        layout=profile_setup.layout,
        model=provider.default_model,
        cap_usd=5.0,
    )
    context = AgentContext(
        scope=profile_setup.scope,
        profile=profile_setup.definition,
        platform="fake",
        constraints=constraints_for("fake"),
    )
    result = runner.run(Writer(), context, WriterInput(idea="One idea", platform="fake"))

    repository = AgentRunRepository(db_session)
    assert [run.id for run in repository.list(profile_setup.scope)] == [result.run_id]
    totals = repository.totals_since(profile_setup.scope, datetime(2000, 1, 1, tzinfo=UTC))
    assert totals.runs == 1
    assert totals.cost_usd == pytest.approx(result.cost_usd)
