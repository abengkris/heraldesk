"""Redact secrets before anything is written to the database or printed.

Known values come from environment variables whose names look like credentials (for
example ``DEEPSEEK_API_KEY``) plus optional values parsed from the instance ``.env``.
Well-known token shapes are scrubbed even when the value is unknown, so a leaked token
inside an LLM reply does not end up in ``agent_runs``.

The redactor never reads the ``.env`` file itself: callers pass an explicit mapping, which
keeps this module free of filesystem side effects.
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Mapping
from typing import Any, Final

REDACTED: Final = "[REDACTED]"
MIN_SECRET_LENGTH = 8

_SECRET_NAME_PATTERN = re.compile(
    r"(api[_-]?key|apikey|access[_-]?key|private[_-]?key|secret|token|password)$"
    r"|(^|_)nsec(_|$)",
    re.IGNORECASE,
)
_SECRET_VALUE_PATTERNS: Final = (
    # OpenAI-style and compatible provider keys.
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    # GitHub and Slack tokens.
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    # Bearer headers copied into a model reply.
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{12,}"),
    # Nostr secret keys (nsec) and public identifiers (npub) in logs and audit payloads.
    re.compile(r"(?i)\bnsec1[02-9ac-hj-np-z]{58}\b"),
    re.compile(r"(?i)\bnpub1[02-9ac-hj-np-z]{58}\b"),
)


def looks_like_secret_name(name: str) -> bool:
    """Return ``True`` when an environment variable name looks credential-shaped."""
    return _SECRET_NAME_PATTERN.search(name) is not None


class SecretRegistry:
    """Replace known secret values and recognisable token shapes with ``[REDACTED]``."""

    def __init__(self, values: Iterable[str] = ()) -> None:
        unique = {
            value for value in values if isinstance(value, str) and len(value) >= MIN_SECRET_LENGTH
        }
        # Longest first so a value that contains another value is replaced as a whole.
        self._values = tuple(sorted(unique, key=len, reverse=True))

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        extra_values: Iterable[str] = (),
    ) -> SecretRegistry:
        """Collect values of credential-shaped environment variables plus *extra_values*."""
        source = os.environ if env is None else env
        values = [value for name, value in source.items() if looks_like_secret_name(name) and value]
        values.extend(extra_values)
        return cls(values)

    @property
    def known_values(self) -> tuple[str, ...]:
        """Return the configured secret values. Never log or print this."""
        return self._values

    def redact(self, text: str) -> str:
        """Return *text* with every known value and recognisable token shape replaced."""
        result = text
        for value in self._values:
            if value in result:
                result = result.replace(value, REDACTED)
        for pattern in _SECRET_VALUE_PATTERNS:
            result = pattern.sub(REDACTED, result)
        return result

    def redact_json(self, value: Any) -> Any:
        """Recursively redact strings inside JSON-compatible data."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {
                (self.redact(key) if isinstance(key, str) else key): self.redact_json(item)
                for key, item in value.items()
            }
        if isinstance(value, (list, tuple)):
            return [self.redact_json(item) for item in value]
        return value
