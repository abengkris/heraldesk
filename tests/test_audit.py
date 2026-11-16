"""Append-only audit log tests."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditError, AuditService
from heraldesk.db.models import AuditLog
from heraldesk.db.repositories import AuditRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.profiles import ProfileDefinition, register_definition

pytestmark = pytest.mark.core

MakeDefinition = Callable[[str], ProfileDefinition]


def scope_for(session: Session, definition: ProfileDefinition) -> ProfileScope:
    summary, _ = register_definition(session, definition)
    return ProfileScope(summary.id)


def test_service_has_no_update_or_delete_methods() -> None:
    for name in ("update", "delete", "remove", "edit", "replace"):
        assert not hasattr(AuditService, name)
        assert not hasattr(AuditRepository, name)


def test_entries_append_in_order_and_are_never_replaced(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    definition = make_definition("alpha")
    scope = scope_for(db_session, definition)
    service = AuditService(db_session)
    first = service.record(
        scope, actor="user", action="profile.created", entity="profile", entity_id=scope.profile_id
    )
    second = service.record(
        scope, actor="user", action="profile.reloaded", entity="profile", entity_id=scope.profile_id
    )

    history = service.history(scope)
    assert [entry.id for entry in history] == [second.id, first.id]
    rows = db_session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
    assert len(rows) == 2
    assert rows[0].action == "profile.created"
    assert rows[1].action == "profile.reloaded"


def test_profile_history_is_isolated(db_session: Session, make_definition: MakeDefinition) -> None:
    alpha = scope_for(db_session, make_definition("alpha"))
    beta = scope_for(db_session, make_definition("beta"))
    service = AuditService(db_session)
    service.record(alpha, actor="user", action="profile.created", entity="profile")
    service.record(beta, actor="user", action="profile.created", entity="profile")

    alpha_history = service.history(alpha)
    beta_history = service.history(beta)
    assert len(alpha_history) == 1
    assert len(beta_history) == 1
    assert alpha_history[0].profile_id == alpha.profile_id
    assert beta_history[0].profile_id == beta.profile_id
    assert alpha_history[0].id != beta_history[0].id


def test_global_entries_are_separate_from_profile_entries(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    scope = scope_for(db_session, make_definition("alpha"))
    service = AuditService(db_session)
    service.record(scope, actor="user", action="profile.created", entity="profile")
    service.record(
        None,
        actor="user",
        action="killswitch.engaged",
        entity="killswitch",
        payload={"scope": "global", "enabled": True},
    )

    global_history = service.history(None)
    assert len(global_history) == 1
    assert global_history[0].profile_id is None
    assert global_history[0].payload == {"scope": "global", "enabled": True}
    assert len(service.history(scope)) == 1


def test_entity_id_is_stored_as_text(db_session: Session, make_definition: MakeDefinition) -> None:
    scope = scope_for(db_session, make_definition("alpha"))
    entry = AuditService(db_session).record(
        scope, actor="user", action="account.added", entity="social_account", entity_id=42
    )
    assert entry.entity_id == "42"


def test_secret_looking_payload_keys_are_rejected(
    db_session: Session, make_definition: MakeDefinition
) -> None:
    scope = scope_for(db_session, make_definition("alpha"))
    service = AuditService(db_session)
    with pytest.raises(AuditError, match="looks like a secret"):
        service.record(
            scope,
            actor="user",
            action="account.added",
            entity="social_account",
            payload={"token": "abc"},
        )
    with pytest.raises(AuditError, match="looks like a secret"):
        service.record(
            scope,
            actor="user",
            action="account.added",
            entity="social_account",
            payload={"account": {"api_key": "abc"}},
        )
    assert service.history(scope) == []


def test_invalid_actor_is_rejected(db_session: Session, make_definition: MakeDefinition) -> None:
    scope = scope_for(db_session, make_definition("alpha"))
    with pytest.raises(AuditError, match="actor"):
        AuditService(db_session).record(
            scope, actor="Agent:Writer", action="profile.created", entity="profile"
        )


def test_non_json_payload_is_rejected(db_session: Session, make_definition: MakeDefinition) -> None:
    scope = scope_for(db_session, make_definition("alpha"))
    with pytest.raises(AuditError, match="JSON serializable"):
        AuditService(db_session).record(
            scope,
            actor="system",
            action="test",
            entity="test",
            payload={"value": object()},
        )
