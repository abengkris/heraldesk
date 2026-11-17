"""Profile isolation and scoped repository API tests."""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime

import pytest
from sqlalchemy.orm import Session

from heraldesk.core.bootstrap import InitResult
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.repositories import (
    ApprovalRepository,
    AuditRepository,
    ContentItemLocator,
    ContentItemRepository,
    JobRepository,
    ProfileRepository,
    SocialAccountRepository,
)
from heraldesk.db.scope import ProfileScope, ScopeError
from heraldesk.domain.accounts import add_account
from heraldesk.domain.profiles import ProfileDefinition, register_definition

pytestmark = pytest.mark.core

MakeDefinition = Callable[[str], ProfileDefinition]


def make_scope(session: Session, definition: ProfileDefinition) -> ProfileScope:
    summary, _ = register_definition(session, definition)
    return ProfileScope(summary.id)


def test_profile_scope_rejects_invalid_ids() -> None:
    with pytest.raises(ScopeError):
        ProfileScope(0)
    with pytest.raises(ScopeError):
        ProfileScope(-1)


def test_profile_repository_cannot_update_another_profile(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    alpha = make_scope(db_session, make_definition("alpha"))
    beta = make_scope(db_session, make_definition("beta"))
    repository = ProfileRepository(db_session)

    repository.set_kill_switch(alpha, True)

    assert repository.require(alpha).kill_switch is True
    assert repository.require(beta).kill_switch is False


def test_accounts_are_invisible_across_scopes(
    db_session: Session, make_definition: MakeDefinition, initialized: InitResult
) -> None:
    alpha = make_scope(db_session, make_definition("alpha"))
    beta = make_scope(db_session, make_definition("beta"))
    cipher = CredentialCipher.from_key_file(initialized.layout.key_path)
    account = add_account(
        db_session,
        alpha,
        platform="fake",
        handle="@alpha",
        credentials={"token": "secret-alpha"},
        cipher=cipher,
    )
    repository = SocialAccountRepository(db_session)

    assert [item.id for item in repository.list(alpha)] == [account.id]
    assert repository.list(beta) == []
    assert repository.get(beta, account.id) is None
    assert repository.find(beta, platform="fake", handle="@alpha") is None
    assert (
        repository.mark_checked(beta, account.id, status="active", checked_at=datetime.now(UTC))
        is None
    )


def test_jobs_are_invisible_across_scopes(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    alpha = make_scope(db_session, make_definition("alpha"))
    beta = make_scope(db_session, make_definition("beta"))
    repository = JobRepository(db_session)
    job = repository.add(alpha, kind="daily_content", run_at=datetime.now(UTC))

    assert [item.id for item in repository.list(alpha)] == [job.id]
    assert repository.list(beta) == []
    assert repository.get(beta, job.id) is None
    assert repository.get(alpha, job.id) is not None


def test_every_scoped_repository_method_takes_scope_first() -> None:
    classes = (
        ProfileRepository,
        SocialAccountRepository,
        AuditRepository,
        JobRepository,
        ContentItemRepository,
        ApprovalRepository,
    )
    for cls in classes:
        for name, method in inspect.getmembers(cls, inspect.isfunction):
            if name.startswith("_"):
                continue
            parameters = list(inspect.signature(method).parameters)
            assert parameters, f"{cls.__name__}.{name} has no parameters"
            assert parameters[0] == "self"
            assert parameters[1] == "scope", f"{cls.__name__}.{name} must take scope first"


def test_approvals_are_invisible_across_scopes(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    alpha = make_scope(db_session, make_definition("alpha"))
    beta = make_scope(db_session, make_definition("beta"))
    item = ContentItemRepository(db_session).add(
        alpha,
        platform="fake",
        body="alpha item",
        state="pending_approval",
    )
    approval = ApprovalRepository(db_session).add(
        alpha,
        content_item_id=item.id,
        expires_at=datetime.now(UTC),
    )
    repository = ApprovalRepository(db_session)

    assert repository.get(alpha, approval.id) is not None
    assert repository.get(beta, approval.id) is None
    assert repository.pending_for_item(beta, item.id) is None
    assert repository.list(beta) == []

    assert ContentItemLocator(db_session).scope_for_content_item(item.id) == alpha
    assert ContentItemLocator(db_session).scope_for_content_item(9999) is None


def test_profile_repository_reads_only_the_scoped_profile(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    alpha = make_scope(db_session, make_definition("alpha"))
    beta = make_scope(db_session, make_definition("beta"))
    repository = ProfileRepository(db_session)

    assert repository.require(alpha).slug == "alpha"
    assert repository.require(beta).slug == "beta"
    assert repository.get(ProfileScope(999)) is None
