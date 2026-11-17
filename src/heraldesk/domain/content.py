"""The content state machine.

A content item changes state only through :func:`transition`. The legal edges are
explicit so a pipeline can never invent a shortcut, and every accepted change is written
to the append-only audit log.

M4 opens the human-edit paths: ``blocked``, ``rejected`` and ``needs_review`` can move
back to ``drafted`` for Compliance re-evaluation, and ``approved`` can move back to
``pending_approval`` after an edit. Only ``published`` and ``cancelled`` are truly
terminal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.db.models import ContentItem, utcnow
from heraldesk.db.repositories import ContentItemRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import ContentItemNotFoundError, IllegalContentTransition


class ContentState(StrEnum):
    """Every state a content item can occupy."""

    DRAFTED = "drafted"
    REVIEWED = "reviewed"
    PENDING_APPROVAL = "pending_approval"
    APPROVED = "approved"
    SCHEDULED = "scheduled"
    PUBLISHED = "published"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    CANCELLED = "cancelled"
    FAILED = "failed"
    NEEDS_REVIEW = "needs_review"


LEGAL_TRANSITIONS: Final[dict[ContentState, frozenset[ContentState]]] = {
    ContentState.DRAFTED: frozenset(
        {
            ContentState.REVIEWED,
            ContentState.BLOCKED,
            ContentState.CANCELLED,
            ContentState.NEEDS_REVIEW,
        }
    ),
    ContentState.REVIEWED: frozenset(
        {
            ContentState.PENDING_APPROVAL,
            ContentState.BLOCKED,
            ContentState.CANCELLED,
        }
    ),
    ContentState.PENDING_APPROVAL: frozenset(
        {
            ContentState.APPROVED,
            ContentState.REJECTED,
            ContentState.BLOCKED,
            ContentState.CANCELLED,
        }
    ),
    ContentState.APPROVED: frozenset(
        {
            ContentState.SCHEDULED,
            ContentState.PENDING_APPROVAL,
            ContentState.CANCELLED,
        }
    ),
    ContentState.SCHEDULED: frozenset(
        {
            ContentState.PUBLISHED,
            ContentState.FAILED,
            ContentState.CANCELLED,
        }
    ),
    ContentState.FAILED: frozenset(
        {
            ContentState.SCHEDULED,
            ContentState.NEEDS_REVIEW,
            ContentState.CANCELLED,
        }
    ),
    ContentState.NEEDS_REVIEW: frozenset(
        {
            ContentState.DRAFTED,
            ContentState.CANCELLED,
        }
    ),
    ContentState.PUBLISHED: frozenset(),
    ContentState.BLOCKED: frozenset({ContentState.DRAFTED}),
    ContentState.REJECTED: frozenset({ContentState.DRAFTED}),
    ContentState.CANCELLED: frozenset(),
}

TERMINAL_STATES: Final[frozenset[ContentState]] = frozenset(
    {
        ContentState.PUBLISHED,
        ContentState.CANCELLED,
    }
)


def parse_state(value: ContentState | str) -> ContentState:
    """Return a :class:`ContentState` or raise a readable domain error."""
    try:
        return value if isinstance(value, ContentState) else ContentState(value)
    except ValueError as exc:
        known = ", ".join(state.value for state in ContentState)
        raise IllegalContentTransition(
            f"unknown content state {value!r}; known states: {known}"
        ) from exc


def can_transition(current: ContentState | str, target: ContentState | str) -> bool:
    """Return ``True`` when *current* may move to *target* in M3."""
    source = parse_state(current)
    destination = parse_state(target)
    return destination in LEGAL_TRANSITIONS[source]


def transition(
    session: Session,
    scope: ProfileScope,
    item_id: int,
    to_state: ContentState | str,
    *,
    actor: str = "system",
    reason: str | None = None,
) -> ContentItem:
    """Move one scoped content item to *to_state* or raise.

    This is the only function that changes ``content_items.state``. A rejected transition
    leaves the row untouched and writes no audit entry.
    """
    destination = parse_state(to_state)
    repository = ContentItemRepository(session)
    item = repository.get(scope, item_id)
    if item is None:
        raise ContentItemNotFoundError(
            f"content item {item_id} does not exist for profile {scope.profile_id}"
        )

    source = parse_state(item.state)
    if not can_transition(source, destination):
        raise IllegalContentTransition(
            f"content item {item.id} cannot move from {source.value} to {destination.value}"
        )

    item.state = destination.value
    item.updated_at = utcnow()
    if destination is ContentState.SCHEDULED:
        item.scheduled_at = item.scheduled_at or utcnow()
    if destination is ContentState.PUBLISHED:
        item.published_at = item.published_at or utcnow()
    session.flush()

    payload: dict[str, str] = {
        "from_state": source.value,
        "to_state": destination.value,
    }
    if reason:
        payload["reason"] = reason
    AuditService(session).record(
        scope,
        actor=actor,
        action="content.state_changed",
        entity="content_item",
        entity_id=item.id,
        payload=payload,
    )
    return item
