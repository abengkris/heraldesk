"""Recurring background work for ``heraldesk serve``.

APScheduler triggers two stateless sweeps:

* the publisher sweep every 60 seconds, using :func:`heraldesk.scheduler.runner.run_scheduler`;
* Telegram ``getUpdates`` every 10 seconds, only in ``telegram.mode: polling``.

The trigger definitions live in memory on purpose. Durable work is already in SQLite
(content items, approvals, the Telegram ledger), so a restart recreates the triggers and
an immediate first sweep resumes everything without an ``apscheduler_jobs`` table. This is
the M5 pattern extended to the server; see ``docs/decisions.md`` (M7).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from sqlalchemy.orm import Session

from heraldesk import paths
from heraldesk.config import Config
from heraldesk.core.redact import SecretRegistry
from heraldesk.scheduler.runner import SchedulerResult, run_scheduler
from heraldesk.server.database import ServerDatabase
from heraldesk.telegram.base import TelegramAdapter
from heraldesk.telegram.notifier import NotificationService
from heraldesk.telegram.poller import PollResult, TelegramPoller

DEFAULT_SCHEDULER_INTERVAL_SECONDS = 60.0
DEFAULT_POLL_INTERVAL_SECONDS = 10.0

logger = logging.getLogger("heraldesk.server")


class BackgroundJobs:
    """Own the APScheduler instance and the two recurring sweeps."""

    def __init__(
        self,
        database: ServerDatabase,
        layout: paths.Layout,
        config: Config,
        *,
        adapter: TelegramAdapter | None = None,
        redactor: SecretRegistry | None = None,
        scheduler_interval_seconds: float = DEFAULT_SCHEDULER_INTERVAL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if scheduler_interval_seconds <= 0 or poll_interval_seconds <= 0:
            raise ValueError("background intervals must be positive")
        self._database = database
        self._layout = layout
        self._config = config
        self._adapter = adapter
        self._redactor = redactor or SecretRegistry.from_environment()
        self._scheduler_interval = scheduler_interval_seconds
        self._poll_interval = poll_interval_seconds
        self._scheduler: BackgroundScheduler | None = None
        self._poller: TelegramPoller | None = None
        if adapter is not None:
            self._poller = TelegramPoller(
                adapter,
                allowed_chat_ids=tuple(config.telegram.allowed_chat_ids),
                allowed_user_ids=tuple(config.telegram.allowed_user_ids),
                redactor=self._redactor,
            )

    @property
    def running(self) -> bool:
        """Return ``True`` while APScheduler is accepting triggers."""
        return self._scheduler is not None and self._scheduler.running

    @property
    def polls_telegram(self) -> bool:
        """Return ``True`` when the polling job is active."""
        return (
            self._poller is not None
            and self._config.telegram.enabled
            and self._config.telegram.mode == "polling"
        )

    def start(self) -> None:
        """Start APScheduler and schedule an immediate first sweep."""
        if self._scheduler is not None:
            return
        scheduler = BackgroundScheduler(timezone="UTC")
        scheduler.add_job(
            self._scheduler_job,
            "interval",
            seconds=self._scheduler_interval,
            id="heraldesk.publish_sweep",
            name="publish sweep",
            next_run_time=datetime.now(UTC),
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        if self.polls_telegram:
            scheduler.add_job(
                self._poll_job,
                "interval",
                seconds=self._poll_interval,
                id="heraldesk.telegram_poll",
                name="telegram poll",
                next_run_time=datetime.now(UTC),
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        scheduler.start()
        self._scheduler = scheduler

    def stop(self) -> None:
        """Stop APScheduler and wait for an in-flight sweep to finish."""
        scheduler = self._scheduler
        self._scheduler = None
        if scheduler is None:
            return
        if scheduler.running:
            scheduler.shutdown(wait=True)

    def _notifier(self, session: Session) -> NotificationService | None:
        if self._adapter is None or not self._config.telegram.enabled:
            return None
        return NotificationService(
            session,
            self._layout,
            self._adapter,
            allowed_chat_ids=tuple(self._config.telegram.allowed_chat_ids),
            redactor=self._redactor,
        )

    def run_scheduler_once(self, *, dry_run: bool = False) -> SchedulerResult:
        """Run one publisher sweep in its own session.

        ``dry_run=True`` keeps the M5 guarantee: no state change, no attempt counter, no
        audit row, no adapter call and no notification.
        """
        with self._database.session() as session:
            return run_scheduler(
                session,
                self._layout,
                dry_run=dry_run,
                notifier=None if dry_run else self._notifier(session),
            )

    def run_poll_once(self) -> PollResult | None:
        """Poll Telegram once, or return ``None`` when polling is not configured."""
        if (
            self._poller is None
            or not self._config.telegram.enabled
            or self._config.telegram.mode != "polling"
        ):
            return None
        with self._database.session() as session:
            return self._poller.poll_once(session, self._layout)

    def _scheduler_job(self) -> None:
        try:
            self.run_scheduler_once()
        except Exception as exc:
            # A job thread must stay alive; log only the exception type, never payloads.
            logger.warning("publish sweep failed (%s)", type(exc).__name__)

    def _poll_job(self) -> None:
        try:
            self.run_poll_once()
        except Exception as exc:
            # Keep the interval trigger alive without logging message text or chat ids.
            logger.warning("telegram poll failed (%s)", type(exc).__name__)


__all__ = (
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_SCHEDULER_INTERVAL_SECONDS",
    "BackgroundJobs",
)
