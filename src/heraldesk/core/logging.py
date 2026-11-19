"""Rotating, secret-redacted file logging for the single-process server.

The file lives in ``$HERALDESK_HOME/logs/heraldesk.log`` with mode 600, rotates at 5 MiB
and keeps three backups. A filter redacts known secret values from the formatted message
before it reaches disk. Logging never replaces the console output configured by uvicorn.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from heraldesk import paths
from heraldesk.core.redact import SecretRegistry

LOGGER_NAME = "heraldesk"
LOG_FILENAME = "heraldesk.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
LOG_MODE = 0o600
_FILE_MARKER = "_heraldesk_file_handler"


class _PrivateRotatingFileHandler(RotatingFileHandler):
    """Rotating handler that keeps every new file at mode 600."""

    def _open(self) -> Any:
        descriptor = os.open(
            self.baseFilename,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND,
            LOG_MODE,
        )
        return os.fdopen(descriptor, self.mode, encoding=self.encoding)


class _RedactionFilter(logging.Filter):
    """Redact known secrets from a record before formatting."""

    def __init__(self, redactor: SecretRegistry) -> None:
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        record.msg = self._redactor.redact(message)
        record.args = ()
        return True


def log_path(layout: paths.Layout) -> Path:
    """Return the configured log file path."""
    return layout.logs / LOG_FILENAME


def configure_logging(
    layout: paths.Layout,
    *,
    level: int = logging.INFO,
    extra_secrets: Sequence[str] = (),
) -> Path:
    """Attach one rotating file handler to the ``heraldesk`` logger, idempotently."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in logger.handlers:
        if getattr(handler, _FILE_MARKER, False):
            return log_path(layout)

    layout.logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = log_path(layout)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, LOG_MODE)
        os.close(descriptor)
        if os.name == "posix":
            path.chmod(LOG_MODE)
    except OSError:
        # Logging setup must never stop the server; uvicorn still logs to the console.
        return path

    handler = _PrivateRotatingFileHandler(
        path,
        maxBytes=MAX_BYTES,
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
    )
    setattr(handler, _FILE_MARKER, True)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(
        _RedactionFilter(SecretRegistry.from_environment(extra_values=list(extra_secrets)))
    )
    logger.addHandler(handler)
    if logger.level == logging.NOTSET or logger.level > level:
        logger.setLevel(level)
    return path


def reset_logging() -> None:
    """Remove the Heraldesk file handler. Intended for tests and process shutdown."""
    logger = logging.getLogger(LOGGER_NAME)
    for handler in list(logger.handlers):
        if getattr(handler, _FILE_MARKER, False):
            logger.removeHandler(handler)
            handler.close()
    if logger.level == logging.INFO:
        logger.setLevel(logging.NOTSET)
