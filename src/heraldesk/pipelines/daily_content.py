"""The ``daily_content`` pipeline.

Flow is deterministic: Strategist slots, Researcher ideas, a 60-day duplicate check,
Writer drafts per slot/platform, an optional deterministic visual placeholder, lint,
Compliance, then the M4 autonomy engine.

``draft_only`` and ``require_approval`` create a pending approval. ``auto``,
``auto_after_delay`` and ``auto_publish`` move the item through ``approved`` to
``scheduled``; the scheduler publishes it when due. ``auto_after_delay`` keeps the
cancellation window recorded in ``publish_delay_minutes``.

``dry_run=True`` performs the whole simulation but writes nothing: no pipeline run, idea,
content item, approval, agent run or audit entry. Only the returned result is printed.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.agents.base import AgentContext
from heraldesk.agents.compliance import Compliance
from heraldesk.agents.errors import AgentRunError
from heraldesk.agents.researcher import Researcher
from heraldesk.agents.runner import AgentRunner
from heraldesk.agents.schemas import (
    ComplianceInput,
    ContentPlan,
    Draft,
    Idea,
    ResearcherInput,
    ResearchOutput,
    RiskAssessment,
    StrategistInput,
    WriterInput,
)
from heraldesk.agents.strategist import Strategist
from heraldesk.agents.writer import Writer
from heraldesk.core.audit import AuditService
from heraldesk.core.locks import is_global_kill_switch_enabled
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.repositories import (
    ContentIdeaRepository,
    ContentItemRepository,
    PipelineRunRepository,
    ProfileRepository,
    SocialAccountRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import request_approval
from heraldesk.domain.autonomy import AutonomyDecision, decide
from heraldesk.domain.content import ContentState, can_transition, transition
from heraldesk.domain.policy import PolicyAction, PolicyMode
from heraldesk.domain.profiles import ProfileDefinition
from heraldesk.llm.types import LLMProvider
from heraldesk.platforms.base import PlatformAdapter, constraints_for
from heraldesk.quality import lint
from heraldesk.telegram.notifier import NotificationService
from heraldesk.tools.web_search import SearchError, WebSearch

PIPELINE_NAME: Final = "daily_content"
DEDUPE_WINDOW_DAYS: Final = 60
SIMILARITY_THRESHOLD: Final = 0.8
MAX_DRAFT_LLM_CALLS: Final = 4
MAX_LINT_FAILURES: Final = 2
MAX_AUDIT_TEXT: Final = 500


class DailyContentError(Exception):
    """Raised when the pipeline cannot complete. Persistence is decided by the caller."""


@dataclass(frozen=True, slots=True)
class DraftSummary:
    """One draft as shown by the CLI or returned by the pipeline."""

    item_id: int | None
    platform: str
    state: str
    risk_level: str | None
    body: str
    reason: str | None = None
    approval_id: int | None = None


@dataclass(frozen=True, slots=True)
class DailyContentResult:
    """Outcome of one ``daily_content`` run."""

    pipeline_run_id: int | None
    status: str
    dry_run: bool
    items: tuple[DraftSummary, ...] = field(default_factory=tuple)
    skipped_ideas: int = 0
    total_cost_usd: float = 0.0
    notes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(slots=True)
class _WorkingDraft:
    item_id: int | None
    platform: str
    account_id: int | None
    body: str
    media: dict[str, str]
    state: ContentState
    risk_level: str | None = None
    risk_notes: str | None = None
    reason: str | None = None
    approval_id: int | None = None

    def summary(self) -> DraftSummary:
        return DraftSummary(
            item_id=self.item_id,
            platform=self.platform,
            state=self.state.value,
            risk_level=self.risk_level,
            body=self.body,
            reason=self.reason,
            approval_id=self.approval_id,
        )


def normalize_text(text: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for similarity checks."""
    cleaned = re.sub(r"[^\w\s]", " ", text.casefold())
    return " ".join(cleaned.split())


def is_similar_text(candidate: str, existing: str) -> bool:
    """Return ``True`` for near-duplicate normalized text."""
    if not candidate or not existing:
        return False
    if candidate == existing:
        return True
    if (
        len(candidate) >= 20
        and len(existing) >= 20
        and (candidate in existing or existing in candidate)
    ):
        return True
    return difflib.SequenceMatcher(None, candidate, existing).ratio() >= SIMILARITY_THRESHOLD


def is_similar_idea(title: str, angle: str, existing_texts: list[str]) -> bool:
    """Compare one idea's title plus angle with previously used text fingerprints."""
    candidate = normalize_text(f"{title} {angle}")
    return any(is_similar_text(candidate, existing) for existing in existing_texts)


