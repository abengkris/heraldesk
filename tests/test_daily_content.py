"""End-to-end tests for the M3 daily_content pipeline."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import (
    AgentRun,
    Approval,
    AuditLog,
    ContentIdea,
    ContentItem,
    PipelineRun,
)
from heraldesk.db.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    ContentIdeaRepository,
    ContentItemRepository,
    PipelineRunRepository,
    SocialAccountRepository,
    TelegramNotificationRepository,
)
from heraldesk.domain.profiles import ProfileDefinition, load_profile
from heraldesk.llm.fake import FakeLLM, json_fixture
from heraldesk.pipelines.daily_content import DailyContentError, run_daily_content
from heraldesk.telegram.fake import FakeTelegramAdapter
from heraldesk.telegram.notifier import NotificationService
from heraldesk.tools.web_search import FakeWebSearch
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)
IDEA_TITLE = "A smaller dashboard that loads faster"
IDEA_ANGLE = "compare the old and new load times on the same hardware"


def run_pipeline(
    db_session,
    setup: ProfileSetup,
    provider: FakeLLM,
    *,
    now: datetime = NOW,
    dry_run: bool = False,
    definition: ProfileDefinition | None = None,
    notifier: NotificationService | None = None,
):
    return run_daily_content(
        db_session,
        setup.layout,
        setup.scope,
        definition or setup.definition,
        provider=provider,
        search=FakeWebSearch(),
        model=provider.default_model,
        cap_usd=5.0,
        dry_run=dry_run,
        now=now,
        notifier=notifier,
    )


def with_policy(setup: ProfileSetup, text: str) -> ProfileDefinition:
    """Write a replacement policy.yaml and return the reloaded definition."""
    (setup.definition.path / "policy.yaml").write_text(text, encoding="utf-8")
    return load_profile(setup.definition.path)


def test_pipeline_end_to_end_records_everything(profile_setup: ProfileSetup, db_session) -> None:
    provider = FakeLLM()
    result = run_pipeline(db_session, profile_setup, provider)

    assert result.status == "succeeded"
    assert result.pipeline_run_id is not None
    assert result.dry_run is False
    assert len(result.items) == 1
    summary = result.items[0]
    assert summary.state == "pending_approval"
    assert summary.risk_level == "low"
    assert "dashboard" in summary.body

    items = ContentItemRepository(db_session).list(profile_setup.scope)
    assert len(items) == 1
    assert items[0].state == "pending_approval"
    assert items[0].media["source"] == "designer_placeholder_m3"
    assert items[0].idea_id is not None
    assert items[0].risk_level == "low"

    approvals = ApprovalRepository(db_session).list(profile_setup.scope)
    assert len(approvals) == 1
    assert approvals[0].content_item_id == items[0].id
    assert approvals[0].state == "pending"
    assert summary.approval_id == approvals[0].id

    ideas = ContentIdeaRepository(db_session).list(profile_setup.scope)
    assert len(ideas) == 1
    assert ideas[0].title == IDEA_TITLE

    pipeline_run = PipelineRunRepository(db_session).get(
        profile_setup.scope, result.pipeline_run_id
    )
    assert pipeline_run is not None
    assert pipeline_run.status == "succeeded"
    assert pipeline_run.pipeline == "daily_content"

    runs = AgentRunRepository(db_session).list(profile_setup.scope)
    assert {run.agent for run in runs} == {"strategist", "researcher", "writer", "compliance"}
    assert all(run.pipeline_run_id == result.pipeline_run_id for run in runs)

    actions = {
        entry.action for entry in AuditService(db_session).history(profile_setup.scope, limit=1000)
    }
    assert {
        "pipeline.started",
        "pipeline.succeeded",
        "content.state_changed",
        "approval.requested",
    } <= actions

    # The fake platform fallback must not create an account row.
    assert SocialAccountRepository(db_session).list(profile_setup.scope) == []


def test_dry_run_writes_nothing(profile_setup: ProfileSetup, db_session) -> None:
    db_session.commit()
    before = {
        model.__tablename__: db_session.scalar(select(func.count()).select_from(model))
        for model in (PipelineRun, ContentIdea, ContentItem, Approval, AgentRun, AuditLog)
    }

    result = run_pipeline(db_session, profile_setup, FakeLLM(), dry_run=True)

    after = {
        model.__tablename__: db_session.scalar(select(func.count()).select_from(model))
        for model in (PipelineRun, ContentIdea, ContentItem, Approval, AgentRun, AuditLog)
    }
    assert before == after
    assert result.dry_run is True
    assert result.pipeline_run_id is None
    assert result.items
    assert all(item.item_id is None for item in result.items)
    assert all(item.state == "pending_approval" for item in result.items)


def test_idea_similar_to_the_last_60_days_is_skipped(
    profile_setup: ProfileSetup, db_session
) -> None:
    ContentIdeaRepository(db_session).add(
        profile_setup.scope,
        source="test",
        title=IDEA_TITLE,
        angle=IDEA_ANGLE,
        evidence=[],
        created_at=NOW - timedelta(days=1),
    )
    db_session.commit()

    result = run_pipeline(db_session, profile_setup, FakeLLM())
    assert result.skipped_ideas == 1
    assert result.items == ()
    assert ContentItemRepository(db_session).list(profile_setup.scope) == []


def test_idea_older_than_60_days_is_allowed(profile_setup: ProfileSetup, db_session) -> None:
    ContentIdeaRepository(db_session).add(
        profile_setup.scope,
        source="test",
        title=IDEA_TITLE,
        angle=IDEA_ANGLE,
        evidence=[],
        created_at=NOW - timedelta(days=61),
    )
    db_session.commit()

    result = run_pipeline(db_session, profile_setup, FakeLLM())
    assert result.skipped_ideas == 0
    assert len(result.items) == 1


def test_compliance_unavailable_fails_closed(profile_setup: ProfileSetup, db_session) -> None:
    provider = FakeLLM(fail_agents=frozenset({"compliance"}))
    result = run_pipeline(db_session, profile_setup, provider)

    assert len(result.items) == 1
    assert result.items[0].state == "blocked"
    assert result.items[0].reason is not None
    assert "compliance unavailable" in result.items[0].reason
    assert result.items[0].state != "pending_approval"

    item = ContentItemRepository(db_session).list(profile_setup.scope)[0]
    assert item.state == "blocked"


def test_compliance_blocked_verdict_blocks_the_draft(
    profile_setup: ProfileSetup, db_session
) -> None:
    provider = FakeLLM(
        role_responses={
            "compliance": [
                json_fixture(
                    {
                        "risk_level": "blocked",
                        "reasons": ["Makes an unverifiable medical claim."],
                        "fixes": ["Remove the claim."],
                    }
                )
            ]
        }
    )
    result = run_pipeline(db_session, profile_setup, provider)
    assert result.items[0].state == "blocked"
    assert "unverifiable" in (result.items[0].reason or "")


def test_lint_failing_twice_goes_to_needs_review(profile_setup: ProfileSetup, db_session) -> None:
    bad_draft = json_fixture(
        {
            "body": "This is a game-changer \u2014 and truly revolutionary.",
            "variants": [],
            "hashtags": [],
        }
    )
    provider = FakeLLM(role_responses={"writer": [bad_draft, bad_draft]})
    result = run_pipeline(db_session, profile_setup, provider)

    assert result.items[0].state == "needs_review"
    assert result.items[0].reason is not None
    assert "lint failed 2 times" in result.items[0].reason
    item = ContentItemRepository(db_session).list(profile_setup.scope)[0]
    assert item.state == "needs_review"


def test_lint_can_recover_on_the_second_writer_attempt(
    profile_setup: ProfileSetup, db_session
) -> None:
    bad_draft = json_fixture(
        {"body": "This is a game-changer \u2014 truly.", "variants": [], "hashtags": []}
    )
    good_draft = json_fixture(
        {
            "body": "The dashboard loads in under a second on the same hardware.",
            "variants": [],
            "hashtags": [],
        }
    )
    provider = FakeLLM(role_responses={"writer": [bad_draft, good_draft]})
    result = run_pipeline(db_session, profile_setup, provider)
    assert result.items[0].state == "pending_approval"


def test_pipeline_output_never_contains_a_secret(profile_setup: ProfileSetup, db_session) -> None:
    secret = "sk-ABCDEFGHIJKLMNOP1234"
    provider = FakeLLM(
        role_responses={
            "writer": [
                json_fixture(
                    {
                        "body": f"Contact {secret} if the dashboard breaks.",
                        "variants": [],
                        "hashtags": [],
                    }
                )
            ]
        }
    )
    result = run_pipeline(db_session, profile_setup, provider)

    assert secret not in result.items[0].body
    item = ContentItemRepository(db_session).list(profile_setup.scope)[0]
    assert secret not in item.body

    runs = AgentRunRepository(db_session).list(profile_setup.scope)
    assert secret not in json.dumps([run.output for run in runs])
    audits = AuditService(db_session).history(profile_setup.scope, limit=1000)
    assert secret not in json.dumps([entry.payload for entry in audits])


def test_require_approval_creates_a_pending_approval(
    profile_setup: ProfileSetup, db_session
) -> None:
    definition = with_policy(profile_setup, "default_mode: require_approval\n")
    result = run_pipeline(db_session, profile_setup, FakeLLM(), definition=definition)

    assert result.items[0].state == "pending_approval"
    approvals = ApprovalRepository(db_session).list(profile_setup.scope)
    assert len(approvals) == 1
    assert approvals[0].state == "pending"

    requested = [
        entry
        for entry in AuditService(db_session).history(profile_setup.scope, limit=1000)
        if entry.action == "approval.requested"
    ]
    assert requested
    assert requested[-1].payload["policy_mode"] == "require_approval"


def test_auto_publish_low_risk_schedules_without_an_approval(
    profile_setup: ProfileSetup, db_session
) -> None:
    definition = with_policy(profile_setup, "default_mode: auto_publish\n")
    result = run_pipeline(db_session, profile_setup, FakeLLM(), definition=definition)

    assert result.items[0].state == "scheduled"
    item = ContentItemRepository(db_session).list(profile_setup.scope)[0]
    assert item.state == "scheduled"
    assert item.scheduled_at is not None
    assert item.publish_mode == "auto_publish"
    assert ApprovalRepository(db_session).list(profile_setup.scope) == []


def test_auto_mode_schedules_immediately(profile_setup: ProfileSetup, db_session) -> None:
    definition = with_policy(profile_setup, "default_mode: auto\n")
    result = run_pipeline(db_session, profile_setup, FakeLLM(), definition=definition)

    assert result.items[0].state == "scheduled"
    item = ContentItemRepository(db_session).list(profile_setup.scope)[0]
    assert item.state == "scheduled"
    assert item.publish_mode == "auto"
    assert item.publish_delay_minutes is None
    scheduled_at = item.scheduled_at
    assert scheduled_at is not None
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    assert scheduled_at == NOW
    assert ApprovalRepository(db_session).list(profile_setup.scope) == []


def test_auto_after_delay_records_the_cancellation_window(
    profile_setup: ProfileSetup, db_session
) -> None:
    definition = with_policy(profile_setup, "default_mode: auto_after_delay\n")
    result = run_pipeline(db_session, profile_setup, FakeLLM(), definition=definition)

    assert result.items[0].state == "scheduled"
    item = ContentItemRepository(db_session).list(profile_setup.scope)[0]
    assert item.state == "scheduled"
    assert item.publish_mode == "auto_after_delay"
    assert item.publish_delay_minutes == 30
    scheduled_at = item.scheduled_at
    assert scheduled_at is not None
    if scheduled_at.tzinfo is None:
        scheduled_at = scheduled_at.replace(tzinfo=UTC)
    assert scheduled_at == NOW + timedelta(minutes=30)
    assert ApprovalRepository(db_session).list(profile_setup.scope) == []


def test_auto_publish_high_risk_still_waits_for_a_human(
    profile_setup: ProfileSetup, db_session
) -> None:
    provider = FakeLLM(
        role_responses={
            "compliance": [
                json_fixture(
                    {
                        "risk_level": "high",
                        "reasons": ["Potentially misleading comparison."],
                        "fixes": ["Soften the claim."],
                    }
                )
            ]
        }
    )
    definition = with_policy(profile_setup, "default_mode: auto_publish\n")
    result = run_pipeline(
        db_session,
        profile_setup,
        provider,
        definition=definition,
    )

    assert result.items[0].state == "pending_approval"
    approvals = ApprovalRepository(db_session).list(profile_setup.scope)
    assert len(approvals) == 1
    assert approvals[0].state == "pending"


def test_strategist_failure_marks_the_pipeline_failed(
    profile_setup: ProfileSetup, db_session
) -> None:
    provider = FakeLLM(fail_agents=frozenset({"strategist"}))
    with pytest.raises(DailyContentError):
        run_pipeline(db_session, profile_setup, provider)

    run = db_session.scalars(select(PipelineRun)).one()
    assert run.status == "failed"
    assert run.error is not None


def test_pending_approval_notifies_once(profile_setup: ProfileSetup, db_session) -> None:
    telegram = FakeTelegramAdapter()
    notifier = NotificationService(
        db_session,
        profile_setup.layout,
        telegram,
        allowed_chat_ids=(123456789,),
        redactor=SecretRegistry.from_environment(),
    )

    result = run_pipeline(db_session, profile_setup, FakeLLM(), notifier=notifier)

    assert result.items[0].state == "pending_approval"
    assert telegram.send_calls == 1
    rows = TelegramNotificationRepository(db_session).list(profile_setup.scope)
    assert len(rows) == 1
    assert rows[0].event == "approval_requested"


def test_dry_run_does_not_send_telegram(profile_setup: ProfileSetup, db_session) -> None:
    telegram = FakeTelegramAdapter()
    notifier = NotificationService(
        db_session,
        profile_setup.layout,
        telegram,
        allowed_chat_ids=(123456789,),
        redactor=SecretRegistry.from_environment(),
    )

    result = run_pipeline(db_session, profile_setup, FakeLLM(), dry_run=True, notifier=notifier)

    assert result.dry_run is True
    assert telegram.send_calls == 0
    assert TelegramNotificationRepository(db_session).list(profile_setup.scope) == []
