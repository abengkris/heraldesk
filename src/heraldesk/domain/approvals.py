"""Approval requests, human decisions, edits and expiry.

Every content item in ``pending_approval`` has exactly one open row in ``approvals``.
The service is the only caller that changes those rows, and every content state change
still goes through :func:`heraldesk.domain.content.transition`.

M4 swept stale approvals opportunistically from ``queue list``. M5 keeps that safety net
and also owns a periodic sweep in the scheduler.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final, Literal

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.core.locks import BlockReason, is_global_kill_switch_enabled
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import Approval, ContentItem, Profile
from heraldesk.db.repositories import (
    ApprovalRepository,
    ContentItemLocator,
    ContentItemRepository,
    ProfileRepository,
    RecordNotFound,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.content import ContentState, parse_state, transition
from heraldesk.domain.errors import (
    ApprovalBlockedError,
    ApprovalError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalStateError,
    ContentItemNotFoundError,
    IllegalContentTransition,
)

APPROVAL_TTL_HOURS: Final = 72
APPROVAL_TTL: Final = timedelta(hours=APPROVAL_TTL_HOURS)
MAX_AUDIT_TEXT: Final = 500
QUEUE_STATES: Final[tuple[str, ...]] = (
    ContentState.DRAFTED.value,
    ContentState.REVIEWED.value,
    ContentState.PENDING_APPROVAL.value,
    ContentState.APPROVED.value,
    ContentState.SCHEDULED.value,
    ContentState.BLOCKED.value,
    ContentState.REJECTED.value,
    ContentState.NEEDS_REVIEW.value,
    ContentState.FAILED.value,
)
SCHEDULED_AFTER_APPROVAL: Final = frozenset(
    {
        "require_approval",
        "auto",
        "auto_publish",
        "auto_after_delay",
    }
)

ApprovalChannel = Literal["web", "telegram", "cli"]


class ApprovalState(StrEnum):
    """Lifecycle of one approval row."""

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True, slots=True)
class ApprovalOutcome:
    """Result of an approve or reject decision."""

    approval_id: int
    content_item_id: int
    from_state: str
    to_state: str
    approval_state: str
    risk_level: str | None
    reason: str | None


@dataclass(frozen=True, slots=True)
class EditOutcome:
    """Result of a human edit."""

    content_item_id: int
    from_state: str
    to_state: str
    approval_id: int | None
    risk_cleared: bool


@dataclass(frozen=True, slots=True)
class ExpiryOutcome:
    """One approval expired by the queue sweep."""

    approval_id: int
    content_item_id: int
    from_state: str
    cancelled: bool


@dataclass(frozen=True, slots=True)
class QueueEntry:
    """One item as shown by ``heraldesk queue list``."""

    item_id: int
    platform: str
    item_state: str
    risk_level: str | None
    body: str
    approval_id: int | None
    approval_state: str | None
    expires_at: datetime | None
    reason: str | None


def now_or_utc(now: datetime | None) -> datetime:
    """Return *now* or the current UTC time."""
    return datetime.now(UTC) if now is None else now


def as_utc(value: datetime) -> datetime:
    """Attach UTC to a naive SQLite datetime or normalize an aware one."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _redact(value: str | None) -> str | None:
    if value is None:
        return None
    return SecretRegistry.from_environment().redact(value)


def _body_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _audit_text(value: str | None) -> str | None:
    redacted = _redact(value)
    if redacted is None:
        return None
    return redacted[:MAX_AUDIT_TEXT]


def _require_item(session: Session, scope: ProfileScope, item_id: int) -> ContentItem:
    item = ContentItemRepository(session).get(scope, item_id)
    if item is None:
        raise ContentItemNotFoundError(
            f"content item {item_id} does not exist for profile {scope.profile_id}"
        )
    return item


def _require_profile(session: Session, scope: ProfileScope) -> Profile:
    try:
        return ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ApprovalError(str(exc)) from exc


def _require_pending_approval(
    session: Session,
    scope: ProfileScope,
    content_item_id: int,
    now: datetime,
) -> Approval:
    approval = ApprovalRepository(session).pending_for_item(scope, content_item_id)
    if approval is None:
        raise ApprovalNotFoundError(f"content item {content_item_id} has no pending approval")
    if as_utc(approval.expires_at) <= now:
        raise ApprovalExpiredError(
            f"approval {approval.id} for content item {content_item_id} expired at "
            f"{as_utc(approval.expires_at).isoformat()}; run 'heraldesk queue list' to sweep it"
        )
    return approval


