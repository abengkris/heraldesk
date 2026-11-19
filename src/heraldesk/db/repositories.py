"""The only place that issues SQLAlchemy queries for domain data.

Scoped repositories take :class:`ProfileScope` as their first argument and add
``profile_id == scope.profile_id`` to every query. :class:`ProfileCatalog` is a narrow
global directory used to bootstrap an id and to list profile metadata; it never exposes
profile file content.
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from heraldesk.db.models import (
    AgentRun,
    Approval,
    AuditLog,
    ContentIdea,
    ContentItem,
    Job,
    PipelineRun,
    Profile,
    SocialAccount,
    TelegramMessage,
    TelegramNotification,
    utcnow,
)
from heraldesk.db.scope import ProfileScope

if TYPE_CHECKING:
    from heraldesk.domain.profiles import ProfileDefinition


class RepositoryError(Exception):
    """Base class for repository failures."""


class RecordNotFound(RepositoryError):
    """Raised when a scoped record does not exist."""


class DuplicateRecord(RepositoryError):
    """Raised when a unique constraint would be violated."""


@dataclass(frozen=True, slots=True)
class ProfileSummary:
    """Profile metadata safe to show in CLI listings."""

    id: int
    slug: str
    name: str
    profile_type: str
    timezone: str
    languages: tuple[str, ...]
    status: str
    kill_switch: bool
    config_hash: str


def summarize(profile: Profile) -> ProfileSummary:
    """Build a :class:`ProfileSummary` from a profile row."""
    return ProfileSummary(
        id=profile.id,
        slug=profile.slug,
        name=profile.name,
        profile_type=profile.type,
        timezone=profile.timezone,
        languages=tuple(profile.languages),
        status=profile.status,
        kill_switch=profile.kill_switch,
        config_hash=profile.config_hash,
    )


class ProfileCatalog:
    """Global profile directory for registration and metadata listing.

    Creating a profile is the one operation that happens before an id exists, so it
    cannot take a :class:`ProfileScope`. All reads after creation use
    :class:`ProfileRepository` with a scope.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list(self) -> list[ProfileSummary]:
        """Return metadata for every registered profile ordered by slug."""
        profiles = self._session.scalars(select(Profile).order_by(Profile.slug)).all()
        return [summarize(profile) for profile in profiles]

    def resolve(self, slug: str) -> int | None:
        """Return the profile id for *slug*, or ``None`` when it is not registered."""
        return self._session.scalar(select(Profile.id).where(Profile.slug == slug))

    def create(self, definition: ProfileDefinition) -> Profile:
        """Register a new profile from a validated file definition."""
        profile = Profile(
            slug=definition.slug,
            name=definition.name,
            type=definition.type.value,
            timezone=definition.timezone,
            languages=list(definition.languages),
            status="draft",
            kill_switch=False,
            config_hash=definition.config_hash,
        )
        self._session.add(profile)
        self._session.flush()
        return profile


