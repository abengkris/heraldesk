"""Daily cost cap and reporting windows."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from heraldesk.agents.base import AgentContext
from heraldesk.agents.errors import AgentRunError
from heraldesk.agents.runner import AgentRunner
from heraldesk.agents.schemas import WriterInput
from heraldesk.agents.writer import Writer
from heraldesk.db.repositories import AgentRunRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import register_definition
from heraldesk.llm.budget import profile_day_window, utc_day_window
from heraldesk.llm.fake import FakeLLM
from heraldesk.platforms.base import constraints_for
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


def make_context(profile_setup: ProfileSetup) -> AgentContext:
    return AgentContext(
        scope=profile_setup.scope,
        profile=profile_setup.definition,
        platform="fake",
        constraints=constraints_for("fake"),
    )


def make_runner(
    db_session, profile_setup: ProfileSetup, provider: FakeLLM, *, cap_usd: float = 5.0
) -> AgentRunner:
    return AgentRunner(
        db_session,
        provider,
        layout=profile_setup.layout,
        model=provider.default_model,
        cap_usd=cap_usd,
        clock=lambda: NOW,
    )


def seed_spend(
    db_session,
    scope: ProfileScope,
    amount: float,
    *,
    started_at: datetime = NOW,
) -> None:
    AgentRunRepository(db_session).add(
        scope,
        agent="writer",
        status="succeeded",
        model="fake/fake",
        input={},
        output={},
        tokens_in=10,
        tokens_out=10,
        cost_usd=amount,
        started_at=started_at,
        finished_at=started_at,
    )
    db_session.commit()


def test_cap_is_checked_before_the_call(profile_setup, db_session) -> None:
    seed_spend(db_session, profile_setup.scope, 5.0)
    provider = FakeLLM()

    with pytest.raises(AgentRunError) as caught:
        make_runner(db_session, profile_setup, provider).run(
            Writer(), make_context(profile_setup), WriterInput(idea="Blocked", platform="fake")
        )

    assert len(provider.calls) == 0
    assert "cost cap" in str(caught.value)
    assert caught.value.run_id is not None

    runs = AgentRunRepository(db_session).list(profile_setup.scope, limit=5)
    failed = runs[0]
    assert failed.status == "failed"
    assert failed.error is not None
    assert "cost cap" in failed.error
    assert failed.tokens_in == 0
    assert failed.cost_usd == 0.0


def test_cap_is_global_across_profiles(profile_setup, db_session, make_definition) -> None:
    seed_spend(db_session, profile_setup.scope, 5.0)
    beta_definition = make_definition("beta")
    beta_summary, _ = register_definition(db_session, beta_definition)
    db_session.commit()
    beta = ProfileScope(beta_summary.id)
    provider = FakeLLM()

    context = AgentContext(
        scope=beta,
        profile=beta_definition,
        platform="fake",
        constraints=constraints_for("fake"),
    )
    runner = AgentRunner(
        db_session,
        provider,
        layout=profile_setup.layout,
        model=provider.default_model,
        cap_usd=5.0,
        clock=lambda: NOW,
    )
    with pytest.raises(AgentRunError):
        runner.run(Writer(), context, WriterInput(idea="Blocked elsewhere", platform="fake"))

    assert len(provider.calls) == 0


def test_previous_day_spend_does_not_count(profile_setup, db_session) -> None:
    seed_spend(
        db_session, profile_setup.scope, 4.99, started_at=datetime(2026, 11, 15, 23, 0, tzinfo=UTC)
    )
    provider = FakeLLM()

    result = make_runner(db_session, profile_setup, provider).run(
        Writer(), make_context(profile_setup), WriterInput(idea="New day", platform="fake")
    )

    assert len(provider.calls) == 1
    assert result.cost_usd > 0


def test_pending_cost_from_earlier_retries_counts(profile_setup, db_session) -> None:
    seed_spend(db_session, profile_setup.scope, 4.9999)
    provider = FakeLLM(responses=["not json", "still not json", "not json either"])

    with pytest.raises(AgentRunError):
        make_runner(db_session, profile_setup, provider).run(
            Writer(), make_context(profile_setup), WriterInput(idea="Budgets", platform="fake")
        )

    # The first attempt's cost pushes the total over the cap before a retry is allowed.
    assert len(provider.calls) == 1


def test_profile_day_window_uses_the_profile_timezone() -> None:
    window = profile_day_window(NOW, "Asia/Jakarta")
    assert window.start == datetime(2026, 11, 15, 17, 0, tzinfo=UTC)
    assert window.end == datetime(2026, 11, 16, 17, 0, tzinfo=UTC)

    late = datetime(2026, 11, 16, 20, 0, tzinfo=UTC)
    shifted = profile_day_window(late, "Asia/Jakarta")
    assert shifted.start == datetime(2026, 11, 16, 17, 0, tzinfo=UTC)
    assert shifted.end == datetime(2026, 11, 17, 17, 0, tzinfo=UTC)


def test_utc_day_window_is_the_enforcement_window() -> None:
    window = utc_day_window(NOW)
    assert window.start == datetime(2026, 11, 16, 0, 0, tzinfo=UTC)
    assert window.end == datetime(2026, 11, 17, 0, 0, tzinfo=UTC)
