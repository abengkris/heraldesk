"""Profiles list and editor tests: files, validation, stale edits, autonomy confirmation."""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from heraldesk.api.app import AppOptions, create_app
from heraldesk.config import Config, load_config
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.models import AuditLog
from heraldesk.domain.profiles import load_profile
from heraldesk.web.profile_editor import apply_policy_helper
from heraldesk.web.service import loosens_autonomy
from tests.conftest import ProfileSetup

pytestmark = pytest.mark.core

ADMIN_TOKEN = "test-admin-token"
_CSRF = re.compile(r'name="csrf" value="([^"]+)"')
_HASH = re.compile(r'name="base_hash" value="([^"]+)"')
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


def base_hash_from(html: str) -> str:
    match = _HASH.search(html)
    assert match is not None
    return match.group(1)


def read(initialized: InitResult, name: str) -> str:
    return (initialized.layout.profiles / "acme" / name).read_text(encoding="utf-8")


def edited_audits(session: Session) -> list[AuditLog]:
    return [
        row for row in session.scalars(select(AuditLog)).all() if row.action == "profile.edited"
    ]


def test_profiles_list_shows_type_autonomy_and_counts(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        response = client.get("/profiles")
    assert response.status_code == 200
    assert "acme" in response.text
    assert "other" in response.text
    assert "draft_only" in response.text
    assert "Autonomy" in response.text
    assert ADMIN_TOKEN not in response.text


def test_profiles_list_is_empty_without_profiles(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        login(client)
        response = client.get("/profiles")
    assert response.status_code == 200
    assert "No profiles yet" in response.text


def test_profiles_list_shows_unknown_for_unreadable_policy(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    (initialized.layout.profiles / "acme" / "policy.yaml").write_text(
        "default_mode: [unclosed\n", encoding="utf-8"
    )
    with make_client(initialized) as client:
        login(client)
        response = client.get("/profiles")
    assert response.status_code == 200
    assert "unknown" in response.text


@pytest.mark.parametrize("name", ["soul.md", "brief.md", "policy.yaml", "banned.yaml"])
def test_editor_renders_each_file(
    initialized: InitResult, profile_setup: ProfileSetup, name: str
) -> None:
    with make_client(initialized) as client:
        login(client)
        response = client.get(f"/profiles/acme/edit?file={name}")
    assert response.status_code == 200
    assert f'name="file" value="{name}"' in response.text
    assert 'class="mono"' in response.text or "profile-text" in response.text
    assert f'href="/profiles/acme/edit?file={name}"' in response.text
    assert 'aria-current="page"' in response.text
    assert "http://" not in response.text
    assert "https://" not in response.text


def test_validate_is_read_only_and_reports_valid(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    before = len(edited_audits(db_session))
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=policy.yaml")
        response = client.post(
            "/profiles/acme/validate",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": read(initialized, "policy.yaml"),
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "Valid. Nothing was saved." in response.text
    assert read(initialized, "policy.yaml").startswith("# Autonomy policy")
    assert len(edited_audits(db_session)) == before


def test_validate_reports_a_yaml_line_number_and_writes_nothing(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    before = len(edited_audits(db_session))
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=policy.yaml")
        response = client.post(
            "/profiles/acme/validate",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": "default_mode: [unclosed\n",
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "line 2" in response.text
    assert 'aria-live="polite"' in response.text
    assert len(edited_audits(db_session)) == before


def test_htmx_validate_returns_a_fragment(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        response = client.post(
            "/profiles/acme/validate",
            data={"csrf": csrf_from(page.text), "file": "brief.md", "text": "A valid brief."},
            headers=HX,
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "<html" not in response.text
    assert "Valid. Nothing was saved." in response.text


def test_review_shows_a_line_diff_and_writes_nothing(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    before = len(edited_audits(db_session))
    original = read(initialized, "brief.md")
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        response = client.post(
            "/profiles/acme/edit/preview",
            data={
                "csrf": csrf_from(page.text),
                "file": "brief.md",
                "text": "# Brand brief: Acme\n\nA changed first line.\n",
                "base_hash": base_hash_from(page.text),
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "<ins>" in response.text
    assert "<del>" in response.text
    assert "nothing is written until you save" in response.text
    assert read(initialized, "brief.md") == original
    assert len(edited_audits(db_session)) == before


def test_save_writes_reloads_and_audits_one_file(
    initialized: InitResult, profile_setup: ProfileSetup, db_session: Session
) -> None:
    updated = "# Brand brief: Acme\n\nA saved second line.\n"
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        response = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "brief.md",
                "text": updated,
                "base_hash": base_hash_from(page.text),
            },
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert read(initialized, "brief.md") == updated
    assert load_profile(initialized.layout.profiles / "acme").brief.startswith("# Brand brief")
    audits = edited_audits(db_session)
    assert audits[-1].payload["files"] == ["brief.md"]
    assert audits[-1].payload["source"] == "web"
    assert "A saved second line" not in str(audits[-1].payload)


def test_invalid_file_is_never_written(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    original = read(initialized, "policy.yaml")
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=policy.yaml")
        response = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": "default_mode: not-a-mode\n",
                "base_hash": base_hash_from(page.text),
            },
            follow_redirects=False,
        )
    assert response.status_code == 400
    assert read(initialized, "policy.yaml") == original


def test_stale_hash_is_rejected_and_does_not_overwrite(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        stale_hash = base_hash_from(page.text)
        path = initialized.layout.profiles / "acme" / "brief.md"
        concurrent = path.read_text(encoding="utf-8") + "\nConcurrent change.\n"
        path.write_text(concurrent, encoding="utf-8")
        response = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "brief.md",
                "text": "My overwriting text.\n",
                "base_hash": stale_hash,
            },
            follow_redirects=False,
        )
    assert response.status_code == 409
    assert "changed on disk" in response.text
    assert "Concurrent change." in response.text
    assert path.read_text(encoding="utf-8") == concurrent


def test_forged_save_without_loosening_confirmation_is_rejected(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    original = read(initialized, "policy.yaml")
    loosened = original.replace("default_mode: draft_only", "default_mode: require_approval")
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=policy.yaml")
        forged = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": loosened,
                "base_hash": base_hash_from(page.text),
            },
            follow_redirects=False,
        )
        assert forged.status_code == 400
        assert "loosens autonomy" in forged.text
        assert read(initialized, "policy.yaml") == original

        confirmed = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": loosened,
                "base_hash": base_hash_from(page.text),
                "loosen_confirm": "yes",
            },
            follow_redirects=False,
        )
    assert confirmed.status_code == 303
    assert read(initialized, "policy.yaml") == loosened


def test_non_loosening_save_needs_no_confirmation(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        response = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "brief.md",
                "text": "# Brand brief: Acme\n\nTighter wording.\n",
                "base_hash": base_hash_from(page.text),
            },
            follow_redirects=False,
        )
    assert response.status_code == 303


def test_path_traversal_is_rejected(initialized: InitResult, profile_setup: ProfileSetup) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        bad_file = client.get("/profiles/acme/edit?file=../policy.yaml")
        assert bad_file.status_code == 400
        bad_slug = client.get("/profiles/..%2F..%2Fetc/edit?file=soul.md")
        assert bad_slug.status_code in {400, 404}
        bad_save = client.post(
            "/profiles/acme/edit",
            data={
                "csrf": csrf_from(page.text),
                "file": "../policy.yaml",
                "text": "default_mode: draft_only\n",
                "base_hash": base_hash_from(page.text),
            },
            follow_redirects=False,
        )
        assert bad_save.status_code == 400


def test_every_write_form_requires_csrf(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=brief.md")
        payload = {
            "file": "brief.md",
            "text": "# Brand brief: Acme\n\nX\n",
            "base_hash": base_hash_from(page.text),
        }
        for path in (
            "/profiles/acme/validate",
            "/profiles/acme/edit/preview",
            "/profiles/acme/edit",
            "/profiles/acme/edit/helper",
        ):
            response = client.post(path, data=payload, follow_redirects=False)
            assert response.status_code == 403, path


def test_expired_session_htmx_validate_redirects_to_login(initialized: InitResult) -> None:
    with make_client(initialized) as client:
        response = client.post(
            "/profiles/acme/validate",
            data={"file": "brief.md", "text": "x"},
            headers=HX,
            follow_redirects=False,
        )
    assert response.status_code == 303
    assert response.headers.get("hx-redirect") == "/login"


def test_policy_helper_preserves_comments_and_order_without_writing(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    original = read(initialized, "policy.yaml")
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=policy.yaml")
        assert 'name="default_mode"' in page.text
        response = client.post(
            "/profiles/acme/edit/helper",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": original,
                "default_mode": "require_approval",
                "max_posts_per_day": "5",
                "quiet_start": "22:00",
                "quiet_end": "06:00",
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "Helper values applied" in response.text
    assert "# Autonomy policy" in response.text
    assert "default_mode: require_approval" in response.text
    assert "max_posts_per_day: 5" in response.text
    assert "22:00" in response.text
    assert "06:00" in response.text
    assert read(initialized, "policy.yaml") == original


def test_policy_helper_refuses_an_unsupported_shape(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    with make_client(initialized) as client:
        login(client)
        page = client.get("/profiles/acme/edit?file=policy.yaml")
        response = client.post(
            "/profiles/acme/edit/helper",
            data={
                "csrf": csrf_from(page.text),
                "file": "policy.yaml",
                "text": read(initialized, "policy.yaml"),
                "default_mode": "draft_only",
                "max_posts_per_day": "3",
                "quiet_start": "22:00",
                "quiet_end": "",
            },
            follow_redirects=False,
        )
    assert response.status_code == 200
    assert "could not update this file safely" in response.text


def _policy(extra: str = "") -> str:
    return (
        "default_mode: draft_only\n"
        "max_posts_per_day: 3\n"
        "disclose_ai: true\n"
        'quiet_hours:\n  start: "21:00"\n  end: "07:00"\n' + extra
    )


def test_loosens_autonomy_detects_every_listed_case() -> None:
    base = _policy("rules: []\n")
    assert loosens_autonomy("policy.yaml", base, base.replace("draft_only", "require_approval"))
    assert loosens_autonomy(
        "policy.yaml", base, base.replace("max_posts_per_day: 3", "max_posts_per_day: 4")
    )
    assert loosens_autonomy(
        "policy.yaml", base, base.replace("disclose_ai: true", "disclose_ai: false")
    )
    assert loosens_autonomy("policy.yaml", base, base.replace('"21:00"', '"22:00"'))
    assert loosens_autonomy(
        "policy.yaml",
        base,
        base.replace('quiet_hours:\n  start: "21:00"\n  end: "07:00"\n', ""),
    )

    rule = "rules:\n  - action: post\n    risk: low\n    mode: require_approval\n"
    base_rule = _policy(rule)
    assert loosens_autonomy(
        "policy.yaml", base_rule, base_rule.replace("mode: require_approval", "mode: auto")
    )
    assert loosens_autonomy(
        "policy.yaml",
        base_rule,
        base_rule.replace("mode: require_approval", "mode: auto_after_delay\n    delay_minutes: 5"),
    )

    new_auto = _policy("rules:\n  - action: post\n    risk: low\n    mode: auto\n")
    assert loosens_autonomy("policy.yaml", base, new_auto)

    delayed = _policy(
        "rules:\n"
        "  - action: post\n"
        "    risk: low\n"
        "    mode: auto_after_delay\n"
        "    delay_minutes: 60\n"
    )
    shorter = delayed.replace("delay_minutes: 60", "delay_minutes: 30")
    assert loosens_autonomy("policy.yaml", delayed, shorter)

    auto_low = _policy("rules:\n  - action: post\n    risk: low\n    mode: auto\n")
    auto_high = auto_low.replace("risk: low", "risk: high")
    assert loosens_autonomy("policy.yaml", auto_low, auto_high)

    intent_one = _policy(
        "rules:\n  - action: post\n    risk: low\n    mode: auto\n    intents: [news]\n"
    )
    intent_two = intent_one.replace("intents: [news]", "intents: [news, promo]")
    assert loosens_autonomy("policy.yaml", intent_one, intent_two)


def test_loosens_autonomy_is_false_for_benign_changes() -> None:
    base = _policy("rules: []\n")
    assert not loosens_autonomy(
        "policy.yaml", base, base.replace("max_posts_per_day: 3", "max_posts_per_day: 2")
    )
    assert not loosens_autonomy("policy.yaml", base, base.replace('"21:00"', '"20:00"'))
    assert not loosens_autonomy(
        "policy.yaml", base, base.replace("disclose_ai: true", "disclose_ai: true")
    )
    assert not loosens_autonomy("policy.yaml", base + "# a comment\n", base + "# another comment\n")
    assert not loosens_autonomy("brief.md", "a", "b")


def test_loosens_autonomy_detects_removed_banned_entries() -> None:
    old = "banned_words:\n  - spam\n  - ads\nbanned_topics:\n  - politics\n"
    assert loosens_autonomy(
        "banned.yaml", old, "banned_words:\n  - spam\nbanned_topics:\n  - politics\n"
    )
    assert loosens_autonomy("banned.yaml", old, "banned_words:\n  - spam\n  - ads\n")
    assert not loosens_autonomy(
        "banned.yaml", old, old.replace("  - ads\n", "  - ads\n  - extra\n")
    )


def test_editor_marks_files_with_validation_problems(
    initialized: InitResult, profile_setup: ProfileSetup
) -> None:
    (initialized.layout.profiles / "acme" / "banned.yaml").write_text(
        "banned_words: [unclosed\n", encoding="utf-8"
    )
    with make_client(initialized) as client:
        login(client)
        response = client.get("/profiles/acme/edit?file=soul.md")
    assert response.status_code == 200
    assert "validation problem" in response.text
    assert "banned.yaml" in response.text


def test_policy_helper_removal_keeps_a_following_top_level_comment() -> None:
    text = (
        "default_mode: draft_only\n"
        "quiet_hours:\n"
        '  start: "21:00"\n'
        '  end: "07:00"\n'
        "# rules comment\n"
        "rules: []\n"
    )
    updated = apply_policy_helper(
        text,
        default_mode="require_approval",
        max_posts_per_day="3",
        quiet_start="",
        quiet_end="",
    )
    assert updated is not None
    assert "quiet_hours" not in updated
    assert "# rules comment" in updated
    assert updated.index("default_mode: require_approval") < updated.index("rules: []")


def test_policy_helper_updates_a_flow_style_quiet_window() -> None:
    text = 'default_mode: draft_only\nquiet_hours: {start: "21:00", end: "07:00"}\n'
    updated = apply_policy_helper(
        text,
        default_mode="draft_only",
        max_posts_per_day="3",
        quiet_start="22:00",
        quiet_end="06:00",
    )
    assert updated is not None
    assert 'start: "22:00"' in updated
    assert 'end: "06:00"' in updated