class ProfileRepository:
    """Scoped access to one profile row."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def get(self, scope: ProfileScope) -> Profile | None:
        """Return the profile for *scope*, or ``None`` when it does not exist."""
        return self._session.scalar(select(Profile).where(Profile.id == scope.profile_id))

    def require(self, scope: ProfileScope) -> Profile:
        """Return the profile for *scope* or raise :class:`RecordNotFound`."""
        profile = self.get(scope)
        if profile is None:
            raise RecordNotFound(f"profile id {scope.profile_id} is not registered")
        return profile

    def set_status(self, scope: ProfileScope, status: str) -> Profile:
        """Change the status of the scoped profile."""
        profile = self.require(scope)
        profile.status = status
        profile.updated_at = utcnow()
        self._session.flush()
        return profile

    def set_kill_switch(self, scope: ProfileScope, enabled: bool) -> Profile:
        """Set the per-profile kill switch."""
        profile = self.require(scope)
        profile.kill_switch = enabled
        profile.updated_at = utcnow()
        self._session.flush()
        return profile

    def update_from_definition(self, scope: ProfileScope, definition: ProfileDefinition) -> Profile:
        """Refresh stored metadata and the file hash for the scoped profile."""
        profile = self.require(scope)
        profile.name = definition.name
        profile.type = definition.type.value
        profile.timezone = definition.timezone
        profile.languages = list(definition.languages)
        profile.config_hash = definition.config_hash
        profile.updated_at = utcnow()
        self._session.flush()
        return profile


class SocialAccountRepository:
    """Scoped access to social accounts."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope,
        *,
        platform: str,
        handle: str,
        credentials_encrypted: str,
        status: str = "unverified",
        label: str | None = None,
    ) -> SocialAccount:
        """Add an account to the scoped profile."""
        existing = self.find(scope, platform=platform, handle=handle)
        if existing is not None:
            raise DuplicateRecord(
                f"account {platform}/{handle} already exists for profile {scope.profile_id}"
            )
        account = SocialAccount(
            profile_id=scope.profile_id,
            platform=platform,
            handle=handle,
            label=label,
            credentials_encrypted=credentials_encrypted,
            status=status,
        )
        self._session.add(account)
        self._session.flush()
        return account

    def list(self, scope: ProfileScope) -> list[SocialAccount]:
        """Return every account of the scoped profile ordered by id."""
        accounts = self._session.scalars(
            select(SocialAccount)
            .where(SocialAccount.profile_id == scope.profile_id)
            .order_by(SocialAccount.id)
        ).all()
        return list(accounts)

    def get(self, scope: ProfileScope, account_id: int) -> SocialAccount | None:
        """Return one account only when it belongs to the scoped profile."""
        return self._session.scalar(
            select(SocialAccount).where(
                SocialAccount.id == account_id,
                SocialAccount.profile_id == scope.profile_id,
            )
        )

    def find(self, scope: ProfileScope, *, platform: str, handle: str) -> SocialAccount | None:
        """Return one account by platform and handle within the scoped profile."""
        return self._session.scalar(
            select(SocialAccount).where(
                SocialAccount.profile_id == scope.profile_id,
                SocialAccount.platform == platform,
                SocialAccount.handle == handle,
            )
        )

    def set_credentials(
        self,
        scope: ProfileScope,
        account_id: int,
        *,
        credentials_encrypted: str,
    ) -> SocialAccount | None:
        """Replace one account's encrypted credential blob."""
        account = self.get(scope, account_id)
        if account is None:
            return None
        account.credentials_encrypted = credentials_encrypted
        account.updated_at = utcnow()
        self._session.flush()
        return account

    def mark_checked(
        self,
        scope: ProfileScope,
        account_id: int,
        *,
        status: str,
        checked_at: datetime,
    ) -> SocialAccount | None:
        """Record the result of a credential check for a scoped account."""
        account = self.get(scope, account_id)
        if account is None:
            return None
        account.status = status
        account.last_checked_at = checked_at
        account.updated_at = checked_at
        self._session.flush()
        return account


class AuditRepository:
    """Append-only storage for audit entries.

    This class deliberately has no update or delete method. A scope of ``None`` means a
    global action; a scope with an id means a profile action.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def append(
        self,
        scope: ProfileScope | None,
        *,
        actor: str,
        action: str,
        entity: str,
        entity_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> AuditLog:
        """Append one audit entry."""
        entry = AuditLog(
            profile_id=None if scope is None else scope.profile_id,
            actor=actor,
            action=action,
            entity=entity,
            entity_id=entity_id,
            payload={} if payload is None else payload,
        )
        self._session.add(entry)
        self._session.flush()
        return entry

    def history(self, scope: ProfileScope | None, *, limit: int = 100) -> list[AuditLog]:
        """Return newest audit entries for a profile, or global entries when scope is None."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = select(AuditLog)
        if scope is None:
            query = query.where(AuditLog.profile_id.is_(None))
        else:
            query = query.where(AuditLog.profile_id == scope.profile_id)
        entries = self._session.scalars(query.order_by(AuditLog.id.desc()).limit(limit)).all()
        return list(entries)

    def latest(self, scope: ProfileScope | None, *, action: str) -> AuditLog | None:
        """Return the newest entry for *action* within the requested scope."""
        query = select(AuditLog).where(AuditLog.action == action)
        if scope is None:
            query = query.where(AuditLog.profile_id.is_(None))
        else:
            query = query.where(AuditLog.profile_id == scope.profile_id)
        return self._session.scalar(query.order_by(AuditLog.id.desc()).limit(1))