def _assert_approval_allowed(layout: paths.Layout, profile: Profile) -> None:
    reason: BlockReason | None = None
    if is_global_kill_switch_enabled(layout):
        reason = BlockReason.GLOBAL_KILL_SWITCH
    elif profile.kill_switch:
        reason = BlockReason.PROFILE_KILL_SWITCH
    elif profile.status == "paused":
        reason = BlockReason.PROFILE_PAUSED
    if reason is not None:
        raise ApprovalBlockedError(f"approval refused: {reason.value} outranks the approval action")


def _record_expired(
    session: Session,
    scope: ProfileScope,
    *,
    approval: Approval,
    from_state: str,
    cancelled: bool,
    reason: str,
    actor: str = "system",
) -> None:
    AuditService(session).record(
        scope,
        actor=actor,
        action="approval.expired",
        entity="approval",
        entity_id=approval.id,
        payload={
            "approval_id": approval.id,
            "content_item_id": approval.content_item_id,
            "from_state": from_state,
            "expires_at": as_utc(approval.expires_at).isoformat(),
            "cancelled": cancelled,
            "reason": reason,
        },
    )


def request_approval(
    session: Session,
    scope: ProfileScope,
    content_item_id: int,
    *,
    now: datetime | None = None,
    channel: ApprovalChannel = "cli",
    actor: str = "system",
    policy_mode: str | None = None,
    edited_body: str | None = None,
) -> Approval:
    """Create one pending approval for a pending_approval item, idempotently."""
    current = now_or_utc(now)
    item = _require_item(session, scope, content_item_id)
    if item.state != ContentState.PENDING_APPROVAL.value:
        raise ApprovalStateError(f"content item {item.id} is {item.state}, not pending_approval")

    repository = ApprovalRepository(session)
    existing = repository.pending_for_item(scope, item.id)
    if existing is not None:
        return existing

    approval = repository.add(
        scope,
        content_item_id=item.id,
        expires_at=current + APPROVAL_TTL,
        created_at=current,
        channel=channel,
        edited_body=_redact(edited_body),
    )
    payload: dict[str, object] = {
        "approval_id": approval.id,
        "content_item_id": item.id,
        "risk_level": item.risk_level,
        "expires_at": as_utc(approval.expires_at).isoformat(),
        "channel": channel,
    }
    if policy_mode is not None:
        payload["policy_mode"] = policy_mode
    AuditService(session).record(
        scope,
        actor=actor,
        action="approval.requested",
        entity="approval",
        entity_id=approval.id,
        payload=payload,
    )
    return approval


def approve(
    session: Session,
    layout: paths.Layout,
    scope: ProfileScope,
    content_item_id: int,
    *,
    actor: str = "user",
    channel: ApprovalChannel = "cli",
    now: datetime | None = None,
) -> ApprovalOutcome:
    """Approve one pending item. Refuses while a kill switch or pause is active."""
    current = now_or_utc(now)
    profile = _require_profile(session, scope)
    _assert_approval_allowed(layout, profile)

    item = _require_item(session, scope, content_item_id)
    if item.state != ContentState.PENDING_APPROVAL.value:
        raise ApprovalStateError(f"content item {item.id} is {item.state}, not pending_approval")
    approval = _require_pending_approval(session, scope, item.id, current)

    updated = transition(
        session,
        scope,
        item.id,
        ContentState.APPROVED,
        actor=actor,
        reason=f"approved via {channel}",
    )
    decided = ApprovalRepository(session).update_decision(
        scope,
        approval.id,
        state=ApprovalState.APPROVED.value,
        decision="approve",
        reason=None,
        edited_body=None,
        decided_at=current,
        channel=channel,
    )
    if decided is None:
        raise ApprovalNotFoundError(f"approval {approval.id} disappeared during approval")

    to_state = updated.state
    if item.publish_mode in SCHEDULED_AFTER_APPROVAL:
        delay_minutes = item.publish_delay_minutes or 0
        if item.publish_mode == "auto_after_delay":
            schedule_at = current + timedelta(minutes=max(1, delay_minutes))
            reason = f"approved via {channel}; auto_after_delay window of {delay_minutes} minutes"
        else:
            schedule_at = current
            reason = f"approved via {channel}; scheduled for the next publisher run"
        updated = transition(
            session,
            scope,
            item.id,
            ContentState.SCHEDULED,
            actor=actor,
            reason=reason,
            schedule_at=schedule_at,
        )
        to_state = updated.state

    AuditService(session).record(
        scope,
        actor=actor,
        action="approval.approved",
        entity="approval",
        entity_id=approval.id,
        payload={
            "approval_id": approval.id,
            "content_item_id": item.id,
            "from_state": ContentState.PENDING_APPROVAL.value,
            "to_state": to_state,
            "risk_level": item.risk_level,
            "channel": channel,
            "body_sha256": _body_hash(item.body),
        },
    )
    return ApprovalOutcome(
        approval_id=approval.id,
        content_item_id=item.id,
        from_state=ContentState.PENDING_APPROVAL.value,
        to_state=to_state,
        approval_state=ApprovalState.APPROVED.value,
        risk_level=item.risk_level,
        reason=None,
    )


