"""Append-only audit log service.

Only append and read operations exist. There is deliberately no update or delete path.
Audit payloads are metadata only: they must never contain DM/comment text or credentials.
Secret-looking keys are rejected before a row is written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy.orm import Session

from heraldesk.db.models import AuditLog
from heraldesk.db.repositories import AuditRepository
from heraldesk.db.scope import ProfileScope

ACTOR_PATTERN = re.compile(r"^(user|system|agent:[a-z0-9_-]+)$")
SENSITIVE_KEY_PATTERN = re.compile(
    r"(secret|token|passwd|password|credential|api[_-]?key|private[_-]?key)",
    re.IGNORECASE,
)
MAX_FIELD_LENGTH = 64


class AuditError(Exception):
    """Raised when an audit entry is invalid."""


def validate_actor(actor: str) -> str:
    """Validate and return an audit actor (``user``, ``system`` or ``agent:<name>``)."""
    if not ACTOR_PATTERN.fullmatch(actor):
        raise AuditError("actor must be 'user', 'system' or 'agent:<name>' with a lowercase name")
    return actor


def _assert_no_secret_keys(value: object, *, path: str = "payload") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise AuditError(f"{path} keys must be strings")
            if SENSITIVE_KEY_PATTERN.search(key):
                raise AuditError(
                    f"audit payload key {path}.{key} looks like a secret; "
                    "audit records store redacted metadata only"
                )
            _assert_no_secret_keys(item, path=f"{path}.{key}")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _assert_no_secret_keys(item, path=f"{path}[{index}]")


def _checked_payload(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    if payload is None:
        return {}
    data = dict(payload)
    _assert_no_secret_keys(data)
    try:
        json.dumps(data, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AuditError("audit payload must be JSON serializable") from exc
    return data


class AuditService:
    """Write and read audit entries through the append-only repository."""

    def __init__(self, session: Session) -> None:
        self._repository = AuditRepository(session)

    def record(
        self,
        scope: ProfileScope | None,
        *,
        actor: str,
        action: str,
        entity: str,
        entity_id: int | str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> AuditLog:
        """Append one audit entry.

        ``scope=None`` records a global action. Profile actions must pass their scope.
        """
        actor = validate_actor(actor)
        if not action.strip():
            raise AuditError("audit action must not be empty")
        if len(action) > MAX_FIELD_LENGTH:
            raise AuditError(f"audit action must be at most {MAX_FIELD_LENGTH} characters")
        if not entity.strip():
            raise AuditError("audit entity must not be empty")
        if len(entity) > MAX_FIELD_LENGTH:
            raise AuditError(f"audit entity must be at most {MAX_FIELD_LENGTH} characters")
        text_id: str | None = None
        if entity_id is not None:
            text_id = str(entity_id)
            if len(text_id) > MAX_FIELD_LENGTH:
                raise AuditError(f"audit entity_id must be at most {MAX_FIELD_LENGTH} characters")
        return self._repository.append(
            scope,
            actor=actor,
            action=action.strip(),
            entity=entity.strip(),
            entity_id=text_id,
            payload=_checked_payload(payload),
        )

    def history(self, scope: ProfileScope | None, *, limit: int = 100) -> list[AuditLog]:
        """Return newest entries for a profile, or global entries when scope is None."""
        return self._repository.history(scope, limit=limit)