class AuditCatalog:
    """Read-only cross-profile audit view for the authenticated audit page.

    This is a narrow diagnostic exception, like :class:`ProfileCatalog` and
    :class:`InboxRepository`: it returns redacted audit rows only, never profile content,
    and it has no write method.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def recent(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
        action: str | None = None,
    ) -> list[AuditLog]:
        """Return newest audit entries across every profile."""
        if limit < 1 or limit > 1000:
            raise ValueError("limit must be between 1 and 1000")
        if offset < 0:
            raise ValueError("offset must not be negative")
        query = select(AuditLog)
        if action:
            query = query.where(AuditLog.action == action)
        entries = self._session.scalars(
            query.order_by(AuditLog.id.desc()).offset(offset).limit(limit)
        ).all()
        return list(entries)


class JobRepository:
    """Scoped access to the business jobs table. The scheduler arrives in M5."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope | None,
        *,
        kind: str,
        run_at: datetime,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> Job:
        """Insert a pending job."""
        job = Job(
            profile_id=None if scope is None else scope.profile_id,
            kind=kind,
            run_at=run_at,
            payload={} if payload is None else payload,
            idempotency_key=idempotency_key,
        )
        self._session.add(job)
        self._session.flush()
        return job

    def list(self, scope: ProfileScope | None) -> list[Job]:
        """Return jobs for a profile, or global jobs when scope is None."""
        query = select(Job)
        if scope is None:
            query = query.where(Job.profile_id.is_(None))
        else:
            query = query.where(Job.profile_id == scope.profile_id)
        jobs = self._session.scalars(query.order_by(Job.id)).all()
        return list(jobs)

    def get(self, scope: ProfileScope | None, job_id: int) -> Job | None:
        """Return one job only when it belongs to the requested scope."""
        query = select(Job).where(Job.id == job_id)
        if scope is None:
            query = query.where(Job.profile_id.is_(None))
        else:
            query = query.where(Job.profile_id == scope.profile_id)
        return self._session.scalar(query)


@dataclass(frozen=True, slots=True)
class RunTotals:
    """Aggregated agent run counts, tokens and cost."""

    runs: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0


def _totals_from_row(row: Any) -> RunTotals:
    return RunTotals(
        runs=int(row[0] or 0),
        tokens_in=int(row[1] or 0),
        tokens_out=int(row[2] or 0),
        cost_usd=float(row[3] or 0.0),
    )


class AgentRunRepository:
    """Scoped storage for agent run traces."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope,
        *,
        agent: str,
        status: str,
        model: str,
        input: dict[str, Any],
        started_at: datetime,
        output: dict[str, Any] | None = None,
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost_usd: float = 0.0,
        error: str | None = None,
        finished_at: datetime | None = None,
        pipeline_run_id: int | None = None,
    ) -> AgentRun:
        """Insert one run trace for the scoped profile."""
        run = AgentRun(
            profile_id=scope.profile_id,
            agent=agent,
            pipeline_run_id=pipeline_run_id,
            input=input,
            output=output,
            model=model,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost_usd,
            status=status,
            error=error,
            started_at=started_at,
            finished_at=finished_at,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def list(self, scope: ProfileScope, *, limit: int = 50) -> list[AgentRun]:
        """Return newest runs of the scoped profile first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        runs = self._session.scalars(
            select(AgentRun)
            .where(AgentRun.profile_id == scope.profile_id)
            .order_by(AgentRun.id.desc())
            .limit(limit)
        ).all()
        return list(runs)

    def totals_since(
        self, scope: ProfileScope, since: datetime, until: datetime | None = None
    ) -> RunTotals:
        """Aggregate runs of one profile from *since* (inclusive) to *until* (exclusive)."""
        query = select(
            func.count(AgentRun.id),
            func.coalesce(func.sum(AgentRun.tokens_in), 0),
            func.coalesce(func.sum(AgentRun.tokens_out), 0),
            func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
        ).where(
            AgentRun.profile_id == scope.profile_id,
            AgentRun.started_at >= since,
        )
        if until is not None:
            query = query.where(AgentRun.started_at < until)
        return _totals_from_row(self._session.execute(query).one())

    def cost_since(self, scope: ProfileScope, since: datetime) -> float:
        """Return the cost of one profile's runs since *since*."""
        return self.totals_since(scope, since).cost_usd


class GlobalCostRepository:
    """Read-only cross-profile cost aggregate for the global daily cap.

    This is the one place allowed to read agent runs without a :class:`ProfileScope`,
    because ``max_daily_cost_usd`` is enforced globally. It returns sums only, never rows
    or profile content.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def totals_since(self, since: datetime, until: datetime | None = None) -> RunTotals:
        """Aggregate every profile's runs from *since* (inclusive) to *until* (exclusive)."""
        query = select(
            func.count(AgentRun.id),
            func.coalesce(func.sum(AgentRun.tokens_in), 0),
            func.coalesce(func.sum(AgentRun.tokens_out), 0),
            func.coalesce(func.sum(AgentRun.cost_usd), 0.0),
        ).where(AgentRun.started_at >= since)
        if until is not None:
            query = query.where(AgentRun.started_at < until)
        return _totals_from_row(self._session.execute(query).one())

    def cost_since(self, since: datetime) -> float:
        """Return the total cost of every profile's runs since *since*."""
        return self.totals_since(since).cost_usd


