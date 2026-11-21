"""Web-facing compatibility layer for the shared profile-file editor helpers.

The implementation lives in :mod:`heraldesk.domain.profile_edit` so the Draft Room and the
Web UI share one validation, lock and write path.
"""

from __future__ import annotations

from heraldesk.domain.profile_edit import (
    MAX_FILE_BYTES,
    PROFILE_FILES,
    AutonomyConfirmationRequired,
    PolicyHelperValues,
    ProfileEditError,
    ProfileEditState,
    StaleProfileEdit,
    apply_policy_helper,
    content_hash,
    policy_helper_values,
    read_profile_files,
    save_profile_file,
    save_profile_files,
    validate_profile_files,
)

__all__ = [
    "MAX_FILE_BYTES",
    "PROFILE_FILES",
    "AutonomyConfirmationRequired",
    "PolicyHelperValues",
    "ProfileEditError",
    "ProfileEditState",
    "StaleProfileEdit",
    "apply_policy_helper",
    "content_hash",
    "policy_helper_values",
    "read_profile_files",
    "save_profile_file",
    "save_profile_files",
    "validate_profile_files",
]
