"""Cost, audit and reports UI tests: windows, filters, pagination, rendering and safety."""

from __future__ import annotations

import html
import re
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.repositories import AgentRunRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import (
    ProfileDefinition,
    load_profile,
    register_definition,
    write_profile_skeleton,
)
from heraldesk.web.service import cost_breakdown
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
HX = {"HX-Request": "true"}


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
    response = client.post(
        "/login", data={"token": ADMIN_TOKEN, "csrf": match.group(1)}, follow_redirects=False
    )
    assert response.status_code == 303


def csrf_from(html: str) -> str:
    match = _CSRF.search(html)
    assert match is not None
    return match.group(1)


def add_profile(session: Session, setup: ProfileSetup, slug: str) -> ProfileScope:
    directory = write_profile_skeleton(setup.layout.profiles, slug)
    definition: ProfileDefinition = load_profile(directory)
    summary, _ = register_definition(session, definition)
    session.commit()
    return ProfileScope(summary.id)


def add_run(
    session: Session,
    scope: ProfileScope,
    *,
    started_at: datetime,
    tokens_in: int = 10,
    tokens_out: int = 5,
    cost_usd: float = 0.5,
) -> None:
    AgentRunRepository(session).add(
        scope,
        agent="writer",
        status="succeeded",
        model="fake",
        input={},
        started_at=started_at,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_usd=cost_usd,
        finished_at=started_at,
    )
    session.commit()


# --- Cost -----------------------------------------------------------------


