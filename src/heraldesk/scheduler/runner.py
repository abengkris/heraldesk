"""The scheduler sweep.

Heraldesk keeps scheduling state in SQLite, so one sweep is enough to resume after a
restart: no job is stored only in memory and no item is published twice. ``heraldesk
scheduler run`` executes one deterministic sweep; ``heraldesk serve`` calls the same
function on a 60-second timer through APScheduler.

Precedence for every profile is fixed: global kill switch, per-profile kill switch,
``paused`` status, ``draft`` status, then the item's recorded ``publish_mode``. A
``draft_only`` item never auto-publishes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher
from heraldesk.db.repositories import ContentItemRepository, ProfileCatalog
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import expire_pending_approvals
from heraldesk.platforms import publish_adapter_for
from heraldesk.platforms.base import PlatformAdapter, PublishError
from heraldesk.publisher import (
    DEFAULT_RETRY_BACKOFF_SECONDS,
    MAX_PUBLISH_ATTEMPTS,
    PublishStatus,
    approval_skip_reason,
    policy_skips_scheduler,
    publish_block_reason,
    publish_item,
)
from heraldesk.telegram.notifier import NotificationService

SchedulerAdapterFactory = Callable[[str], PlatformAdapter]


@dataclass(frozen=True, slots=True)
class SchedulerEntry:
    """One due item and what happened to it."""

    item_id: int
    profile_slug: str
    platform: str
    state: str
    scheduled_at: datetime | None
    publish_mode: str | None
    publish_attempts: int
    action: str
    outcome: str | None = None
    reason: str | None = None
    external_id: str | None = None


@dataclass(frozen=True, slots=True)
class SchedulerResult:
    """Outcome of one scheduler sweep."""

    dry_run: bool
    profiles_checked: int
    entries: tuple[SchedulerEntry, ...]
    expired_approvals: int = 0

    @property
    def due(self) -> int:
        return len(self.entries)

    @property
    def published(self) -> tuple[SchedulerEntry, ...]:
        return tuple(entry for entry in self.entries if entry.outcome == PublishStatus.PUBLISHED)

    @property
    def failed(self) -> tuple[SchedulerEntry, ...]:
        return tuple(entry for entry in self.entries if entry.outcome == PublishStatus.FAILED)

    @property
    def skipped(self) -> tuple[SchedulerEntry, ...]:
        return tuple(entry for entry in self.entries if entry.outcome == PublishStatus.SKIPPED)

    @property
    def planned(self) -> tuple[SchedulerEntry, ...]:
        return tuple(entry for entry in self.entries if entry.outcome is None)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def run_scheduler(
    session: Session,
    layout: paths.Layout,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
    adapter_factory: SchedulerAdapterFactory | None = None,
    cipher: CredentialCipher | None = None,
    actor: str = "system",
    retry_backoff_seconds: Sequence[int] = DEFAULT_RETRY_BACKOFF_SECONDS,
    notifier: NotificationService | None = None,
) -> SchedulerResult:
    """Publish every due item, or list them with ``dry_run=True`` and write nothing.

    Real runs also expire stale approvals per profile. Dry runs read only: no state
    change, no attempt counter, no audit row and no adapter call.
    """
    current = _as_utc(datetime.now(UTC) if now is None else now)
    factory: SchedulerAdapterFactory = (
        adapter_factory if adapter_factory is not None else publish_adapter_for
    )
    items_repository = ContentItemRepository(session)
    summaries = ProfileCatalog(session).list()
    entries: list[SchedulerEntry] = []
    expired = 0

    for summary in summaries:
        scope = ProfileScope(summary.id)
        if not dry_run:
            expired += len(expire_pending_approvals(session, scope, now=current))

        due = items_repository.due_for_publish(
            scope,
            now=current,
            max_attempts=MAX_PUBLISH_ATTEMPTS,
        )
        if not due:
            continue

        block = publish_block_reason(session, layout, scope)
        for item in due:
            entry = SchedulerEntry(
                item_id=item.id,
                profile_slug=summary.slug,
                platform=item.platform,
                state=item.state,
                scheduled_at=item.scheduled_at,
                publish_mode=item.publish_mode,
                publish_attempts=item.publish_attempts,
                action="retry" if item.state == "failed" else "publish",
            )
            if block is not None:
                entries.append(replace(entry, outcome=PublishStatus.SKIPPED, reason=block.value))
                continue
            if policy_skips_scheduler(item):
                entries.append(
                    replace(
                        entry,
                        outcome=PublishStatus.SKIPPED,
                        reason="publish mode draft_only is manual only",
                    )
                )
                continue
            approval_reason = approval_skip_reason(session, scope, item)
            if approval_reason is not None:
                entries.append(
                    replace(entry, outcome=PublishStatus.SKIPPED, reason=approval_reason)
                )
                continue
            if dry_run:
                entries.append(entry)
                continue

            resolved_cipher = cipher
            if resolved_cipher is None:
                resolved_cipher = CredentialCipher.from_key_file(layout.key_path)
            adapter: PlatformAdapter | None
            try:
                adapter = factory(item.platform)
            except PublishError:
                adapter = None
            outcome = publish_item(
                session,
                layout,
                scope,
                item.id,
                adapter=adapter,
                cipher=resolved_cipher,
                now=current,
                actor=actor,
                trigger="scheduler",
                retry_backoff_seconds=retry_backoff_seconds,
                notifier=notifier,
            )
            entries.append(
                replace(
                    entry,
                    outcome=outcome.status.value,
                    reason=outcome.reason or outcome.error,
                    external_id=outcome.external_id,
                    publish_attempts=outcome.attempts or entry.publish_attempts,
                )
            )

    result = SchedulerResult(
        dry_run=dry_run,
        profiles_checked=len(summaries),
        entries=tuple(entries),
        expired_approvals=expired,
    )
    if not dry_run:
        AuditService(session).record(
            None,
            actor=actor,
            action="scheduler.run",
            entity="scheduler",
            payload={
                "profiles_checked": result.profiles_checked,
                "due": result.due,
                "published": len(result.published),
                "failed": len(result.failed),
                "skipped": len(result.skipped),
                "expired_approvals": result.expired_approvals,
            },
        )
    return result