def reject(
    session: Session,
    scope: ProfileScope,
    content_item_id: int,
    *,
    reason: str | None = None,
    actor: str = "user",
    channel: ApprovalChannel = "cli",
    now: datetime | None = None,
) -> ApprovalOutcome:
    """Reject one pending item, optionally with a redacted reason."""
    current = now_or_utc(now)
    item = _require_item(session, scope, content_item_id)
    if item.state != ContentState.PENDING_APPROVAL.value:
        raise ApprovalStateError(f"content item {item.id} is {item.state}, not pending_approval")
    approval = _require_pending_approval(session, scope, item.id, current)

    redacted_reason = _redact(reason)
    transition_reason = f"rejected via {channel}"
    if redacted_reason:
        transition_reason = f"{transition_reason}: {_audit_text(redacted_reason)}"
    updated = transition(
        session,
        scope,
        item.id,
        ContentState.REJECTED,
        actor=actor,
        reason=transition_reason,
    )
    decided = ApprovalRepository(session).update_decision(
        scope,
        approval.id,
        state=ApprovalState.REJECTED.value,
        decision="reject",
        reason=redacted_reason,
        edited_body=None,
        decided_at=current,
        channel=channel,
    )
    if decided is None:
        raise ApprovalNotFoundError(f"approval {approval.id} disappeared during rejection")

    AuditService(session).record(
        scope,
        actor=actor,
        action="approval.rejected",
        entity="approval",
        entity_id=approval.id,
        payload={
            "approval_id": approval.id,
            "content_item_id": item.id,
            "from_state": ContentState.PENDING_APPROVAL.value,
            "to_state": ContentState.REJECTED.value,
            "risk_level": item.risk_level,
            "channel": channel,
            "reason": _audit_text(redacted_reason),
            "body_sha256": _body_hash(item.body),
        },
    )
    return ApprovalOutcome(
        approval_id=approval.id,
        content_item_id=item.id,
        from_state=ContentState.PENDING_APPROVAL.value,
        to_state=updated.state,
        approval_state=ApprovalState.REJECTED.value,
        risk_level=item.risk_level,
        reason=redacted_reason,
    )


def edit_item(
    session: Session,
    scope: ProfileScope,
    content_item_id: int,
    *,
    body: str,
    actor: str = "user",
    channel: ApprovalChannel = "cli",
    now: datetime | None = None,
) -> EditOutcome:
    """Edit one item and apply the M4 human-edit transition rules.

    User edits are allowed only for ``blocked``, ``rejected``, ``needs_review``,
    ``approved`` and ``pending_approval``. ``drafted`` and ``reviewed`` are transient
    pipeline states, not user-holdable states, so an edit on them is illegal in M4.
    """
    current = now_or_utc(now)
    redacted_body = _redact(body)
    if redacted_body is None or not redacted_body.strip():
        raise ApprovalError("edited body must not be empty")

    item = _require_item(session, scope, content_item_id)
    source = parse_state(item.state)
    editable = frozenset(
        {
            ContentState.BLOCKED,
            ContentState.REJECTED,
            ContentState.NEEDS_REVIEW,
            ContentState.APPROVED,
            ContentState.PENDING_APPROVAL,
        }
    )
    if source not in editable:
        raise ApprovalStateError(
            f"content item {item.id} is {source.value}; user edits are only allowed for "
            "blocked, rejected, needs_review, approved and pending_approval"
        )

    repository = ApprovalRepository(session)
    pending: Approval | None = None
    if source is ContentState.PENDING_APPROVAL:
        pending = _require_pending_approval(session, scope, item.id, current)

    ContentItemRepository(session).update_draft(scope, item.id, body=redacted_body)
    ContentItemRepository(session).set_risk(
        scope,
        item.id,
        risk_level=None,
        risk_notes=None,
    )

    approval_id: int | None = None
    if source in {
        ContentState.BLOCKED,
        ContentState.REJECTED,
        ContentState.NEEDS_REVIEW,
    }:
        stale = repository.pending_for_item(scope, item.id)
        if stale is not None:
            repository.update_decision(
                scope,
                stale.id,
                state=ApprovalState.EXPIRED.value,
                decision=None,
                reason="content edited; approval closed",
                edited_body=None,
                decided_at=current,
                channel=channel,
            )
            _record_expired(
                session,
                scope,
                approval=stale,
                from_state=source.value,
                cancelled=False,
                reason="content edited; approval closed",
                actor=actor,
            )
        transition(
            session,
            scope,
            item.id,
            ContentState.DRAFTED,
            actor=actor,
            reason="human edit; Compliance re-evaluation required",
        )
        target = ContentState.DRAFTED
    elif source is ContentState.APPROVED:
        transition(
            session,
            scope,
            item.id,
            ContentState.PENDING_APPROVAL,
            actor=actor,
            reason="human edit after approval; a new approval is required",
        )
        approval = request_approval(
            session,
            scope,
            item.id,
            now=current,
            channel=channel,
            actor=actor,
            edited_body=redacted_body,
        )
        approval_id = approval.id
        target = ContentState.PENDING_APPROVAL
    else:
        if pending is None:
            raise ApprovalNotFoundError(f"content item {item.id} has no pending approval")
        updated_pending = repository.touch_pending(
            scope,
            pending.id,
            edited_body=redacted_body,
            expires_at=current + APPROVAL_TTL,
        )
        if updated_pending is None:
            raise ApprovalNotFoundError(f"approval {pending.id} disappeared during the edit")
        approval_id = updated_pending.id
        target = ContentState.PENDING_APPROVAL

    AuditService(session).record(
        scope,
        actor=actor,
        action="approval.edited",
        entity="approval",
        entity_id=approval_id,
        payload={
            "content_item_id": item.id,
            "approval_id": approval_id,
            "from_state": source.value,
            "to_state": target.value,
            "channel": channel,
            "risk_cleared": True,
            "body_sha256": _body_hash(redacted_body),
            "body_length": len(redacted_body),
        },
    )
    return EditOutcome(
        content_item_id=item.id,
        from_state=source.value,
        to_state=target.value,
        approval_id=approval_id,
        risk_cleared=True,
    )


