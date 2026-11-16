"""The only place that issues SQLAlchemy queries for domain data.

Scoped repositories take :class:`ProfileScope` as their first argument and add
``profile_id == scope.profile_id`` to every query. :class:`ProfileCatalog` is a narrow
global directory used to bootstrap an id and to list profile metadata; it never exposes
profile file content.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from heraldesk.db.models import (
    AgentRun,
    AuditLog,
    ContentIdea,
    ContentItem,
    Job,
    PipelineRun,
    Profile,
    SocialAccount,
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