class PipelineRunRepository:
    """Scoped storage for pipeline executions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope,
        *,
        pipeline: str,
        trigger: str,
        started_at: datetime,
        status: str = "running",
    ) -> PipelineRun:
        """Insert one pipeline run for the scoped profile."""
        run = PipelineRun(
            profile_id=scope.profile_id,
            pipeline=pipeline,
            trigger=trigger,
            status=status,
            started_at=started_at,
        )
        self._session.add(run)
        self._session.flush()
        return run

    def get(self, scope: ProfileScope, run_id: int) -> PipelineRun | None:
        """Return one pipeline run only when it belongs to the scoped profile."""
        return self._session.scalar(
            select(PipelineRun).where(
                PipelineRun.id == run_id,
                PipelineRun.profile_id == scope.profile_id,
            )
        )

    def finish(
        self,
        scope: ProfileScope,
        run_id: int,
        *,
        status: str,
        finished_at: datetime,
        error: str | None = None,
    ) -> PipelineRun | None:
        """Mark a scoped pipeline run finished."""
        run = self.get(scope, run_id)
        if run is None:
            return None
        run.status = status
        run.finished_at = finished_at
        run.error = error
        self._session.flush()
        return run


class ContentIdeaRepository:
    """Scoped access to researched content ideas."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope,
        *,
        source: str,
        title: str,
        angle: str,
        evidence: list[str],
        status: str = "proposed",
        created_at: datetime | None = None,
    ) -> ContentIdea:
        """Insert one idea for the scoped profile."""
        idea = ContentIdea(
            profile_id=scope.profile_id,
            source=source,
            title=title,
            angle=angle,
            evidence=list(evidence),
            status=status,
        )
        if created_at is not None:
            idea.created_at = created_at
        self._session.add(idea)
        self._session.flush()
        return idea

    def get(self, scope: ProfileScope, idea_id: int) -> ContentIdea | None:
        """Return one idea only when it belongs to the scoped profile."""
        return self._session.scalar(
            select(ContentIdea).where(
                ContentIdea.id == idea_id,
                ContentIdea.profile_id == scope.profile_id,
            )
        )

    def list(
        self,
        scope: ProfileScope,
        *,
        since: datetime | None = None,
        limit: int = 200,
    ) -> list[ContentIdea]:
        """Return ideas of the scoped profile, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = select(ContentIdea).where(ContentIdea.profile_id == scope.profile_id)
        if since is not None:
            query = query.where(ContentIdea.created_at >= since)
        ideas = self._session.scalars(query.order_by(ContentIdea.id.desc()).limit(limit)).all()
        return list(ideas)

    def mark_status(self, scope: ProfileScope, idea_id: int, status: str) -> ContentIdea | None:
        """Change the status of one scoped idea."""
        idea = self.get(scope, idea_id)
        if idea is None:
            return None
        idea.status = status
        self._session.flush()
        return idea


class ContentItemRepository:
    """Scoped access to content items.

    State changes deliberately have no repository method: callers must use
    :func:`heraldesk.domain.content.transition`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope,
        *,
        platform: str,
        body: str,
        idea_id: int | None = None,
        account_id: int | None = None,
        media: dict[str, Any] | None = None,
        risk_level: str | None = None,
        risk_notes: str | None = None,
        created_by_run_id: int | None = None,
        state: str = "drafted",
        created_at: datetime | None = None,
    ) -> ContentItem:
        """Insert one content item for the scoped profile."""
        item = ContentItem(
            profile_id=scope.profile_id,
            account_id=account_id,
            idea_id=idea_id,
            platform=platform,
            body=body,
            media={} if media is None else media,
            state=state,
            risk_level=risk_level,
            risk_notes=risk_notes,
            created_by_run_id=created_by_run_id,
        )
        if created_at is not None:
            item.created_at = created_at
        self._session.add(item)
        self._session.flush()
        return item

    def get(self, scope: ProfileScope, item_id: int) -> ContentItem | None:
        """Return one content item only when it belongs to the scoped profile."""
        return self._session.scalar(
            select(ContentItem).where(
                ContentItem.id == item_id,
                ContentItem.profile_id == scope.profile_id,
            )
        )

    def list(
        self,
        scope: ProfileScope,
        *,
        since: datetime | None = None,
        states: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[ContentItem]:
        """Return content items of the scoped profile, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = select(ContentItem).where(ContentItem.profile_id == scope.profile_id)
        if since is not None:
            query = query.where(ContentItem.created_at >= since)
        if states is not None:
            query = query.where(ContentItem.state.in_(states))
        items = self._session.scalars(query.order_by(ContentItem.id.desc()).limit(limit)).all()
        return list(items)

    def list_between(
        self,
        scope: ProfileScope,
        *,
        start: datetime,
        end: datetime,
        limit: int = 10_000,
    ) -> builtins.list[ContentItem]:
        """Return scoped content items created in ``[start, end)``, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        items = self._session.scalars(
            select(ContentItem)
            .where(
                ContentItem.profile_id == scope.profile_id,
                ContentItem.created_at >= start,
                ContentItem.created_at < end,
            )
            .order_by(ContentItem.created_at.desc(), ContentItem.id.desc())
            .limit(limit)
        ).all()
        return list(items)

    def state_counts(self, scope: ProfileScope) -> dict[str, int]:
        """Return how many scoped content items exist per state."""
        rows = self._session.execute(
            select(ContentItem.state, func.count(ContentItem.id))
            .where(ContentItem.profile_id == scope.profile_id)
            .group_by(ContentItem.state)
        ).all()
        return {str(state): int(count) for state, count in rows}

    def update_draft(
        self,
        scope: ProfileScope,
        item_id: int,
        *,
        body: str,
        media: dict[str, Any] | None = None,
    ) -> ContentItem | None:
        """Replace the body and media of a scoped draft item."""
        item = self.get(scope, item_id)
        if item is None:
            return None
        item.body = body
        if media is not None:
            item.media = media
        item.updated_at = utcnow()
        self._session.flush()
        return item

    def set_risk(
        self,
        scope: ProfileScope,
        item_id: int,
        *,
        risk_level: str | None,
        risk_notes: str | None,
    ) -> ContentItem | None:
        """Store the Compliance result on a scoped item."""
        item = self.get(scope, item_id)
        if item is None:
            return None
        item.risk_level = risk_level
        item.risk_notes = risk_notes
        item.updated_at = utcnow()
        self._session.flush()
        return item

    def set_publish_policy(
        self,
        scope: ProfileScope,
        item_id: int,
        *,
        mode: str | None,
        delay_minutes: int | None,
    ) -> ContentItem | None:
        """Store the autonomy decision that governs how this item is published."""
        item = self.get(scope, item_id)
        if item is None:
            return None
        item.publish_mode = mode
        item.publish_delay_minutes = delay_minutes
        item.updated_at = utcnow()
        self._session.flush()
        return item

    def set_scheduled_at(
        self,
        scope: ProfileScope,
        item_id: int,
        *,
        scheduled_at: datetime | None,
    ) -> ContentItem | None:
        """Set the due time used by the scheduler, including retry backoff."""
        item = self.get(scope, item_id)
        if item is None:
            return None
        item.scheduled_at = scheduled_at
        item.updated_at = utcnow()
        self._session.flush()
        return item

    def record_publish_attempt(
        self,
        scope: ProfileScope,
        item_id: int,
        *,
        attempts: int,
        error: str | None,
    ) -> ContentItem | None:
        """Store the durable attempt count and the redacted provider error."""
        item = self.get(scope, item_id)
        if item is None:
            return None
        item.publish_attempts = attempts
        item.last_error = error
        item.updated_at = utcnow()
        self._session.flush()
        return item

    def mark_published(
        self,
        scope: ProfileScope,
        item_id: int,
        *,
        external_id: str,
        published_at: datetime,
    ) -> ContentItem | None:
        """Store the platform id and publication time of a successful post."""
        item = self.get(scope, item_id)
        if item is None:
            return None
        item.external_id = external_id
        item.published_at = published_at
        item.last_error = None
        item.updated_at = utcnow()
        self._session.flush()
        return item

    def count_published_between(
        self,
        scope: ProfileScope,
        *,
        start: datetime,
        end: datetime,
    ) -> int:
        """Count published items with ``published_at`` in ``[start, end)``."""
        query = select(func.count(ContentItem.id)).where(
            ContentItem.profile_id == scope.profile_id,
            ContentItem.state == "published",
            ContentItem.published_at.is_not(None),
            ContentItem.published_at >= start,
            ContentItem.published_at < end,
        )
        return int(self._session.scalar(query) or 0)

    def due_for_publish(
        self,
        scope: ProfileScope,
        *,
        now: datetime,
        max_attempts: int,
    ) -> builtins.list[ContentItem]:
        """Return scoped items the scheduler may publish or retry at *now*.

        A due item is either ``scheduled``, or ``failed`` with attempts left and a retry
        time that has arrived. Cancelled, future and exhausted items never appear.
        """
        query = (
            select(ContentItem)
            .where(
                ContentItem.profile_id == scope.profile_id,
                or_(
                    and_(
                        ContentItem.state == "scheduled",
                        ContentItem.scheduled_at.is_not(None),
                        ContentItem.scheduled_at <= now,
                    ),
                    and_(
                        ContentItem.state == "failed",
                        ContentItem.publish_attempts < max_attempts,
                        ContentItem.scheduled_at.is_not(None),
                        ContentItem.scheduled_at <= now,
                    ),
                ),
            )
            .order_by(ContentItem.scheduled_at, ContentItem.id)
        )
        return list(self._session.scalars(query).all())


class ContentItemLocator:
    """Resolve a globally unique content item id to its profile scope.

    This is the one unscoped content query. It returns only the owning profile id, never
    content, so callers must still use scoped repositories for every read and write. The
    CLI uses it for ``queue approve|reject|edit <id>``, which deliberately has no profile
    argument because content item ids are globally unique.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def scope_for_content_item(self, item_id: int) -> ProfileScope | None:
        """Return the scope that owns *item_id*, or ``None`` when it does not exist."""
        profile_id = self._session.scalar(
            select(ContentItem.profile_id).where(ContentItem.id == item_id)
        )
        if profile_id is None:
            return None
        return ProfileScope(profile_id)


class ApprovalLocator:
    """Resolve a globally unique approval id to its scope and content item.

    This is the narrow unscoped approval read used by the authenticated Web/API layer,
    which receives approval ids in its URLs. It returns ids only; every state change still
    goes through :class:`ApprovalRepository` and the scoped domain service.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def resolve(self, approval_id: int) -> tuple[ProfileScope, int] | None:
        """Return ``(scope, content_item_id)`` or ``None`` when the approval is missing."""
        row = self._session.execute(
            select(Approval.profile_id, Approval.content_item_id).where(Approval.id == approval_id)
        ).first()
        if row is None:
            return None
        profile_id, content_item_id = row
        return ProfileScope(int(profile_id)), int(content_item_id)


class ApprovalRepository:
    """Scoped storage for approval requests and decisions."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope,
        *,
        content_item_id: int,
        expires_at: datetime,
        channel: str = "cli",
        edited_body: str | None = None,
        created_at: datetime | None = None,
    ) -> Approval:
        """Insert one pending approval for a scoped content item."""
        item = ContentItemRepository(self._session).get(scope, content_item_id)
        if item is None:
            raise RecordNotFound(
                f"content item {content_item_id} does not exist for profile {scope.profile_id}"
            )
        if self.pending_for_item(scope, content_item_id) is not None:
            raise DuplicateRecord(f"content item {content_item_id} already has a pending approval")
        approval = Approval(
            profile_id=scope.profile_id,
            content_item_id=content_item_id,
            state="pending",
            channel=channel,
            edited_body=edited_body,
            expires_at=expires_at,
        )
        if created_at is not None:
            approval.created_at = created_at
        self._session.add(approval)
        self._session.flush()
        return approval

    def get(self, scope: ProfileScope, approval_id: int) -> Approval | None:
        """Return one approval only when it belongs to the scoped profile."""
        return self._session.scalar(
            select(Approval).where(
                Approval.id == approval_id,
                Approval.profile_id == scope.profile_id,
            )
        )

    def pending_for_item(self, scope: ProfileScope, content_item_id: int) -> Approval | None:
        """Return the open approval for a scoped content item, if any."""
        return self._session.scalar(
            select(Approval).where(
                Approval.profile_id == scope.profile_id,
                Approval.content_item_id == content_item_id,
                Approval.state == "pending",
            )
        )

    def latest_for_item(self, scope: ProfileScope, content_item_id: int) -> Approval | None:
        """Return the newest approval for a scoped content item, whatever its state."""
        return self._session.scalar(
            select(Approval)
            .where(
                Approval.profile_id == scope.profile_id,
                Approval.content_item_id == content_item_id,
            )
            .order_by(Approval.id.desc())
        )

    def list(
        self,
        scope: ProfileScope,
        *,
        states: tuple[str, ...] | None = None,
        limit: int = 200,
    ) -> list[Approval]:
        """Return approvals of the scoped profile, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = select(Approval).where(Approval.profile_id == scope.profile_id)
        if states is not None:
            query = query.where(Approval.state.in_(states))
        approvals = self._session.scalars(query.order_by(Approval.id.desc()).limit(limit)).all()
        return list(approvals)

    def stale_pending(self, scope: ProfileScope, *, before: datetime) -> builtins.list[Approval]:
        """Return pending approvals whose expiry is older than *before*."""
        approvals = self._session.scalars(
            select(Approval)
            .where(
                Approval.profile_id == scope.profile_id,
                Approval.state == "pending",
                Approval.expires_at < before,
            )
            .order_by(Approval.id)
        ).all()
        return builtins.list(approvals)

    def update_decision(
        self,
        scope: ProfileScope,
        approval_id: int,
        *,
        state: str,
        decision: str | None,
        reason: str | None,
        edited_body: str | None,
        decided_at: datetime,
        channel: str,
    ) -> Approval | None:
        """Store a final decision or expiry for one scoped approval."""
        approval = self.get(scope, approval_id)
        if approval is None:
            return None
        approval.state = state
        approval.decision = decision
        approval.reason = reason
        if edited_body is not None:
            approval.edited_body = edited_body
        approval.decided_at = decided_at
        approval.channel = channel
        approval.updated_at = utcnow()
        self._session.flush()
        return approval

    def touch_pending(
        self,
        scope: ProfileScope,
        approval_id: int,
        *,
        edited_body: str,
        expires_at: datetime,
    ) -> Approval | None:
        """Record an edit on a pending approval and restart its expiry window."""
        approval = self.get(scope, approval_id)
        if approval is None:
            return None
        if approval.state != "pending":
            return None
        approval.edited_body = edited_body
        approval.expires_at = expires_at
        approval.updated_at = utcnow()
        self._session.flush()
        return approval


class TelegramUpdateLocator:
    """Resolve whether a Telegram ``update_id`` was already processed.

    Telegram update ids are globally unique. This is the one unscoped Telegram query; it
    returns only a boolean and never message text, chat ids or profile data. Callers still
    store messages through the scoped :class:`TelegramMessageRepository`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def exists(self, update_id: int) -> bool:
        """Return ``True`` when *update_id* is already recorded."""
        found = self._session.scalar(
            select(TelegramMessage.id).where(TelegramMessage.update_id == update_id).limit(1)
        )
        return found is not None


class TelegramMessageRepository:
    """Scoped storage for inbound Telegram messages.

    ``scope=None`` is used only before a message is linked to a profile. Once a command
    resolves a profile, the row is written with that scope.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def add(
        self,
        scope: ProfileScope | None,
        *,
        chat_id: int,
        sender_id: int | None,
        text: str,
        command: str | None,
        content_item_id: int | None,
        response: str | None,
        created_at: datetime | None = None,
        update_id: int | None = None,
    ) -> TelegramMessage:
        """Store one already-redacted inbound message."""
        message = TelegramMessage(
            profile_id=None if scope is None else scope.profile_id,
            update_id=update_id,
            chat_id=chat_id,
            sender_id=sender_id,
            text=text,
            command=command,
            content_item_id=content_item_id,
            response=response,
            created_at=utcnow() if created_at is None else created_at,
        )
        self._session.add(message)
        self._session.flush()
        return message

    def list(self, scope: ProfileScope, *, limit: int = 20) -> list[TelegramMessage]:
        """Return the newest messages of one profile, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        messages = self._session.scalars(
            select(TelegramMessage)
            .where(TelegramMessage.profile_id == scope.profile_id)
            .order_by(TelegramMessage.id.desc())
            .limit(limit)
        ).all()
        return list(messages)


class InboxRepository:
    """Cross-profile manual-triage view over ``telegram_messages``.

    The M8 ``heraldesk inbox`` commands intentionally have no profile argument, so this is
    a documented narrow exception to the scoped-repository rule, like
    :class:`GlobalCostRepository`. It touches only ``telegram_messages`` and returns only
    inbox rows; every other profile-owned table still goes through :class:`ProfileScope`.
    """

    def __init__(self, session: Session) -> None:
        self._session = session

    def list(
        self,
        *,
        status: str,
        limit: int = 20,
        offset: int = 0,
    ) -> list[TelegramMessage]:
        """Return newest messages with *status* across every profile, newest first."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must not be negative")
        messages = self._session.scalars(
            select(TelegramMessage)
            .where(TelegramMessage.status == status)
            .order_by(TelegramMessage.id.desc())
            .offset(offset)
            .limit(limit)
        ).all()
        return list(messages)

    def get(self, item_id: int) -> TelegramMessage | None:
        """Return one inbox message by id, regardless of its profile link."""
        return self._session.scalar(select(TelegramMessage).where(TelegramMessage.id == item_id))

    def status_counts(self, *, since: datetime, until: datetime) -> dict[str, int]:
        """Count inbox messages created in ``[since, until)`` grouped by triage status."""
        rows = self._session.execute(
            select(TelegramMessage.status, func.count(TelegramMessage.id))
            .where(
                TelegramMessage.created_at >= since,
                TelegramMessage.created_at < until,
            )
            .group_by(TelegramMessage.status)
        ).all()
        counts = {"pending": 0, "handled": 0, "ignored": 0}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    def count_by_status(self) -> dict[str, int]:
        """Count every inbox message by triage status, with zeros for empty statuses."""
        rows = self._session.execute(
            select(TelegramMessage.status, func.count(TelegramMessage.id)).group_by(
                TelegramMessage.status
            )
        ).all()
        counts = {"pending": 0, "handled": 0, "ignored": 0}
        for status, count in rows:
            counts[str(status)] = int(count)
        return counts

    def count_older_than(
        self,
        cutoff: datetime,
        *,
        include_pending: bool = False,
    ) -> int:
        """Count inbox rows older than *cutoff* eligible for retention."""
        query = select(func.count(TelegramMessage.id)).where(TelegramMessage.created_at < cutoff)
        if not include_pending:
            query = query.where(TelegramMessage.status.in_(("handled", "ignored")))
        return int(self._session.scalar(query) or 0)

    def delete_older_than(
        self,
        cutoff: datetime,
        *,
        include_pending: bool = False,
    ) -> int:
        """Delete old inbox rows and return the number removed."""
        query = delete(TelegramMessage).where(TelegramMessage.created_at < cutoff)
        if not include_pending:
            query = query.where(TelegramMessage.status.in_(("handled", "ignored")))
        result = self._session.execute(query.execution_options(synchronize_session=False))
        self._session.flush()
        rowcount = getattr(result, "rowcount", 0)
        return int(rowcount or 0)

    def mark_handled(
        self,
        item_id: int,
        *,
        response: str,
        triaged_at: datetime,
    ) -> TelegramMessage | None:
        """Mark one inbox message handled and store its redacted outbound reply."""
        message = self.get(item_id)
        if message is None:
            return None
        message.status = "handled"
        message.response = response
        message.triaged_at = triaged_at
        self._session.flush()
        return message

    def mark_ignored(
        self,
        item_id: int,
        *,
        reason: str,
        triaged_at: datetime,
    ) -> TelegramMessage | None:
        """Mark one inbox message ignored with a reason."""
        message = self.get(item_id)
        if message is None:
            return None
        message.status = "ignored"
        message.ignore_reason = reason
        message.triaged_at = triaged_at
        self._session.flush()
        return message


