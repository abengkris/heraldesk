"""Draft Room backend: configuration, isolation, turns, limits and redaction."""

from __future__ import annotations

import ast
import inspect
import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk import studio as studio_module
from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import load_config
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import (
    AgentRunRepository,
    ContentItemRepository,
    StudioMessageRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import register_definition
from heraldesk.llm.fake import FakeLLM
from heraldesk.studio import (
    StudioCostError,
    StudioDisabledError,
    StudioLimitError,
    StudioNotFoundError,
    StudioService,
)
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
SECRET = "sk-ABCDEFGHIJKLMNOP1234"


def write_studio_config(initialized: InitResult, *, enabled: bool, fake_llm: bool = False) -> None:
    lines = [
        "version: 1",
        "studio:",
        f"  enabled: {str(enabled).lower()}",
        "  max_messages: 40",
        "  max_cost_usd: 0.25",
    ]
    if fake_llm:
        lines.extend(["llm:", "  provider: fake", "  default_model: fake/editor"])
    initialized.layout.config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def service_for(
    setup: ProfileSetup,
    session: Session,
    *,
    provider: FakeLLM | None = None,
    max_messages: int = 40,
    thread_cap_usd: float = 0.25,
    enabled: bool = True,
) -> StudioService:
    return StudioService(
        session,
        setup.layout,
        provider=provider,
        model="fake/editor",
        daily_cap_usd=5.0,
        thread_cap_usd=thread_cap_usd,
        max_messages=max_messages,
        enabled=enabled,
    )


def other_scope(setup: ProfileSetup, session: Session) -> ProfileScope:
    from heraldesk.domain.profiles import load_profile, write_profile_skeleton

    definition = load_profile(write_profile_skeleton(setup.layout.profiles, "other"))
    summary, _ = register_definition(session, definition)
    session.commit()
    return ProfileScope(summary.id)


def login(client: TestClient) -> None:
    page = client.get("/login")
    assert page.status_code == 200
    match = _CSRF.search(page.text)
    assert match is not None
    response = client.post(
        "/login",
        data={"token": ADMIN_TOKEN, "csrf": match.group(1)},
        follow_redirects=False,
    )
    assert response.status_code == 303


def client_for(initialized: InitResult) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=load_config(initialized.layout.home),
                enable_telegram=False,
                admin_token=ADMIN_TOKEN,
                adapter_factory=lambda platform: None,
                scheduler_interval_seconds=3600.0,
            )
        )
    )


def test_studio_is_disabled_by_default(initialized: InitResult) -> None:
    assert load_config(initialized.layout.home).studio.enabled is False

    write_studio_config(initialized, enabled=True)
    assert load_config(initialized.layout.home).studio.enabled is True


def test_studio_route_and_nav_are_absent_when_disabled(initialized: InitResult) -> None:
    with client_for(initialized) as client:
        assert client.get("/studio").status_code == 404
        login(client)
        dashboard = client.get("/")
        assert 'href="/studio"' not in dashboard.text


