"""End-to-end CLI tests for the M1 profile, account and off-switch commands."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from typer.testing import CliRunner

from heraldesk.cli.main import app
from heraldesk.core.audit import AuditService
from heraldesk.core.bootstrap import InitResult
from heraldesk.db.models import AuditLog, Profile, SocialAccount
from heraldesk.db.repositories import ProfileCatalog
from heraldesk.db.scope import ProfileScope
from heraldesk.db.session import database_session

pytestmark = pytest.mark.core

runner = CliRunner()


@contextmanager
def session_for(initialized: InitResult) -> Iterator[Session]:
    with database_session(initialized.layout) as session:
        yield session


def invoke(initialized: InitResult, *args: str):
    result = runner.invoke(app, list(args))
    return result


def test_profile_new_list_and_reload(initialized: InitResult) -> None:
    result = invoke(initialized, "profile", "new", "acme", "--name", "Acme Corp")
    assert result.exit_code == 0, result.output
    directory = initialized.layout.profiles / "acme"
    assert (directory / "soul.md").is_file()
    assert (directory / "policy.yaml").is_file()

    with session_for(initialized) as session:
        profile = session.scalar(select(Profile).where(Profile.slug == "acme"))
        assert profile is not None
        assert profile.name == "Acme Corp"
        assert profile.status == "draft"
        assert profile.config_hash

    listed = invoke(initialized, "profile", "list")
    assert listed.exit_code == 0
    assert "acme" in listed.output
    assert "draft" in listed.output

    (directory / "brief.md").write_text("# Brief\n\nChanged.\n", encoding="utf-8")
    reloaded = invoke(initialized, "profile", "reload", "acme")
    assert reloaded.exit_code == 0
    assert "Reloaded" in reloaded.output


def test_profile_new_rejects_duplicates_and_bad_slugs(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    duplicate = invoke(initialized, "profile", "new", "acme")
    assert duplicate.exit_code == 1
    assert "already" in duplicate.output.lower()

    bad = invoke(initialized, "profile", "new", "Not A Slug")
    assert bad.exit_code == 1
    assert "slug" in bad.output.lower()


def test_profile_reload_failure_keeps_the_old_hash(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    with session_for(initialized) as session:
        before = session.scalar(select(Profile).where(Profile.slug == "acme"))
        assert before is not None
        old_hash = before.config_hash

    (initialized.layout.profiles / "acme" / "policy.yaml").write_text(
        "rules: [\n", encoding="utf-8"
    )
    result = invoke(initialized, "profile", "reload", "acme")
    assert result.exit_code == 1
    assert "policy.yaml" in result.output

    with session_for(initialized) as session:
        after = session.scalar(select(Profile).where(Profile.slug == "acme"))
        assert after is not None
        assert after.config_hash == old_hash


def test_profile_reload_all_reports_failures(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "alpha").exit_code == 0
    assert invoke(initialized, "profile", "new", "beta").exit_code == 0
    (initialized.layout.profiles / "beta" / "banned.yaml").unlink()

    result = invoke(initialized, "profile", "reload")
    assert result.exit_code == 1
    assert "alpha" in result.output
    assert "beta" in result.output
    assert "banned.yaml" in result.output


def test_profile_commands_reject_path_traversal(initialized: InitResult) -> None:
    reloaded = invoke(initialized, "profile", "reload", "../acme")
    assert reloaded.exit_code == 1
    assert "slug" in reloaded.output.lower()

    paused = invoke(initialized, "profile", "pause", "../acme")
    assert paused.exit_code == 1
    assert "slug" in paused.output.lower()


def test_pause_resume_and_killswitch(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0

    paused = invoke(initialized, "profile", "pause", "acme")
    assert paused.exit_code == 0
    assert "paused" in paused.output
    assert invoke(initialized, "profile", "pause", "acme").exit_code == 0

    resumed = invoke(initialized, "profile", "resume", "acme")
    assert resumed.exit_code == 0
    assert "active" in resumed.output

    engaged = invoke(initialized, "profile", "killswitch", "acme")
    assert engaged.exit_code == 0
    assert "engaged" in engaged.output
    with session_for(initialized) as session:
        profile = session.scalar(select(Profile).where(Profile.slug == "acme"))
        assert profile is not None
        assert profile.kill_switch is True

    cleared = invoke(initialized, "profile", "killswitch", "acme", "--off")
    assert cleared.exit_code == 0
    assert "cleared" in cleared.output


def test_stop_all_engages_and_clears_the_global_marker(initialized: InitResult) -> None:
    marker = initialized.layout.home / "killswitch"
    assert not marker.exists()

    result = invoke(initialized, "stop-all")
    assert result.exit_code == 0
    assert marker.is_file()
    assert "engaged" in result.output
    listed = invoke(initialized, "profile", "list")
    assert "Global kill switch is engaged" in listed.output

    again = invoke(initialized, "stop-all")
    assert again.exit_code == 0
    assert "already" in again.output

    cleared = invoke(initialized, "stop-all", "--off")
    assert cleared.exit_code == 0
    assert not marker.exists()

    with session_for(initialized) as session:
        actions = [entry.action for entry in AuditService(session).history(None)]
    assert actions == ["killswitch.disengaged", "killswitch.engaged"]


def test_account_add_and_test_do_not_print_or_store_plaintext(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    added = invoke(
        initialized,
        "account",
        "add",
        "acme",
        "--platform",
        "fake",
        "--handle",
        "@acme",
        "--credential",
        "token=super-secret",
    )
    assert added.exit_code == 0, added.output
    assert "super-secret" not in added.output
    assert "encrypted" in added.output.lower()

    with session_for(initialized) as session:
        profile_id = ProfileCatalog(session).resolve("acme")
        assert profile_id is not None
        scope = ProfileScope(profile_id)
        account = session.scalar(
            select(SocialAccount).where(SocialAccount.profile_id == profile_id)
        )
        assert account is not None
        assert "super-secret" not in account.credentials_encrypted
        for entry in AuditService(session).history(scope):
            assert "super-secret" not in str(entry.payload)

    tested = invoke(initialized, "account", "test", "acme")
    assert tested.exit_code == 0, tested.output
    assert "credentials accepted" in tested.output
    assert "super-secret" not in tested.output

    with session_for(initialized) as session:
        profile_id = ProfileCatalog(session).resolve("acme")
        assert profile_id is not None
        account = session.scalar(
            select(SocialAccount).where(SocialAccount.profile_id == profile_id)
        )
        assert account is not None
        assert account.status == "active"


def test_account_test_failure_exits_one_but_keeps_the_audit(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    assert (
        invoke(
            initialized,
            "account",
            "add",
            "acme",
            "--platform",
            "fake",
            "--handle",
            "@acme",
            "--credential",
            "username=no-token",
        ).exit_code
        == 0
    )

    result = invoke(initialized, "account", "test", "acme")
    assert result.exit_code == 1
    assert "fail" in result.output

    with session_for(initialized) as session:
        profile_id = ProfileCatalog(session).resolve("acme")
        assert profile_id is not None
        account = session.scalar(
            select(SocialAccount).where(SocialAccount.profile_id == profile_id)
        )
        assert account is not None
        assert account.status == "error"
        assert "account.tested" in [
            entry.action for entry in AuditService(session).history(ProfileScope(profile_id))
        ]


def test_account_add_rejects_unknown_platform(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    result = invoke(
        initialized,
        "account",
        "add",
        "acme",
        "--platform",
        "myspace",
        "--handle",
        "@acme",
        "--credential",
        "token=x",
    )
    assert result.exit_code == 1
    assert "unknown platform" in result.output


def test_account_add_requires_a_credential(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    result = invoke(
        initialized, "account", "add", "acme", "--platform", "fake", "--handle", "@acme"
    )
    assert result.exit_code == 1
    assert "--credential" in result.output


def test_commands_fail_cleanly_without_an_initialised_home(home: Path) -> None:
    result = runner.invoke(app, ["profile", "list"])
    assert result.exit_code == 1
    assert "heraldesk init" in result.output


def test_profile_list_with_no_profiles(initialized: InitResult) -> None:
    result = invoke(initialized, "profile", "list")
    assert result.exit_code == 0
    assert "No profiles registered" in result.output


def test_audit_log_stores_no_secret_payloads(initialized: InitResult) -> None:
    assert invoke(initialized, "profile", "new", "acme").exit_code == 0
    assert (
        invoke(
            initialized,
            "account",
            "add",
            "acme",
            "--platform",
            "fake",
            "--handle",
            "@acme",
            "--credential",
            "token=hidden",
        ).exit_code
        == 0
    )
    with session_for(initialized) as session:
        raw = session.scalars(select(AuditLog)).all()
    assert raw
    assert all("hidden" not in str(entry.payload) for entry in raw)
