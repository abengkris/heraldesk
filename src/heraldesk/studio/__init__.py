"""Draft Room service: planning conversation with no approval or publish path.

The service owns thread local state only. It never imports the publisher, scheduler or a
platform adapter, and it never changes ``content_items.state``. Draft creation and profile
patches arrive in later phases and must call the existing approval and profile services.
"""

from __future__ import annotations

import difflib
import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.agents.base import AgentContext
from heraldesk.agents.compliance import Compliance
from heraldesk.agents.editor import Editor
from heraldesk.agents.errors import AgentRunError
from heraldesk.agents.profile_editor import ProfileEditor
from heraldesk.agents.runner import AgentRunner, AgentRunResult
from heraldesk.agents.schemas import (
    ComplianceInput,
    Draft,
    EditorInput,
    EditorReply,
    ProfileEditInput,
    ProfilePatchProposal,
    RiskAssessment,
    StudioTurn,
    WriterInput,
)
from heraldesk.agents.writer import Writer
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import StudioMessage, StudioThread
from heraldesk.db.repositories import (
    ContentItemRepository,
    StudioMessageRepository,
    StudioThreadRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import as_utc, list_queue, request_approval
from heraldesk.domain.content import ContentState, transition
from heraldesk.domain.profiles import load_profile, profile_summary
from heraldesk.llm.types import LLMProvider
from heraldesk.quality.lint import lint
from heraldesk.web.profile_editor import (
    PROFILE_FILES,
    ProfileEditError,
    read_profile_files,
    save_profile_files,
)

MAX_MESSAGE_CHARS = 4000
MAX_TITLE_CHARS = 200
MAX_AUDIT_TEXT = 500
MAX_PREVIEW_CHARS = 120
HISTORY_MESSAGES = 12
QUEUE_ITEMS = 10
CALENDAR_DAYS = 7


class StudioError(Exception):
    """Base class for Draft Room failures."""


class StudioDisabledError(StudioError):
    """Raised when the Draft Room is disabled in configuration."""


class StudioNotFoundError(StudioError):
    """Raised when a thread does not belong to the scoped profile."""


class StudioLimitError(StudioError):
    """Raised when a thread reaches its message limit."""


class StudioCostError(StudioError):
    """Raised when a thread reaches its cost limit."""


class StudioRoleError(StudioError):
    """Raised when a role is not available in this phase."""


@dataclass(frozen=True, slots=True)
class StudioTurnResult:
    """One successful user turn and its reply."""

    thread: StudioThread
    user_message: StudioMessage
    assistant_message: StudioMessage
    run: AgentRunResult


@dataclass(frozen=True, slots=True)
class StudioDraftResult:
    """The outcome of turning a draft artifact into a content item."""

    item_id: int
    state: str
    risk_level: str | None
    risk_notes: str | None
    approval_id: int | None
    blocked: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class StudioPatchResult:
    """The outcome of applying a profile patch."""

    slug: str
    file: str
    old_hash: str
    new_hash: str


def _collapse(text: str, limit: int) -> str:
    value = " ".join(text.split())
    return value if len(value) <= limit else value[: limit - 3] + "..."


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class StudioService:
    """Create threads and run one Editor turn at a time.

    ``provider`` is optional so the Web UI can list threads without constructing an LLM
    provider. Sending a message requires both a provider and a model.
    """

    def __init__(
        self,
        session: Session,
        layout: paths.Layout,
        *,
        provider: LLMProvider | None = None,
        model: str = "",
        daily_cap_usd: float = 5.0,
        thread_cap_usd: float = 0.25,
        max_messages: int = 40,
        enabled: bool = True,
        redactor: SecretRegistry | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if daily_cap_usd < 0 or thread_cap_usd < 0:
            raise ValueError("cost caps must not be negative")
        if max_messages < 2:
            raise ValueError("max_messages must be at least 2")
        self._session = session
        self._layout = layout
        self._provider = provider
        self._model = model.strip()
        self._daily_cap_usd = daily_cap_usd
        self._thread_cap_usd = thread_cap_usd
        self._max_messages = max_messages
        self._enabled = enabled
        self._redactor = SecretRegistry.from_environment() if redactor is None else redactor
        self._clock = clock if clock is not None else (lambda: datetime.now(UTC))
        self._threads = StudioThreadRepository(session)
        self._messages = StudioMessageRepository(session)
        self._audit = AuditService(session)

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise StudioDisabledError("the Draft Room is disabled; set studio.enabled: true")

    def create_thread(self, scope: ProfileScope, *, title: str = "New thread") -> StudioThread:
        """Create one empty thread for the scoped profile."""
        self._require_enabled()
        clean = " ".join(title.split()).strip() or "New thread"
        if len(clean) > MAX_TITLE_CHARS:
            raise StudioError(f"thread title must be at most {MAX_TITLE_CHARS} characters")
        thread = self._threads.add(scope, title=clean, created_at=self._clock())
        self._audit.record(
            scope,
            actor="user",
            action="studio.thread_created",
            entity="studio_thread",
            entity_id=thread.id,
            payload={"thread_id": thread.id, "title": clean},
        )
        return thread

    def list_threads(
        self, scope: ProfileScope, *, limit: int = 50, include_archived: bool = True
    ) -> list[StudioThread]:
        """Return Draft Room threads of the scoped profile."""
        self._require_enabled()
        return self._threads.list_threads(scope, limit=limit, include_archived=include_archived)

    def get_thread(self, scope: ProfileScope, thread_id: int) -> StudioThread:
        """Return one scoped thread or raise :class:`StudioNotFoundError`."""
        self._require_enabled()
        thread = self._threads.get(scope, thread_id)
        if thread is None:
            raise StudioNotFoundError(f"studio thread {thread_id} does not exist")
        return thread

    def list_messages(
        self, scope: ProfileScope, thread_id: int, *, limit: int = 200
    ) -> list[StudioMessage]:
        """Return messages of one scoped thread."""
        self.get_thread(scope, thread_id)
        return self._messages.list_messages(scope, thread_id, limit=limit)

    def archive_thread(self, scope: ProfileScope, thread_id: int) -> StudioThread:
        """Archive one thread without deleting it."""
        self._require_enabled()
        thread = self._threads.archive(scope, thread_id)
        if thread is None:
            raise StudioNotFoundError(f"studio thread {thread_id} does not exist")
        self._audit.record(
            scope,
            actor="user",
            action="studio.thread_archived",
            entity="studio_thread",
            entity_id=thread.id,
            payload={"thread_id": thread.id},
        )
        return thread

    def send_message(
        self,
        scope: ProfileScope,
        thread_id: int,
        text: str,
        *,
        role: str = "editor",
        platform: str | None = None,
        constraints: Any = None,
        file: str | None = None,
    ) -> StudioTurnResult:
        """Run one role turn and store the user and assistant messages.

        ``editor`` plans, ``writer`` produces a draft artifact, ``compliance`` reviews the
        latest draft in the thread. The message limit and thread cost cap are checked
        before any provider call. The global daily cap is enforced by
        :class:`AgentRunner`.
        """
        self._require_enabled()
        if role not in ("editor", "writer", "compliance", "profile"):
            raise StudioRoleError(
                f"unknown role '{role}'; expected editor, writer, compliance or profile"
            )
        if role == "writer" and not platform:
            raise StudioError("the writer role requires a target platform")
        if role == "profile" and file not in PROFILE_FILES:
            raise StudioError(f"the profile role requires a file in {', '.join(PROFILE_FILES)}")
        if self._provider is None or not self._model:
            raise StudioError("no LLM provider is configured for the Draft Room")
        message = text.strip()
        if not message:
            raise StudioError("message must not be empty")
        if len(message) > MAX_MESSAGE_CHARS:
            raise StudioError(f"message must be at most {MAX_MESSAGE_CHARS} characters")

        thread = self.get_thread(scope, thread_id)
        if thread.status != "open":
            raise StudioError("this thread is archived; create a new one")
        if self._messages.count(scope, thread_id) >= self._max_messages:
            raise StudioLimitError(
                f"this thread has reached its {self._max_messages}-message limit"
            )
        spent = self._messages.cost(scope, thread_id)
        if spent >= self._thread_cap_usd:
            raise StudioCostError(
                f"this thread has spent ${spent:.2f} of its ${self._thread_cap_usd:.2f} cap"
            )

        safe_message = self._redactor.redact(message)
        summary = profile_summary(self._session, scope)
        definition = load_profile(self._layout.profiles / summary.slug)
        context = AgentContext(
            scope=scope,
            profile=definition,
            platform=platform,
            constraints=constraints,
        )
        runner = AgentRunner(
            self._session,
            self._provider,
            layout=self._layout,
            model=self._model,
            cap_usd=self._daily_cap_usd,
            redactor=self._redactor,
        )

        artifact_type: str | None = None
        artifact_json: dict[str, Any] | None = None
        if role == "editor":
            history = self._history(scope, thread_id)
            payload = EditorInput(
                message=safe_message,
                history=history,
                queue=self._queue_lines(scope),
                calendar=self._calendar_lines(scope),
            )
            run = runner.run(Editor(), context, payload)
            if not isinstance(run.output, EditorReply):
                raise StudioError("editor returned an unexpected output type")
            assistant_role = "editor"
            reply = self._redactor.redact(run.output.reply.strip())
        elif role == "writer":
            run = runner.run(
                Writer(),
                context,
                WriterInput(idea=safe_message, platform=platform or ""),
            )
            if not isinstance(run.output, Draft):
                raise StudioError("writer returned an unexpected output type")
            assistant_role = "writer"
            reply = self._redactor.redact(run.output.body)
            artifact_type = "draft"
            artifact_json = {**run.output.model_dump(mode="json"), "platform": platform}
        elif role == "profile":
            state = read_profile_files(self._layout, summary.slug)
            current = state.files[file or ""]
            run = runner.run(
                ProfileEditor(),
                context,
                ProfileEditInput(
                    file=cast(Any, file), current_text=current, instruction=safe_message
                ),
            )
            if not isinstance(run.output, ProfilePatchProposal):
                raise StudioError("profile editor returned an unexpected output type")
            assistant_role = "profile"
            reply = self._redactor.redact(run.output.rationale.strip())
            base_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            diff = "".join(
                difflib.unified_diff(
                    current.splitlines(keepends=True),
                    run.output.proposed_text.splitlines(keepends=True),
                    fromfile=f"{summary.slug}/{file}",
                    tofile=f"{summary.slug}/{file} (proposed)",
                )
            )
            artifact_type = "profile_patch"
            artifact_json = {
                "file": file,
                "rationale": run.output.rationale,
                "proposed_text": run.output.proposed_text,
                "base_hash": base_hash,
                "diff": diff,
                "applied": False,
            }
        else:
            draft = self._latest_draft(scope, thread_id)
            if draft is None:
                raise StudioError("no draft in this thread to review; ask the Writer first")
            run = runner.run(
                Compliance(),
                AgentContext(
                    scope=scope,
                    profile=definition,
                    platform=draft["platform"],
                    constraints=None,
                ),
                ComplianceInput(body=draft["body"], platform=draft["platform"]),
            )
            if not isinstance(run.output, RiskAssessment):
                raise StudioError("compliance returned an unexpected output type")
            assistant_role = "compliance"
            reasons = "; ".join(run.output.reasons)
            reply = f"Risk: {run.output.risk_level}."
            if reasons:
                reply += f" {reasons}"
            reply = self._redactor.redact(reply)
            artifact_type = "compliance"
            artifact_json = {
                **run.output.model_dump(mode="json"),
                "platform": draft["platform"],
            }

        now = self._clock()
        user_message = self._messages.add(
            scope,
            thread_id,
            role="user",
            content=safe_message,
            created_at=now,
        )
        assistant = self._messages.add(
            scope,
            thread_id,
            role=assistant_role,
            content=reply,
            artifact_type=artifact_type,
            artifact_json=artifact_json,
            agent_run_id=run.run_id,
            cost_usd=run.cost_usd,
            created_at=now,
        )
        if thread.title == "New thread":
            self._threads.rename(scope, thread_id, _collapse(safe_message, MAX_TITLE_CHARS))
        self._threads.touch(scope, thread_id, now)
        self._audit.record(
            scope,
            actor="user",
            action="studio.message_sent",
            entity="studio_message",
            entity_id=assistant.id,
            payload={
                "thread_id": thread_id,
                "role": assistant_role,
                "message_sha256": _hash(safe_message),
                "message_length": len(safe_message),
                "cost_usd": round(run.cost_usd, 6),
                "agent_run_id": run.run_id,
            },
        )
        return StudioTurnResult(
            thread=thread,
            user_message=user_message,
            assistant_message=assistant,
            run=run,
        )

    def create_draft(
        self,
        scope: ProfileScope,
        message_id: int,
        *,
        constraints: Any = None,
        max_text_bytes: int | None = None,
    ) -> StudioDraftResult:
        """Turn a draft artifact into a content item, running lint and Compliance.

        Platform limits are checked before any item is created. A lint or Compliance block
        creates a ``blocked`` item and never an approval; otherwise the item enters
        ``pending_approval`` through the normal approval service. This method never
        approves or schedules.
        """
        self._require_enabled()
        if self._provider is None or not self._model:
            raise StudioError("no LLM provider is configured for the Draft Room")
        message = self._messages.get(scope, message_id)
        if message is None:
            raise StudioNotFoundError(f"studio message {message_id} does not exist")
        if message.artifact_type != "draft":
            raise StudioError("only a draft artifact can be turned into a content item")
        if message.content_item_id is not None:
            raise StudioError("this draft already created a content item")

        data = message.artifact_json or {}
        body = data.get("body")
        variants = data.get("variants") or []
        hashtags = data.get("hashtags") or []
        platform = str(data.get("platform") or "")
        if not isinstance(body, str) or not body.strip():
            raise StudioError("the draft artifact has no body")
        if not platform:
            raise StudioError("the draft artifact has no platform")

        if constraints is not None:
            if len(body) > constraints.max_characters:
                raise StudioError(
                    f"draft is {len(body)} characters but {constraints.platform} allows "
                    f"{constraints.max_characters}"
                )
            if len(hashtags) > constraints.max_hashtags:
                raise StudioError(
                    f"draft has {len(hashtags)} hashtags but {constraints.platform} allows "
                    f"{constraints.max_hashtags}"
                )
        if max_text_bytes is not None and len(body.encode("utf-8")) > max_text_bytes:
            raise StudioError(f"draft is over {max_text_bytes} bytes of UTF-8 text")

        summary = profile_summary(self._session, scope)
        definition = load_profile(self._layout.profiles / summary.slug)
        items = ContentItemRepository(self._session)
        item = items.add(
            scope,
            platform=platform,
            body=body,
            media={"variants": list(variants), "hashtags": list(hashtags)},
            created_by_run_id=message.agent_run_id,
        )

        lint_result = lint(body, definition)
        if not lint_result.ok:
            reason = f"lint: {lint_result.summary}"
            transition(
                self._session, scope, item.id, ContentState.BLOCKED, actor="user", reason=reason
            )
            items.set_risk(scope, item.id, risk_level=None, risk_notes=reason)
            message.content_item_id = item.id
            self._session.flush()
            self._audit.record(
                scope,
                actor="user",
                action="studio.draft_created",
                entity="content_item",
                entity_id=item.id,
                payload={"item_id": item.id, "state": "blocked", "reason": "lint"},
            )
            return StudioDraftResult(item.id, "blocked", None, reason, None, True, reason)

        runner = AgentRunner(
            self._session,
            self._provider,
            layout=self._layout,
            model=self._model,
            cap_usd=self._daily_cap_usd,
            redactor=self._redactor,
        )
        try:
            run = runner.run(
                Compliance(),
                AgentContext(
                    scope=scope,
                    profile=definition,
                    platform=platform,
                    constraints=constraints,
                ),
                ComplianceInput(body=body, platform=platform),
            )
        except AgentRunError as exc:
            detail = self._redactor.redact(str(exc))[:MAX_AUDIT_TEXT] or "no reason recorded"
            reason = f"compliance unavailable: {detail}"
            transition(
                self._session, scope, item.id, ContentState.BLOCKED, actor="user", reason=reason
            )
            items.set_risk(scope, item.id, risk_level=None, risk_notes=reason)
            message.content_item_id = item.id
            self._session.flush()
            self._audit.record(
                scope,
                actor="user",
                action="studio.draft_created",
                entity="content_item",
                entity_id=item.id,
                payload={"item_id": item.id, "state": "blocked", "reason": "compliance"},
            )
            return StudioDraftResult(item.id, "blocked", None, reason, None, True, reason)

        assessment = run.output
        if not isinstance(assessment, RiskAssessment):
            reason = "compliance unavailable: invalid assessment"
            transition(
                self._session, scope, item.id, ContentState.BLOCKED, actor="user", reason=reason
            )
            items.set_risk(scope, item.id, risk_level=None, risk_notes=reason)
            message.content_item_id = item.id
            self._session.flush()
            self._audit.record(
                scope,
                actor="user",
                action="studio.draft_created",
                entity="content_item",
                entity_id=item.id,
                payload={"item_id": item.id, "state": "blocked", "reason": "compliance"},
            )
            return StudioDraftResult(item.id, "blocked", None, reason, None, True, reason)

        notes = self._redactor.redact("; ".join((*assessment.reasons, *assessment.fixes)))[
            :MAX_AUDIT_TEXT
        ]
        items.set_risk(scope, item.id, risk_level=assessment.risk_level, risk_notes=notes or None)
        if assessment.risk_level == "blocked":
            reason = f"compliance blocked: {notes or 'no reason supplied'}"
            transition(
                self._session, scope, item.id, ContentState.BLOCKED, actor="user", reason=reason
            )
            message.content_item_id = item.id
            self._session.flush()
            self._audit.record(
                scope,
                actor="user",
                action="studio.draft_created",
                entity="content_item",
                entity_id=item.id,
                payload={"item_id": item.id, "state": "blocked", "risk_level": "blocked"},
            )
            return StudioDraftResult(item.id, "blocked", "blocked", notes, None, True, reason)

        transition(
            self._session, scope, item.id, ContentState.REVIEWED, actor="user", reason="studio"
        )
        transition(
            self._session,
            scope,
            item.id,
            ContentState.PENDING_APPROVAL,
            actor="user",
            reason="require_approval: human approval required",
        )
        approval = request_approval(
            self._session,
            scope,
            item.id,
            actor="user",
            channel="web",
            policy_mode="studio",
        )
        message.content_item_id = item.id
        self._session.flush()
        self._audit.record(
            scope,
            actor="user",
            action="studio.draft_created",
            entity="content_item",
            entity_id=item.id,
            payload={
                "item_id": item.id,
                "approval_id": approval.id,
                "state": "pending_approval",
                "risk_level": assessment.risk_level,
            },
        )
        return StudioDraftResult(
            item.id,
            "pending_approval",
            assessment.risk_level,
            notes,
            approval.id,
            False,
            None,
        )

    def apply_profile_patch(self, scope: ProfileScope, message_id: int) -> StudioPatchResult:
        """Apply a profile patch after re-checking the base hash.

        The current file must still match ``base_hash`` recorded when the patch was
        proposed. The four files are staged and validated before the target file is
        swapped, then the profile is reloaded and audited. No patch is applied without an
        explicit call from the operator.
        """
        self._require_enabled()
        message = self._messages.get(scope, message_id)
        if message is None:
            raise StudioNotFoundError(f"studio message {message_id} does not exist")
        if message.artifact_type != "profile_patch":
            raise StudioError("only a profile patch can be applied")
        data = message.artifact_json or {}
        if data.get("applied"):
            raise StudioError("this patch has already been applied")
        file = data.get("file")
        proposed_text = data.get("proposed_text")
        base_hash = data.get("base_hash")
        if file not in PROFILE_FILES:
            raise StudioError("the patch has no valid target file")
        if not isinstance(proposed_text, str) or not proposed_text.strip():
            raise StudioError("the patch has no proposed content")
        if not isinstance(base_hash, str):
            raise StudioError("the patch has no base hash")

        summary = profile_summary(self._session, scope)
        state = read_profile_files(self._layout, summary.slug)
        current = state.files[file]
        current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
        if current_hash != base_hash:
            raise StudioError(f"{file} changed since the patch was proposed; regenerate the patch")

        files = dict(state.files)
        files[file] = proposed_text
        try:
            save_profile_files(self._session, self._layout, summary.slug, files)
        except ProfileEditError as exc:
            raise StudioError(self._redactor.redact(str(exc))[:MAX_AUDIT_TEXT]) from exc

        new_hash = hashlib.sha256(proposed_text.encode("utf-8")).hexdigest()
        data["applied"] = True
        message.artifact_json = data
        self._session.flush()
        self._audit.record(
            scope,
            actor="user",
            action="studio.profile_patch_applied",
            entity="profile",
            entity_id=scope.profile_id,
            payload={
                "slug": summary.slug,
                "file": file,
                "old_hash": current_hash,
                "new_hash": new_hash,
            },
        )
        return StudioPatchResult(summary.slug, file, current_hash, new_hash)

    def _history(self, scope: ProfileScope, thread_id: int) -> list[StudioTurn]:
        total_messages = self._messages.count(scope, thread_id)
        recent = list(self._messages.recent(scope, thread_id, limit=HISTORY_MESSAGES))
        history = [
            StudioTurn.model_validate({"role": item.role, "content": item.content})
            for item in recent
        ]
        if total_messages > len(history):
            history.insert(
                0,
                StudioTurn(
                    role="system",
                    content=(
                        f"The oldest {total_messages - len(history)} message(s) were omitted "
                        "to keep the context bounded."
                    ),
                ),
            )
        return history

    def _latest_draft(self, scope: ProfileScope, thread_id: int) -> dict[str, Any] | None:
        for message in reversed(self._messages.list_messages(scope, thread_id, limit=200)):
            if message.artifact_type == "draft" and message.artifact_json:
                data = message.artifact_json
                if data.get("body") and data.get("platform"):
                    return data
        return None

    def _queue_lines(self, scope: ProfileScope) -> list[str]:
        entries = list_queue(self._session, scope, limit=QUEUE_ITEMS)
        return [
            f"#{entry.item_id} {entry.platform} {entry.item_state} "
            f"risk={entry.risk_level or 'unknown'}: {_collapse(entry.body, MAX_PREVIEW_CHARS)}"
            for entry in entries
        ]

    def _calendar_lines(self, scope: ProfileScope) -> list[str]:
        now = self._clock()
        end = now + timedelta(days=CALENDAR_DAYS)
        items = ContentItemRepository(self._session).list(scope, limit=200)
        lines: list[str] = []
        for item in items:
            if item.scheduled_at is None:
                continue
            when = as_utc(item.scheduled_at)
            if not (now <= when < end):
                continue
            lines.append(
                f"{when.isoformat(timespec='minutes')} {item.platform} {item.state}: "
                f"{_collapse(item.body, MAX_PREVIEW_CHARS)}"
            )
        return sorted(lines)[:20]
