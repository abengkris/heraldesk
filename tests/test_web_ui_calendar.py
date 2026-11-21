"""Calendar weekly grid tests: boundaries, timezones, quiet hours, links."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.content import ContentState
from heraldesk.domain.profiles import (
    ProfileDefinition,
    load_profile,
    register_definition,
    write_profile_skeleton,
)
from heraldesk.web.service import calendar_week
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')


def write_config(initialized: InitResult) -> None:
    initialized.layout.config_path.write_text("version: 1\n", encoding="utf-8")


def config_for(initialized: InitResult) -> Config:
    write_config(initialized)
    return load_config(initialized.layout.home)


def make_client(initialized: InitResult) -> TestClient:
    return TestClient(
        create_app(
            AppOptions(
                layout=initialized.layout,
                config=config_for(initialized),
                enable_telegram=False,
                admin_token=ADMIN_TOKEN,
                adapter_factory=lambda platform: None,
                scheduler_interval_seconds=3600.0,
            )
        )
    )


def login(client: TestClient) -> None:
    page = client.get("/login")
    match = _CSRF.search(page.text)
    assert match is not None
    client.post(
        "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
    )


def add_profile(setup: ProfileSetup, session: Session, slug: str, timezone: str) -> ProfileScope:
    directory = write_profile_skeleton(
        setup.layout.profiles, slug, name=slug.title(), timezone=timezone
    )
    definition: ProfileDefinition = load_profile(directory)
    summary, _ = register_definition(session, definition)
    session.commit()
    return ProfileScope(summary.id)


def schedule(
    session: Session,
    scope: ProfileScope,
    *,
    when: datetime,
    body: str = "post",
    state: str = ContentState.SCHEDULED.value,
) -> int:
    item = ContentItemRepository(session).add(scope, platform="fake", body=body, state=state)
    ContentItemRepository(session).set_scheduled_at(scope, item.id, scheduled_at=when)
    session.commit()
    return item.id


MONDAY = date(2026, 11, 16)


def test_week_boundaries_are_monday_start(profile_setup: ProfileSetup, db_session: Session) -> None:
    schedule(db_session, profile_setup.scope, when=datetime(2026, 11, 16, 0, 5, tzinfo=UTC))
    schedule(db_session, profile_setup.scope, when=datetime(2026, 11, 22, 23, 59, tzinfo=UTC))
    schedule(db_session, profile_setup.scope, when=datetime(2026, 11, 15, 23, 59, tzinfo=UTC))

    week = calendar_week(db_session, SecretRegistry(), week_start=MONDAY, timezone="UTC")

    all_items = [item for items in week["items_by_cell"].values() for item in items]
    assert len(all_items) == 2
    assert week["days"][0]["name"] == "Mon"


def test_timezone_conversion_for_non_utc_profile(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    jakarta = add_profile(profile_setup, db_session, "jakarta", "Asia/Jakarta")
    schedule(db_session, jakarta, when=datetime(2026, 11, 16, 0, 30, tzinfo=UTC))

    week = calendar_week(db_session, SecretRegistry(), week_start=MONDAY, timezone="Asia/Jakarta")

    items = [item for items in week["items_by_cell"].values() for item in items]
    assert items[0]["time"] == "07:30"
    assert items[0]["day"] == 0
    assert items[0]["hour"] == 7


def test_dst_transition_week_uses_correct_offsets(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    ny = add_profile(profile_setup, db_session, "ny", "America/New_York")
    schedule(db_session, ny, when=datetime(2026, 10, 31, 6, 0, tzinfo=UTC))  # EDT -4
    schedule(db_session, ny, when=datetime(2026, 11, 1, 6, 0, tzinfo=UTC))  # EST -5

    week = calendar_week(
        db_session, SecretRegistry(), week_start=date(2026, 10, 26), timezone="America/New_York"
    )
    items = sorted(
        (item for items in week["items_by_cell"].values() for item in items),
        key=lambda item: item["time"],
    )
    assert [item["time"] for item in items] == ["01:00", "02:00"]


def test_quiet_hours_shade_cross_midnight_window(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    quiet = SimpleNamespace(start="21:00", end="07:00")
    week = calendar_week(
        db_session, SecretRegistry(), week_start=MONDAY, timezone="UTC", quiet_hours=quiet
    )
    assert "0-21" in week["quiet_cells"]
    assert "0-0" in week["quiet_cells"]
    assert "0-12" not in week["quiet_cells"]


def test_no_quiet_shading_for_all_profiles(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    schedule(db_session, profile_setup.scope, when=datetime(2026, 11, 16, 12, 0, tzinfo=UTC))
    week = calendar_week(db_session, SecretRegistry(), week_start=MONDAY, timezone=None)
    assert week["quiet_cells"] == []


def test_calendar_empty_week_and_profile_links(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/calendar")
        assert "Nothing this week." in page.text

        scoped = client.get("/calendar?profile=acme")
        assert "&profile=acme" in scoped.text
        assert "/calendar?profile=acme" in scoped.text  # Today link
        assert "week=" in scoped.text  # prev/next links
