"""Secrets never reach prompts, run traces or audit payloads."""

from __future__ import annotations

import json
import os

import pytest
from sqlalchemy import select

from heraldesk.agents.base import AgentContext
from heraldesk.agents.runner import AgentRunner
from heraldesk.agents.schemas import WriterInput
from heraldesk.agents.writer import Writer
from heraldesk.config import load_config
from heraldesk.db.models import AgentRun, AuditLog
from heraldesk.llm.factory import build_llm
from heraldesk.llm.fake import FakeLLM, json_fixture
from heraldesk.platforms.base import constraints_for

pytestmark = pytest.mark.core

SECRET = "sk-test-very-secret-key-1234567890"


def test_api_key_is_not_in_the_prompt(profile_setup, db_session, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
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

    runner.run(Writer(), context, WriterInput(idea="Tell the story", platform="fake"))

    assert provider.calls
    for message in provider.calls[0].messages:
        assert SECRET not in message.content


def test_dotenv_keys_are_exported_but_not_put_in_prompts(
    profile_setup, db_session, monkeypatch
) -> None:
    monkeypatch.setattr(os, "environ", dict(os.environ))
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    (profile_setup.layout.home / ".env").write_text(
        f"OPENROUTER_API_KEY={SECRET}\n", encoding="utf-8"
    )
    config = load_config(profile_setup.layout.home)
    provider = build_llm(config, profile_setup.layout)
    assert isinstance(provider, FakeLLM)
    assert os.environ.get("OPENROUTER_API_KEY") == SECRET

    runner = AgentRunner(
        db_session,
        provider,
        layout=profile_setup.layout,
        model="fake/fake",
        cap_usd=5.0,
    )
    context = AgentContext(
        scope=profile_setup.scope,
        profile=profile_setup.definition,
        platform="fake",
        constraints=constraints_for("fake"),
    )
    runner.run(Writer(), context, WriterInput(idea="Key safety", platform="fake"))

    for message in provider.calls[0].messages:
        assert SECRET not in message.content


def test_leaked_secret_is_redacted_before_storage(profile_setup, db_session, monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", SECRET)
    provider = FakeLLM(
        responses=[
            json_fixture(
                {
                    "body": f"Oops, I copied the key {SECRET} into the draft.",
                    "variants": [f"Variant with {SECRET}"],
                    "hashtags": [],
                }
            )
        ]
    )
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
    result = runner.run(Writer(), context, WriterInput(idea="Leak", platform="fake"))

    stored = db_session.scalar(select(AgentRun).where(AgentRun.id == result.run_id))
    assert stored is not None
    serialized = json.dumps({"input": stored.input, "output": stored.output, "error": stored.error})
    assert SECRET not in serialized
    assert "[REDACTED]" in serialized

    for entry in db_session.scalars(select(AuditLog)).all():
        assert SECRET not in json.dumps(entry.payload)


def test_secret_shaped_values_from_unknown_sources_are_redacted(profile_setup, db_session) -> None:
    shaped = "sk-abcdefghijklmnopqrstuvwx"
    provider = FakeLLM(
        responses=[
            json_fixture({"body": f"The model echoed {shaped}.", "variants": [], "hashtags": []})
        ]
    )
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
    result = runner.run(Writer(), context, WriterInput(idea="Shape", platform="fake"))

    stored = db_session.scalar(select(AgentRun).where(AgentRun.id == result.run_id))
    assert stored is not None
    assert stored.output is not None
    assert shaped not in json.dumps(stored.output)