def expire_pending_approvals(
    session: Session,
    scope: ProfileScope,
    *,
    now: datetime | None = None,
) -> list[ExpiryOutcome]:
    """Expire stale pending approvals and cancel their items through ``transition``."""
    current = now_or_utc(now)
    repository = ApprovalRepository(session)
    outcomes: list[ExpiryOutcome] = []
    for approval in repository.stale_pending(scope, before=current):
        item = ContentItemRepository(session).get(scope, approval.content_item_id)
        from_state = item.state if item is not None else "missing"
        cancelled = False
        if item is not None and item.state == ContentState.PENDING_APPROVAL.value:
            try:
                transition(
                    session,
                    scope,
                    item.id,
                    ContentState.CANCELLED,
                    actor="system",
                    reason="approval expired after 72 hours",
                )
                cancelled = True
            except IllegalContentTransition:
                cancelled = False
        repository.update_decision(
            scope,
            approval.id,
            state=ApprovalState.EXPIRED.value,
            decision=None,
            reason=f"approval expired after {APPROVAL_TTL_HOURS} hours",
            edited_body=None,
            decided_at=current,
            channel=approval.channel,
        )
        _record_expired(
            session,
            scope,
            approval=approval,
            from_state=from_state,
            cancelled=cancelled,
            reason=f"approval expired after {APPROVAL_TTL_HOURS} hours",
        )
        outcomes.append(
            ExpiryOutcome(
                approval_id=approval.id,
                content_item_id=approval.content_item_id,
                from_state=from_state,
                cancelled=cancelled,
            )
        )
    return outcomes


def list_queue(
    session: Session,
    scope: ProfileScope,
    *,
    limit: int = 200,
) -> list[QueueEntry]:
    """Return actionable content items and their open approvals, newest first."""
    items = ContentItemRepository(session).list(scope, states=QUEUE_STATES, limit=limit)
    repository = ApprovalRepository(session)
    entries: list[QueueEntry] = []
    for item in items:
        pending = repository.pending_for_item(scope, item.id)
        latest = pending if pending is not None else repository.latest_for_item(scope, item.id)
        reason = _redact(latest.reason) if latest is not None else None
        if reason is None and item.state == ContentState.FAILED.value and item.last_error:
            reason = _redact(item.last_error)
        if reason is None and item.risk_notes:
            reason = _redact(item.risk_notes)
        if reason is not None:
            reason = reason[:MAX_AUDIT_TEXT]
        entries.append(
            QueueEntry(
                item_id=item.id,
                platform=item.platform,
                item_state=item.state,
                risk_level=item.risk_level,
                body=item.body,
                approval_id=pending.id if pending is not None else None,
                approval_state=pending.state if pending is not None else None,
                expires_at=as_utc(pending.expires_at) if pending is not None else None,
                reason=reason,
            )
        )
    return entries


def scope_for_content_item(session: Session, item_id: int) -> ProfileScope | None:
    """Resolve a content item id through the narrow global locator."""
    return ContentItemLocator(session).scope_for_content_item(item_id)