def test_cost_window_selection_and_invalid_days(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    now = datetime.now(UTC)
    add_run(db_session, profile_setup.scope, started_at=now, cost_usd=1.25)
    add_run(db_session, profile_setup.scope, started_at=now - timedelta(days=20), cost_usd=9.0)

    with make_client(initialized) as client:
        login(client)
        seven = client.get("/cost?days=7")
        invalid = client.get("/cost?days=abc")
        thirty = client.get("/cost?days=30")

    assert seven.status_code == 200
    assert "7 days" in seven.text
    assert "$9.0000" not in seven.text
    assert invalid.status_code == 200
    assert "14 days" in invalid.text
    assert thirty.status_code == 200
    assert "30 days" in thirty.text
    assert "UTC" in seven.text


def test_cost_per_profile_filter_and_link_preservation(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    other = add_profile(db_session, profile_setup, "beta")
    now = datetime.now(UTC)
    add_run(db_session, profile_setup.scope, started_at=now, cost_usd=0.5)
    add_run(db_session, other, started_at=now, cost_usd=2.5)

    with make_client(initialized) as client:
        login(client)
        page = client.get("/cost?profile=acme")
    assert page.status_code == 200
    assert "acme" in page.text
    assert 'data-label="Profile">beta' not in page.text
    assert "$2.5000" not in page.text
    assert "&amp;profile=acme" in page.text


@pytest.mark.parametrize(
    ("spent", "expected"),
    [(1.0, "ok"), (4.5, "warn"), (6.0, "danger")],
)
def test_cost_cap_meter_states(
    profile_setup: ProfileSetup, db_session: Session, spent: float, expected: str
) -> None:
    now = datetime.now(UTC)
    add_run(db_session, profile_setup.scope, started_at=now, cost_usd=spent)
    summary = cost_breakdown(db_session, days=7, now=now, cap_usd=5.0)
    assert summary["cap"] is not None
    assert summary["cap"]["state"] == expected
    assert summary["cap"]["spent"] == pytest.approx(spent)


def test_cost_cap_meter_not_configured(profile_setup: ProfileSetup, db_session: Session) -> None:
    summary = cost_breakdown(db_session, days=7, cap_usd=0.0)
    assert summary["cap"] is None


def test_cost_empty_window(initialized: InitResult, profile_setup: ProfileSetup) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/cost")
    assert page.status_code == 200
    assert "No spend in this window." in page.text


def test_cost_rows_are_grouped_per_day_and_newest_first(
    profile_setup: ProfileSetup, db_session: Session
) -> None:
    now = datetime(2026, 11, 21, 12, tzinfo=UTC)
    add_run(db_session, profile_setup.scope, started_at=now - timedelta(days=1), cost_usd=1.0)
    add_run(db_session, profile_setup.scope, started_at=now, cost_usd=2.0)
    summary = cost_breakdown(db_session, days=7, now=now, cap_usd=5.0)
    assert [row["day"] for row in summary["rows"]] == ["2026-11-21", "2026-11-20"]
    assert summary["total"]["cost_usd"] == pytest.approx(3.0)
    assert summary["max_cost"] == pytest.approx(2.0)


# --- Audit ----------------------------------------------------------------


def test_audit_filters_and_invalid_values(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    audit = AuditService(db_session)
    audit.record(
        profile_setup.scope,
        actor="user",
        action="content.approved",
        entity="content_item",
        entity_id="7",
    )
    audit.record(None, actor="system", action="serve.started", entity="server")
    db_session.commit()

    with make_client(initialized) as client:
        login(client)
        by_actor = client.get("/audit?actor=system")
        by_action = client.get("/audit?action=content.approved")
        by_prefix = client.get("/audit?action=con*")
        by_entity = client.get("/audit?entity=server")
        combined = client.get("/audit?actor=user&entity=content_item&action=content.approved")
        invalid = client.get("/audit?date_from=not-a-date&offset=abc")

    assert 'data-label="Action"><code>serve.started' in by_actor.text
    assert 'data-label="Action"><code>content.approved' not in by_actor.text
    assert 'data-label="Action"><code>content.approved' in by_action.text
    assert 'data-label="Action"><code>serve.started' not in by_action.text
    assert 'data-label="Action"><code>content.approved' in by_prefix.text
    assert 'data-label="Action"><code>serve.started' in by_entity.text
    assert "content.approved" in combined.text
    assert invalid.status_code == 200
    assert "date was ignored" in invalid.text
    assert "offset was ignored" in invalid.text


def test_audit_date_range_filters(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    audit = AuditService(db_session)
    audit.record(None, actor="system", action="serve.started", entity="server")
    db_session.commit()
    today = datetime.now(UTC).date()
    tomorrow = today + timedelta(days=1)
    yesterday = today - timedelta(days=1)
    with make_client(initialized) as client:
        login(client)
        today_page = client.get(f"/audit?date_from={today}&date_to={today}")
        future = client.get(f"/audit?date_from={tomorrow}")
        past = client.get(f"/audit?date_to={yesterday}")
    assert "serve.started" in today_page.text
    assert "No audit entries match." in future.text
    assert "No audit entries match." in past.text


def test_audit_pagination_preserves_filters(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    audit = AuditService(db_session)
    for index in range(60):
        audit.record(
            profile_setup.scope,
            actor="user",
            action="test.bulk",
            entity="bulk",
            entity_id=str(index),
        )
    db_session.commit()

    with make_client(initialized) as client:
        login(client)
        first = client.get("/audit?actor=user&action=test.bulk")
        match = re.search(r'href="([^"]+)" rel="next"', first.text)
        assert match is not None
        next_url = html.unescape(match.group(1))
        second = client.get(next_url)

    assert "Rows 1-50" in first.text
    assert "Rows 51-60" in second.text
    assert "actor=user" in next_url
    assert "action=test.bulk" in next_url
    assert 'rel="prev"' in second.text


def test_audit_payload_is_redacted_and_truncated(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    audit = AuditService(db_session)
    audit.record(
        profile_setup.scope,
        actor="user",
        action="test.payload",
        entity="example",
        payload={"note": "sk-ABCDEFGHIJKLMNOP1234 " + "x" * 5000},
    )
    db_session.commit()
    with make_client(initialized) as client:
        login(client)
        page = client.get("/audit?action=test.payload")
    assert page.status_code == 200
    assert "sk-ABCDEFGHIJKLMNOP1234" not in page.text
    assert "[REDACTED]" in page.text
    assert "Truncated." in page.text
    assert "<details" in page.text


def test_audit_has_no_mutating_routes(initialized: InitResult) -> None:
    app = create_app(
        AppOptions(
            layout=initialized.layout,
            config=config_for(initialized),
            enable_telegram=False,
            admin_token=ADMIN_TOKEN,
            adapter_factory=lambda platform: None,
            scheduler_interval_seconds=3600.0,
        )
    )
    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path.startswith("/audit"):
            assert methods <= {"GET", "HEAD"}


# --- Reports --------------------------------------------------------------


def test_reports_empty_state_shows_generate_forms(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/reports")
    assert page.status_code == 200
    assert "No reports yet." in page.text
    assert 'action="/reports/weekly"' in page.text
    assert 'action="/reports/monthly"' in page.text
    assert "hx-disabled-elt" in page.text
    assert "http://" not in page.text
    assert "https://" not in page.text


def test_reports_generate_monthly_and_show_in_list(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/reports")
        response = client.post(
            "/reports/monthly",
            data={"csrf": csrf_from(page.text), "month": "2026-11"},
            follow_redirects=False,
        )
        listing = client.get("/reports")
    assert response.status_code == 303
    assert "monthly-2026-11.md" in listing.text
    assert "2026-11" in listing.text


def test_reports_htmx_generation_returns_list_and_toast(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/reports")
        response = client.post(
            "/reports/weekly",
            data={"csrf": csrf_from(page.text), "week": "2026-W48"},
            headers=HX,
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "weekly-2026-W48.md" in response.text
    assert 'hx-swap-oob="true"' in response.text
    assert "Report generated." in response.text


def test_report_view_and_download_headers(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/reports")
        client.post(
            "/reports/weekly",
            data={"csrf": csrf_from(page.text), "week": "2026-W48"},
            follow_redirects=False,
        )
        view = client.get("/reports/view/weekly-2026-W48.md")
        download = client.get("/reports/download/weekly-2026-W48.md")
    assert view.status_code == 200
    assert "report-body" in view.text
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/markdown")
    assert "attachment" in download.headers["content-disposition"]
    assert "weekly-2026-W48.md" in download.headers["content-disposition"]


@pytest.mark.parametrize(
    "name",
    [
        "../secret.md",
        "..%2F..%2Fsecret.md",
        "weekly-2026-W48.txt",
        "weekly-2026-W4.md",
        "weekly-2026-W48.md/../../secret.md",
    ],
)
def test_report_view_and_download_reject_invalid_names(initialized: InitResult, name: str) -> None:
    with make_client(initialized) as client:
        login(client)
        view = client.get(f"/reports/view/{name}")
        download = client.get(f"/reports/download/{name}")
    assert view.status_code in {400, 404}
    assert download.status_code in {400, 404}


def test_report_symlink_escaping_the_directory_is_rejected(initialized: InitResult) -> None:
    initialized.layout.reports.mkdir(parents=True, exist_ok=True)
    target = initialized.layout.home / "secret.md"
    target.write_text("secret", encoding="utf-8")
    link = initialized.layout.reports / "weekly-2026-W48.md"
    link.symlink_to(target)
    try:
        with make_client(initialized) as client:
            login(client)
            view = client.get("/reports/view/weekly-2026-W48.md")
            download = client.get("/reports/download/weekly-2026-W48.md")
    finally:
        link.unlink(missing_ok=True)
        target.unlink(missing_ok=True)
    assert view.status_code == 404
    assert download.status_code == 404


def test_reports_generate_requires_csrf(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        response = client.post("/reports/weekly", data={"week": "2026-W48"}, follow_redirects=False)
    assert response.status_code == 403


def test_expired_session_htmx_audit_redirects_to_login(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        response = client.get("/audit", headers=HX, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers.get("hx-redirect") == "/login"