class TelegramNotificationRepository:
    """Scoped storage for the idempotent outbound notification ledger."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def find(
        self,
        scope: ProfileScope | None,
        idempotency_key: str,
    ) -> TelegramNotification | None:
        """Return the notification for *idempotency_key* within the requested scope."""
        query = select(TelegramNotification).where(
            TelegramNotification.idempotency_key == idempotency_key
        )
        if scope is None:
            query = query.where(TelegramNotification.profile_id.is_(None))
        else:
            query = query.where(TelegramNotification.profile_id == scope.profile_id)
        return self._session.scalar(query)

    def add(
        self,
        scope: ProfileScope | None,
        *,
        event: str,
        idempotency_key: str,
        status: str,
        message_preview: str | None,
        recipients: list[str],
        error: str | None,
        created_at: datetime | None = None,
        sent_at: datetime | None = None,
    ) -> TelegramNotification:
        """Insert one notification ledger row."""
        notification = TelegramNotification(
            profile_id=None if scope is None else scope.profile_id,
            event=event,
            idempotency_key=idempotency_key,
            status=status,
            message_preview=message_preview,
            recipients=list(recipients),
            error=error,
            created_at=utcnow() if created_at is None else created_at,
            sent_at=sent_at,
        )
        self._session.add(notification)
        self._session.flush()
        return notification

    def list(
        self,
        scope: ProfileScope | None,
        *,
        limit: int = 50,
    ) -> list[TelegramNotification]:
        """Return newest notification rows for a profile, or global rows for ``None``."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        query = select(TelegramNotification)
        if scope is None:
            query = query.where(TelegramNotification.profile_id.is_(None))
        else:
            query = query.where(TelegramNotification.profile_id == scope.profile_id)
        rows = self._session.scalars(
            query.order_by(TelegramNotification.id.desc()).limit(limit)
        ).all()
        return list(rows)
