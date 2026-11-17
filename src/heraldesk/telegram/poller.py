"""Telegram ``getUpdates`` polling for ``heraldesk serve``.

The poller keeps the Bot API offset in memory and advances it before a message is
handled, so a crash cannot replay the same update forever. It also relies on the
database deduplication in :func:`heraldesk.telegram.inbox.handle_update`: Telegram may
replay updates after a restart, and a recorded ``update_id`` is never processed twice.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.core.redact import SecretRegistry
from heraldesk.telegram.base import TelegramAdapter
from heraldesk.telegram.inbox import InboxOutcome, handle_update


@dataclass(frozen=True, slots=True)
class PollResult:
    """Outcome of one ``getUpdates`` poll."""

    received: int = 0
    processed: int = 0
    duplicates: int = 0
    ignored: int = 0
    errors: int = 0
    next_offset: int | None = None


class TelegramPoller:
    """Poll one Telegram adapter and feed allowlisted updates to the inbox."""

    def __init__(
        self,
        adapter: TelegramAdapter,
        *,
        allowed_chat_ids: Sequence[int],
        profile_hint: str | None = None,
        redactor: SecretRegistry | None = None,
    ) -> None:
        self._adapter = adapter
        self._allowed_chat_ids = tuple(allowed_chat_ids)
        self._profile_hint = profile_hint
        self._redactor = redactor
        self._offset: int | None = None

    @property
    def offset(self) -> int | None:
        """Return the offset sent with the next ``getUpdates`` call."""
        return self._offset

    def poll_once(self, session: Session, layout: paths.Layout) -> PollResult:
        """Fetch and handle one batch of updates.

        The offset advances for every update in the batch, including ignored and
        duplicate ones, so a non-allowlisted chat cannot block the queue.
        """
        updates = self._adapter.poll_updates(self._offset)
        processed = 0
        duplicates = 0
        ignored = 0
        errors = 0
        next_offset = self._offset
        for update in updates:
            outcome: InboxOutcome = handle_update(
                session,
                layout,
                update,
                allowed_chat_ids=self._allowed_chat_ids,
                adapter=self._adapter,
                profile_hint=self._profile_hint,
                redactor=self._redactor,
            )
            if update.update_id is not None:
                next_offset = update.update_id + 1
            if outcome.status == "duplicate":
                duplicates += 1
            elif outcome.status == "ignored":
                ignored += 1
            elif outcome.status == "error":
                errors += 1
                processed += 1
            else:
                processed += 1
        # Advance only after the whole batch was handled. If any update raises, the
        # caller's transaction rolls back and the same batch is retried, not confirmed.
        self._offset = next_offset
        return PollResult(
            received=len(updates),
            processed=processed,
            duplicates=duplicates,
            ignored=ignored,
            errors=errors,
            next_offset=next_offset,
        )
