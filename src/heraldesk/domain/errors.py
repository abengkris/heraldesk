"""Domain-level errors with messages meant for CLI users."""

from __future__ import annotations


class DomainError(Exception):
    """Base class for expected domain failures."""


class ProfileValidationError(DomainError):
    """A profile file is missing, malformed or fails schema validation."""


class ProfileNotFoundError(DomainError):
    """The requested profile is not registered or its directory is missing."""


class AccountError(DomainError):
    """An account operation cannot be completed."""


class AccountExistsError(AccountError):
    """The same platform/handle already exists for this profile."""


class UnknownPlatformError(AccountError):
    """The platform name is not one Heraldesk knows about."""


class NoAccountsError(AccountError):
    """No social account matched the request."""
