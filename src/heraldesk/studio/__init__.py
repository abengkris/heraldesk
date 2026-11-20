"""Draft Room service: planning conversation with no approval or publish path.

The service owns thread local state only. It never imports the publisher, scheduler or a
platform adapter, and it never changes ``content_items.state``. Draft creation and profile
patches arrive in later phases and must call the existing approval and profile services.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.agents.base import AgentContext
from heraldesk.agents.editor import Editor
from heraldesk.agents.runner import AgentRunner, AgentRunResult
from heraldesk.agents.schemas import EditorInput, EditorReply, StudioTurn
from heraldesk.core.audit import AuditService
from heraldesk.core.redact import SecretRegistry
from heraldesk.db.models import StudioMessage, StudioThread
from heraldesk.db.repositories import (
    ContentItemRepository,
    StudioMessageRepository,
    StudioThreadRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.approvals import as_utc, list_queue
from heraldesk.domain.profiles import load_profile, profile_summary
from heraldesk.llm.types import LLMProvider

MAX_MESSAGE_CHARS = 4000
MAX_TITLE_CHARS = 200
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
    """One successful user turn and its Editor reply."""

    thread: StudioThread
    user_message: StudioMessage
    assistant_message: StudioMessage
    run: AgentRunResult


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
    ) -> StudioTurnResult:
        """Run one Editor turn and store the user and assistant messages.

        The message limit and thread cost cap are checked before any provider call. The
        global daily cap is still enforced by :class:`AgentRunner`.
        """
        self._require_enabled()
        if role != "editor":
            raise StudioRoleError(
                f"role '{role}' is not available in this phase; only 'editor' is callable"
            )
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
        payload = EditorInput(
            message=safe_message,
            history=history,
            queue=self._queue_lines(scope),
            calendar=self._calendar_lines(scope),
        )
        context = AgentContext(scope=scope, profile=definition)
        runner = AgentRunner(
            self._session,
            self._provider,
            layout=self._layout,
            model=self._model,
            cap_usd=self._daily_cap_usd,
            redactor=self._redactor,
        )
        run = runner.run(Editor(), context, payload)
        if not isinstance(run.output, EditorReply):
            raise StudioError("editor returned an unexpected output type")

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
            role="editor",
            content=self._redactor.redact(run.output.reply.strip()),
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
                "role": "editor",
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
