"""Profile-scoped repository access.

Every repository function that touches profile-owned data takes a
:class:`ProfileScope` as its first argument. The scope holds exactly one profile id;
there is no "all profiles" mode on scoped repositories.
"""

from __future__ import annotations

from dataclasses import dataclass


class ScopeError(Exception):
    """Raised when a profile scope is invalid."""


@dataclass(frozen=True, slots=True, order=True)
class ProfileScope:
    """The single profile a repository call is allowed to touch."""

    profile_id: int

    def __post_init__(self) -> None:
        if isinstance(self.profile_id, bool) or not isinstance(self.profile_id, int):
            raise ScopeError("profile_id must be an integer")
        if self.profile_id <= 0:
            raise ScopeError("profile_id must be positive")