def test_studio_route_and_nav_appear_when_enabled(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    write_studio_config(initialized, enabled=True)
    with client_for(initialized) as client:
        login(client)
        assert 'href="/studio"' in client.get("/").text
        page = client.get("/studio")
        assert page.status_code == 200
        assert "Draft Room" in page.text


def test_a_thread_is_isolated_to_its_profile(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    service = service_for(profile_setup, db_session)
    thread = service.create_thread(profile_setup.scope, title="Planning")
    other = other_scope(profile_setup, db_session)

    assert service.get_thread(profile_setup.scope, thread.id).id == thread.id
    with pytest.raises(StudioNotFoundError):
        service.get_thread(other, thread.id)
    with pytest.raises(StudioNotFoundError):
        service.list_messages(other, thread.id)


def test_a_turn_records_messages_and_one_agent_run(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    fake = FakeLLM()
    service = service_for(profile_setup, db_session, provider=fake)
    thread = service.create_thread(profile_setup.scope)

    result = service.send_message(profile_setup.scope, thread.id, "Plan the week.")
    db_session.commit()

    messages = StudioMessageRepository(db_session).list_messages(profile_setup.scope, thread.id)
    assert [message.role for message in messages] == ["user", "editor"]
    assert messages[0].content == "Plan the week."
    assert messages[1].content
    assert messages[1].agent_run_id == result.run.run_id
    assert messages[1].cost_usd > 0
    assert thread.title == "Plan the week."
    assert len(AgentRunRepository(db_session).list(profile_setup.scope)) == 1
    assert fake.calls and fake.calls[0].agent == "editor"


def test_a_turn_does_not_create_content_or_change_state(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    service = service_for(profile_setup, db_session, provider=FakeLLM())
    thread = service.create_thread(profile_setup.scope)

    service.send_message(profile_setup.scope, thread.id, "Draft an idea.")
    db_session.commit()

    assert ContentItemRepository(db_session).list(profile_setup.scope) == []


def test_disabled_service_refuses_writes(profile_setup: ProfileSetup, db_session: Session) -> None:
    service = service_for(profile_setup, db_session, enabled=False)

    with pytest.raises(StudioDisabledError):
        service.create_thread(profile_setup.scope)


def test_a_turn_is_refused_at_the_thread_cost_cap(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    fake = FakeLLM()
    service = service_for(profile_setup, db_session, provider=fake, thread_cap_usd=0.001)
    thread = service.create_thread(profile_setup.scope)
    StudioMessageRepository(db_session).add(
        profile_setup.scope,
        thread.id,
        role="editor",
        content="previous",
        cost_usd=0.002,
    )
    db_session.flush()

    with pytest.raises(StudioCostError):
        service.send_message(profile_setup.scope, thread.id, "next")
    assert fake.calls == []


def test_a_turn_is_refused_at_the_message_limit(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    fake = FakeLLM()
    service = service_for(profile_setup, db_session, provider=fake, max_messages=2)
    thread = service.create_thread(profile_setup.scope)
    repository = StudioMessageRepository(db_session)
    repository.add(profile_setup.scope, thread.id, role="user", content="one")
    repository.add(profile_setup.scope, thread.id, role="editor", content="two")
    db_session.flush()

    with pytest.raises(StudioLimitError):
        service.send_message(profile_setup.scope, thread.id, "three")
    assert fake.calls == []


def test_history_is_bounded_to_twelve_messages(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    fake = FakeLLM()
    service = service_for(profile_setup, db_session, provider=fake)
    thread = service.create_thread(profile_setup.scope)
    repository = StudioMessageRepository(db_session)
    for index in range(15):
        repository.add(
            profile_setup.scope,
            thread.id,
            role="user",
            content=f"turn-{index}",
        )
    db_session.flush()

    service.send_message(profile_setup.scope, thread.id, "now")

    prompt = fake.calls[0].messages[-1].content
    assert "turn-0" not in prompt
    assert "turn-2" not in prompt
    assert "turn-3" in prompt
    assert "turn-14" in prompt
    assert "omitted" in prompt


def test_secrets_are_redacted_before_storage_and_audit(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    service = service_for(profile_setup, db_session, provider=FakeLLM())
    thread = service.create_thread(profile_setup.scope)

    service.send_message(profile_setup.scope, thread.id, f"token {SECRET}")
    db_session.commit()

    stored = StudioMessageRepository(db_session).list_messages(profile_setup.scope, thread.id)[0]
    assert SECRET not in stored.content
    assert "[REDACTED]" in stored.content

    entries = AuditService(db_session).history(profile_setup.scope, limit=20)
    payloads = " ".join(str(entry.payload) for entry in entries)
    assert SECRET not in payloads
    assert "message_sha256" in payloads


def test_studio_service_has_no_publisher_or_adapter_imports() -> None:
    tree = ast.parse(inspect.getsource(studio_module))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    forbidden = ("heraldesk.publisher", "heraldesk.scheduler", "heraldesk.platforms")
    assert not [name for name in imported if name.startswith(forbidden)]


def test_studio_has_no_publish_or_approve_route(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    write_studio_config(initialized, enabled=True)
    with client_for(initialized) as client:
        login(client)
        assert client.post("/studio/1/publish").status_code == 404
        assert client.post("/studio/1/approve").status_code == 404
        assert client.post("/studio/1/reject").status_code == 404


def create_thread_via_ui(client: TestClient, profile: str = "acme") -> int:
    page = client.get(f"/studio?profile={profile}")
    match = _CSRF.search(page.text)
    assert match is not None
    response = client.post(
        "/studio/threads",
        data={"csrf": match.group(1), "profile": profile, "title": "Weekly plan"},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    return int(location.rsplit("/", 1)[-1])


def test_create_thread_from_the_ui(initialized: InitResult, profile_setup: ProfileSetup) -> None:
    write_studio_config(initialized, enabled=True)
    with client_for(initialized) as client:
        login(client)
        thread_id = create_thread_via_ui(client)
        page = client.get(f"/studio/{thread_id}")
        assert page.status_code == 200
        assert "Weekly plan" in page.text
        assert "No messages yet" in page.text


def test_send_message_from_the_ui(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    write_studio_config(initialized, enabled=True, fake_llm=True)
    with client_for(initialized) as client:
        login(client)
        thread_id = create_thread_via_ui(client)
        page = client.get(f"/studio/{thread_id}")
        match = _CSRF.search(page.text)
        assert match is not None
        response = client.post(
            f"/studio/{thread_id}/messages",
            data={"csrf": match.group(1), "text": "Plan the week.", "role": "editor"},
            follow_redirects=False,
        )
        assert response.status_code == 303, response.text
        after = client.get(f"/studio/{thread_id}")
        assert "Plan the week." in after.text
        assert "Here is a concrete plan" in after.text
        assert "Draft Room" in after.text

    stored = StudioMessageRepository(db_session).list_messages(profile_setup.scope, thread_id)
    assert [message.role for message in stored] == ["user", "editor"]


def test_message_error_keeps_the_draft_text(
    initialized: InitResult,
    profile_setup: ProfileSetup,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from heraldesk.llm.errors import LLMError

    write_studio_config(initialized, enabled=True, fake_llm=True)
    with client_for(initialized) as client:
        login(client)
        thread_id = create_thread_via_ui(client)
        page = client.get(f"/studio/{thread_id}")
        match = _CSRF.search(page.text)
        assert match is not None
        monkeypatch.setattr(
            "heraldesk.web.ui.build_llm",
            lambda config, layout: FakeLLM(fail_with=LLMError("provider down")),
        )
        response = client.post(
            f"/studio/{thread_id}/messages",
            data={"csrf": match.group(1), "text": "keep this text", "role": "editor"},
        )
        assert response.status_code == 400
        assert "provider down" in response.text
        assert "keep this text" in response.text


def test_message_requires_csrf(initialized: InitResult, profile_setup: ProfileSetup) -> None:
    write_studio_config(initialized, enabled=True, fake_llm=True)
    with client_for(initialized) as client:
        login(client)
        thread_id = create_thread_via_ui(client)
        response = client.post(
            f"/studio/{thread_id}/messages",
            data={"text": "no token"},
        )
        assert response.status_code == 403


def test_archive_thread_from_the_ui(initialized: InitResult, profile_setup: ProfileSetup) -> None:
    write_studio_config(initialized, enabled=True, fake_llm=True)
    with client_for(initialized) as client:
        login(client)
        thread_id = create_thread_via_ui(client)
        page = client.get(f"/studio/{thread_id}")
        match = _CSRF.search(page.text)
        assert match is not None
        response = client.post(
            f"/studio/{thread_id}/archive",
            data={"csrf": match.group(1)},
            follow_redirects=False,
        )
        assert response.status_code == 303
        archived = client.get(f"/studio/{thread_id}")
        assert "archived" in archived.text.lower()
        assert "This thread is archived" in archived.text