def designer_placeholder(title: str, platform: str) -> dict[str, str]:
    """Return a deterministic visual brief without calling an LLM.

    M3 does not generate images. The placeholder gives the approval queue and the future
    Designer enough context to request or replace one.
    """
    return {
        "visual_brief": f"A simple, factual image illustrating: {title}",
        "alt_text": f"Visual for a {platform} post about {title}",
        "source": "designer_placeholder_m3",
    }


class _DailyContentRunner:
    def __init__(
        self,
        session: Session,
        layout: paths.Layout,
        scope: ProfileScope,
        definition: ProfileDefinition,
        provider: LLMProvider,
        search: WebSearch,
        *,
        model: str,
        cap_usd: float,
        dry_run: bool,
        now: datetime,
        platforms: tuple[str, ...],
        account_by_platform: dict[str, int],
        redactor: SecretRegistry,
        notifier: NotificationService | None,
    ) -> None:
        self._session = session
        self._layout = layout
        self._scope = scope
        self._definition = definition
        self._provider = provider
        self._search = search
        self._model = model
        self._cap_usd = cap_usd
        self._dry_run = dry_run
        self._now = now
        self._platforms = platforms
        self._account_by_platform = account_by_platform
        self._redactor = redactor
        self._notifier = notifier
        self._ideas = ContentIdeaRepository(session)
        self._items = ContentItemRepository(session)
        self._pipeline_runs = PipelineRunRepository(session)
        self._audit_service = AuditService(session)
        self._pipeline_run_id: int | None = None
        self._recent_texts: list[str] = []
        self._total_cost = 0.0
        self._skipped = 0
        self._notes: list[str] = []

    def run(self) -> DailyContentResult:
        """Validate preconditions, execute the steps and return a result."""
        profile = ProfileRepository(self._session).require(self._scope)
        if is_global_kill_switch_enabled(self._layout):
            raise DailyContentError(
                "the global kill switch is engaged; clear it with stop-all --off"
            )
        if profile.kill_switch:
            raise DailyContentError(
                f"profile '{profile.slug}' has its kill switch engaged; clear it first"
            )
        if profile.status == "paused":
            raise DailyContentError(f"profile '{profile.slug}' is paused; resume it first")

        if not self._dry_run:
            run = self._pipeline_runs.add(
                self._scope,
                pipeline=PIPELINE_NAME,
                trigger="cli",
                started_at=self._now,
            )
            self._pipeline_run_id = run.id
            self._audit(
                "pipeline.started",
                entity="pipeline_run",
                entity_id=run.id,
                payload={"pipeline": PIPELINE_NAME, "trigger": "cli"},
            )

        try:
            items = self._execute()
        except Exception as exc:
            message = self._redactor.redact(str(exc))[:MAX_AUDIT_TEXT]
            if not self._dry_run and self._pipeline_run_id is not None:
                self._pipeline_runs.finish(
                    self._scope,
                    self._pipeline_run_id,
                    status="failed",
                    finished_at=self._now,
                    error=message,
                )
                self._audit(
                    "pipeline.failed",
                    entity="pipeline_run",
                    entity_id=self._pipeline_run_id,
                    payload={"pipeline": PIPELINE_NAME, "error": message},
                )
                self._session.commit()
            raise

        if not self._dry_run and self._pipeline_run_id is not None:
            self._pipeline_runs.finish(
                self._scope,
                self._pipeline_run_id,
                status="succeeded",
                finished_at=self._now,
            )
            self._audit(
                "pipeline.succeeded",
                entity="pipeline_run",
                entity_id=self._pipeline_run_id,
                payload={
                    "pipeline": PIPELINE_NAME,
                    "items": len(items),
                    "skipped_ideas": self._skipped,
                },
            )

        return DailyContentResult(
            pipeline_run_id=self._pipeline_run_id,
            status="succeeded",
            dry_run=self._dry_run,
            items=tuple(item.summary() for item in items),
            skipped_ideas=self._skipped,
            total_cost_usd=round(self._total_cost, 6),
            notes=tuple(self._notes),
        )

    def _execute(self) -> list[_WorkingDraft]:
        self._recent_texts = self._load_recent_texts()
        plan = self._strategy()
        created: list[_WorkingDraft] = []
        for slot in plan.slots:
            platform = slot.platform if slot.platform in self._platforms else self._platforms[0]
            account_id = self._account_by_platform.get(platform)
            evidence = self._research_evidence(platform, slot.pillar, slot.goal, slot.angle)
            ideas = self._research(platform, slot.pillar, slot.goal, slot.angle, evidence)
            for idea in ideas:
                title = self._redactor.redact(idea.title)
                angle = self._redactor.redact(idea.angle)
                if is_similar_idea(title, angle, self._recent_texts):
                    self._skipped += 1
                    self._audit(
                        "content.idea_skipped",
                        entity="content_idea",
                        payload={"reason": "similar_to_last_60_days"},
                    )
                    continue
                self._recent_texts.append(normalize_text(f"{title} {angle}"))
                idea_id = self._store_idea(idea, title, angle)
                created.append(
                    self._write_and_assess(
                        platform=platform,
                        account_id=account_id,
                        idea=idea,
                        idea_id=idea_id,
                        title=title,
                        angle=angle,
                    )
                )
        return created

    def _strategy(self) -> ContentPlan:
        runner = self._runner(max_output_retries=2)
        try:
            result = runner.run(
                Strategist(),
                AgentContext(
                    scope=self._scope,
                    profile=self._definition,
                    platform=self._platforms[0],
                    constraints=constraints_for(self._platforms[0]),
                ),
                StrategistInput(
                    date=self._now.date().isoformat(),
                    platforms=list(self._platforms),
                    slots=max(1, len(self._platforms)),
                ),
            )
        except AgentRunError as exc:
            raise DailyContentError(f"strategist failed: {exc}") from exc
        self._total_cost += result.cost_usd
        output = result.output
        if not isinstance(output, ContentPlan):
            raise DailyContentError("strategist returned an unexpected output")
        return output

    def _research_evidence(
        self,
        platform: str,
        pillar: str,
        goal: str,
        angle: str,
    ) -> list[str]:
        _ = platform
        query = " ".join(
            part
            for part in (self._definition.name, pillar, goal, angle, self._now.date().isoformat())
            if part
        )
        try:
            results = self._search.search(query)
        except SearchError as exc:
            self._notes.append(f"search unavailable: {type(exc).__name__}")
            self._audit(
                "research.search_failed",
                entity="pipeline_run",
                entity_id=self._pipeline_run_id,
                payload={"error": type(exc).__name__},
            )
            return []
        return [
            self._redactor.redact(f"{result.title} | {result.url} | {result.snippet}")
            for result in results
        ]

    def _research(
        self,
        platform: str,
        pillar: str,
        goal: str,
        angle: str,
        evidence: list[str],
    ) -> list[Idea]:
        runner = self._runner(max_output_retries=2)
        try:
            result = runner.run(
                Researcher(),
                AgentContext(
                    scope=self._scope,
                    profile=self._definition,
                    platform=platform,
                    constraints=constraints_for(platform),
                ),
                ResearcherInput(
                    date=self._now.date().isoformat(),
                    platform=platform,
                    pillar=pillar,
                    goal=goal,
                    angle=angle,
                    evidence=evidence,
                ),
            )
        except AgentRunError as exc:
            raise DailyContentError(f"researcher failed: {exc}") from exc
        self._total_cost += result.cost_usd
        output = result.output
        if not isinstance(output, ResearchOutput):
            raise DailyContentError("researcher returned an unexpected output")
        return list(output.ideas)

    def _store_idea(self, idea: Idea, title: str, angle: str) -> int | None:
        if self._dry_run:
            return None
        evidence = [self._redactor.redact(item) for item in idea.evidence]
        stored = self._ideas.add(
            self._scope,
            source="researcher",
            title=title[:300],
            angle=angle,
            evidence=evidence,
        )
        return stored.id

    def _write_and_assess(
        self,
        *,
        platform: str,
        account_id: int | None,
        idea: Idea,
        idea_id: int | None,
        title: str,
        angle: str,
    ) -> _WorkingDraft:
        calls_used = 0
        lint_failures = 0
        feedback: list[str] = []
        working: _WorkingDraft | None = None
        evidence = [self._redactor.redact(item) for item in (*idea.evidence, *idea.sources)]

        while True:
            remaining = MAX_DRAFT_LLM_CALLS - calls_used
            if remaining <= 0:
                if working is None:
                    raise DailyContentError(
                        f"no LLM budget remained for {platform} draft preparation"
                    )
                self._set_state(
                    working,
                    ContentState.NEEDS_REVIEW,
                    "LLM call budget exhausted before lint passed",
                )
                return working

            runner = self._runner(max_output_retries=min(2, remaining - 1))
            try:
                result = runner.run(
                    Writer(),
                    AgentContext(
                        scope=self._scope,
                        profile=self._definition,
                        platform=platform,
                        constraints=constraints_for(platform),
                    ),
                    WriterInput(
                        idea=title,
                        platform=platform,
                        angle=angle,
                        evidence=evidence,
                        feedback=feedback,
                    ),
                )
            except AgentRunError as exc:
                raise DailyContentError(f"writer failed for {platform}: {exc}") from exc
            calls_used += result.attempts
            self._total_cost += result.cost_usd

            output = result.output
            if not isinstance(output, Draft):
                raise DailyContentError("writer returned an unexpected output")
            body = self._redactor.redact(output.body)
            media = designer_placeholder(title, platform)

            if working is None:
                working = _WorkingDraft(
                    item_id=self._create_item(
                        platform=platform,
                        account_id=account_id,
                        idea_id=idea_id,
                        body=body,
                        media=media,
                        created_by_run_id=result.run_id or None,
                    ),
                    platform=platform,
                    account_id=account_id,
                    body=body,
                    media=media,
                    state=ContentState.DRAFTED,
                )
            else:
                working.body = body
                working.media = media
                if not self._dry_run and working.item_id is not None:
                    self._items.update_draft(
                        self._scope,
                        working.item_id,
                        body=body,
                        media=media,
                    )

            lint_result = lint(body, self._definition)
            if lint_result.ok:
                break

            lint_failures += 1
            self._audit(
                "content.lint_failed",
                entity="content_item",
                entity_id=working.item_id,
                payload={
                    "attempt": lint_failures,
                    "rules": sorted({issue.rule for issue in lint_result.issues}),
                },
            )
            if lint_failures >= MAX_LINT_FAILURES:
                self._set_state(
                    working,
                    ContentState.NEEDS_REVIEW,
                    f"lint failed {lint_failures} times: {lint_result.summary}",
                )
                return working
            feedback = [issue.message for issue in lint_result.issues]

        self._set_state(working, ContentState.REVIEWED)

        remaining = MAX_DRAFT_LLM_CALLS - calls_used
        if remaining <= 0:
            self._block(working, "compliance unavailable: LLM call budget exhausted")
            return working

        runner = self._runner(max_output_retries=min(2, remaining - 1))
        try:
            result = runner.run(
                Compliance(),
                AgentContext(
                    scope=self._scope,
                    profile=self._definition,
                    platform=platform,
                    constraints=constraints_for(platform),
                ),
                ComplianceInput(body=working.body, platform=platform),
            )
        except AgentRunError:
            self._block(working, "compliance unavailable")
            return working
        self._total_cost += result.cost_usd

        assessment = result.output
        if not isinstance(assessment, RiskAssessment):
            self._block(working, "compliance unavailable: invalid assessment")
            return working

        notes = self._redactor.redact("; ".join((*assessment.reasons, *assessment.fixes)))[
            :MAX_AUDIT_TEXT
        ]
        working.risk_level = assessment.risk_level
        working.risk_notes = notes or None
        if not self._dry_run and working.item_id is not None:
            self._items.set_risk(
                self._scope,
                working.item_id,
                risk_level=working.risk_level,
                risk_notes=working.risk_notes,
            )

        if assessment.risk_level == "blocked":
            self._set_state(
                working,
                ContentState.BLOCKED,
                f"compliance blocked: {notes or 'no reason supplied'}",
            )
            return working

        decision = decide(
            self._definition.policy,
            action=PolicyAction.POST,
            risk_level=assessment.risk_level,
            compliance_passed=True,
        )
        delay_minutes = (
            decision.delay_minutes if decision.mode is PolicyMode.AUTO_AFTER_DELAY else None
        )
        self._set_publish_policy(working, decision.mode.value, delay_minutes)
        if decision.decision is AutonomyDecision.AUTO_APPROVE:
            self._set_state(working, ContentState.PENDING_APPROVAL, decision.reason)
            self._set_state(working, ContentState.APPROVED, decision.reason)
            schedule_at = self._now + timedelta(minutes=decision.delay_minutes)
            self._set_state(
                working,
                ContentState.SCHEDULED,
                decision.reason,
                schedule_at=schedule_at,
            )
            working.reason = decision.reason
            return working

        self._set_state(working, ContentState.PENDING_APPROVAL, decision.reason)
        if not self._dry_run and working.item_id is not None:
            approval = request_approval(
                self._session,
                self._scope,
                working.item_id,
                now=self._now,
                actor="system",
                policy_mode=decision.mode.value,
            )
            working.approval_id = approval.id
            if self._notifier is not None:
                stored = self._items.get(self._scope, working.item_id)
                if stored is not None:
                    self._notifier.notify_approval_requested(
                        self._scope,
                        slug=self._definition.slug,
                        item=stored,
                        approval=approval,
                        now=self._now,
                    )
        return working

    def _create_item(
        self,
        *,
        platform: str,
        account_id: int | None,
        idea_id: int | None,
        body: str,
        media: dict[str, str],
        created_by_run_id: int | None,
    ) -> int | None:
        if self._dry_run:
            return None
        item = self._items.add(
            self._scope,
            platform=platform,
            account_id=account_id,
            idea_id=idea_id,
            body=body,
            media=dict(media),
            created_by_run_id=created_by_run_id,
            state=ContentState.DRAFTED.value,
        )
        return item.id

    def _block(self, working: _WorkingDraft, reason: str) -> None:
        working.reason = reason
        self._set_state(working, ContentState.BLOCKED, reason)

    def _set_state(
        self,
        working: _WorkingDraft,
        target: ContentState,
        reason: str | None = None,
        *,
        schedule_at: datetime | None = None,
    ) -> None:
        if self._dry_run:
            if not can_transition(working.state, target):
                raise DailyContentError(
                    f"simulated draft cannot move from {working.state.value} to {target.value}"
                )
            working.state = target
            if reason:
                working.reason = reason
            return
        if working.item_id is None:
            raise DailyContentError("cannot change state of an unsaved content item")
        transition(
            self._session,
            self._scope,
            working.item_id,
            target,
            actor="system",
            reason=reason,
            schedule_at=schedule_at,
        )
        working.state = target
        if reason:
            working.reason = reason

    def _set_publish_policy(
        self,
        working: _WorkingDraft,
        mode: str,
        delay_minutes: int | None,
    ) -> None:
        if self._dry_run or working.item_id is None:
            return
        self._items.set_publish_policy(
            self._scope,
            working.item_id,
            mode=mode,
            delay_minutes=delay_minutes,
        )

    def _load_recent_texts(self) -> list[str]:
        since = self._now - timedelta(days=DEDUPE_WINDOW_DAYS)
        texts: list[str] = []
        for idea in self._ideas.list(self._scope, since=since, limit=1000):
            texts.append(normalize_text(f"{idea.title} {idea.angle}"))
        for item in self._items.list(self._scope, since=since, limit=1000):
            texts.append(normalize_text(item.body))
        return [text for text in texts if text]

    def _runner(self, *, max_output_retries: int) -> AgentRunner:
        return AgentRunner(
            self._session,
            self._provider,
            layout=self._layout,
            model=self._model,
            cap_usd=self._cap_usd,
            max_output_retries=max_output_retries,
            redactor=self._redactor,
            clock=lambda: self._now,
            pipeline_run_id=self._pipeline_run_id,
            persist=not self._dry_run,
        )

    def _audit(
        self,
        action: str,
        *,
        entity: str,
        entity_id: int | str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        if self._dry_run:
            return
        self._audit_service.record(
            self._scope,
            actor="system",
            action=action,
            entity=entity,
            entity_id=entity_id,
            payload={} if payload is None else payload,
        )


def run_daily_content(
    session: Session,
    layout: paths.Layout,
    scope: ProfileScope,
    definition: ProfileDefinition,
    *,
    provider: LLMProvider,
    search: WebSearch,
    model: str,
    cap_usd: float,
    dry_run: bool = False,
    now: datetime | None = None,
    platforms: tuple[str, ...] | None = None,
    adapter: PlatformAdapter | None = None,
    notifier: NotificationService | None = None,
) -> DailyContentResult:
    """Run the deterministic ``daily_content`` pipeline for one profile.

    With ``dry_run=True`` every repository write and every agent/audit trace is skipped.
    The fake platform fallback does not create a ``social_accounts`` row.
    """
    current = now if now is not None else datetime.now(UTC)
    accounts = SocialAccountRepository(session).list(scope)
    account_by_platform: dict[str, int] = {}
    for account in accounts:
        account_by_platform.setdefault(account.platform, account.id)

    if platforms is not None:
        available = tuple(platforms)
    elif account_by_platform:
        available = tuple(account_by_platform)
    elif adapter is not None:
        available = (adapter.platform,)
    else:
        available = ("fake",)
    if not available:
        raise DailyContentError("no target platform is available for this profile")

    runner = _DailyContentRunner(
        session,
        layout,
        scope,
        definition,
        provider,
        search,
        model=model,
        cap_usd=cap_usd,
        dry_run=dry_run,
        now=current,
        platforms=available,
        account_by_platform=account_by_platform,
        redactor=SecretRegistry.from_environment(),
        notifier=notifier,
    )
    return runner.run()
