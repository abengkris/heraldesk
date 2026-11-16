"""Social account use cases: add an encrypted account and test its credentials.

Credentials are decrypted only for the adapter call. They never enter audit payloads,
CLI output or error messages.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from sqlalchemy.orm import Session

from heraldesk.core.audit import AuditService
from heraldesk.core.crypto import CredentialCipher, CryptoError
from heraldesk.db.models import SocialAccount, utcnow
from heraldesk.db.repositories import (
    DuplicateRecord,
    ProfileRepository,
    RecordNotFound,
    SocialAccountRepository,
)
from heraldesk.db.scope import ProfileScope
from heraldesk.domain.errors import (
    AccountError,
    AccountExistsError,
    NoAccountsError,
    ProfileNotFoundError,
    UnknownPlatformError,
)
from heraldesk.platforms.base import KNOWN_PLATFORMS, AccountRef, PlatformAdapter


@dataclass(frozen=True, slots=True)
class AccountTestResult:
    """Outcome of checking one account's credentials."""

    account_id: int
    platform: str
    handle: str
    ok: bool
    detail: str


def add_account(
    session: Session,
    scope: ProfileScope,
    *,
    platform: str,
    handle: str,
    credentials: Mapping[str, str],
    cipher: CredentialCipher,
    actor: str = "user",
) -> SocialAccount:
    """Encrypt credentials and add an account to the scoped profile."""
    repository = SocialAccountRepository(session)
    try:
        ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ProfileNotFoundError(str(exc)) from exc

    normalized_platform = platform.strip().lower()
    if normalized_platform not in KNOWN_PLATFORMS:
        known = ", ".join(sorted(KNOWN_PLATFORMS))
        raise UnknownPlatformError(f"unknown platform '{platform}'; known platforms: {known}")
    normalized_handle = handle.strip()
    if not normalized_handle:
        raise AccountError("handle must not be empty")

    clean: dict[str, str] = {}
    for key, value in credentials.items():
        clean_key = key.strip()
        if not clean_key:
            raise AccountError("credential keys must not be empty")
        if not value.strip():
            raise AccountError(f"credential '{clean_key}' must not be empty")
        clean[clean_key] = value
    if not clean:
        raise AccountError("at least one credential is required")

    try:
        account = repository.add(
            scope,
            platform=normalized_platform,
            handle=normalized_handle,
            credentials_encrypted=cipher.encrypt_json(clean),
        )
    except DuplicateRecord as exc:
        raise AccountExistsError(str(exc)) from exc

    AuditService(session).record(
        scope,
        actor=actor,
        action="account.added",
        entity="social_account",
        entity_id=account.id,
        payload={"platform": account.platform, "handle": account.handle},
    )
    return account


async def check_accounts(
    session: Session,
    scope: ProfileScope,
    *,
    cipher: CredentialCipher,
    adapter: PlatformAdapter,
    platform: str | None = None,
    handle: str | None = None,
    actor: str = "user",
) -> list[AccountTestResult]:
    """Check matching accounts with *adapter* and persist the result."""
    try:
        ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ProfileNotFoundError(str(exc)) from exc

    repository = SocialAccountRepository(session)
    accounts = repository.list(scope)
    if platform is not None:
        wanted = platform.strip().lower()
        accounts = [account for account in accounts if account.platform == wanted]
    if handle is not None:
        wanted_handle = handle.strip()
        accounts = [account for account in accounts if account.handle == wanted_handle]
    if not accounts:
        raise NoAccountsError("no social account matches this profile and filter")

    results: list[AccountTestResult] = []
    for account in accounts:
        ok = False
        try:
            decrypted = cipher.decrypt_json(account.credentials_encrypted)
            ok = await adapter.validate_credentials(
                AccountRef(
                    platform=account.platform,
                    handle=account.handle,
                    credentials=decrypted,
                )
            )
            detail = "credentials accepted" if ok else "credentials rejected by the adapter"
        except CryptoError:
            detail = (
                "credentials cannot be decrypted; secret.key does not match "
                "or the stored value is corrupted"
            )
        except Exception as exc:  # adapter failures must not abort the whole batch
            detail = f"adapter error: {type(exc).__name__}"

        repository.mark_checked(
            scope,
            account.id,
            status="active" if ok else "error",
            checked_at=utcnow(),
        )
        AuditService(session).record(
            scope,
            actor=actor,
            action="account.tested",
            entity="social_account",
            entity_id=account.id,
            payload={
                "platform": account.platform,
                "handle": account.handle,
                "ok": ok,
            },
        )
        results.append(
            AccountTestResult(
                account_id=account.id,
                platform=account.platform,
                handle=account.handle,
                ok=ok,
                detail=detail,
            )
        )
    return results
