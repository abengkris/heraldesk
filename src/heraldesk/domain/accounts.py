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
from heraldesk.platforms.base import (
    KNOWN_PLATFORMS,
    AccountRef,
    PlatformAdapter,
    PublishError,
)
from heraldesk.platforms.nostr import npub_from_secret, parse_relay_list
from heraldesk.platforms.nostr_keys import NostrKeyError, decode_nsec, is_npub, mask_npub

MAX_LABEL_LENGTH = 120


@dataclass(frozen=True, slots=True)
class AccountTestResult:
    """Outcome of checking one account's credentials."""

    account_id: int
    platform: str
    handle: str
    ok: bool
    detail: str


def _nostr_handle(handle: str, credentials: Mapping[str, str]) -> str:
    """Return the public npub for a Nostr account and validate the key material.

    The nsec must never become the handle, a label or an audit value. A missing or
    malformed key is an :class:`AccountError` whose message never echoes the key.
    """
    secret_value = credentials.get("nsec", "").strip()
    if not secret_value:
        raise AccountError(
            "nostr accounts require an 'nsec' credential; pass it with "
            "--credential-from-env so the key is never typed on the command line"
        )
    try:
        secret = decode_nsec(secret_value)
    except NostrKeyError as exc:
        raise AccountError(f"invalid nostr nsec credential: {exc}") from exc

    candidate = handle.strip()
    if candidate.lower().startswith("nsec1"):
        raise AccountError("handle must be the public npub, never the secret nsec")
    if candidate and not is_npub(candidate):
        raise AccountError("nostr handles must be a valid npub1... public key")
    if "relays" in credentials:
        try:
            parse_relay_list(credentials["relays"])
        except PublishError as exc:
            raise AccountError(str(exc)) from exc
    return candidate or npub_from_secret(secret)


def add_account(
    session: Session,
    scope: ProfileScope,
    *,
    platform: str,
    handle: str,
    credentials: Mapping[str, str],
    cipher: CredentialCipher,
    label: str | None = None,
    actor: str = "user",
) -> SocialAccount:
    """Encrypt credentials and add an account to the scoped profile.

    For ``nostr`` the handle is the public npub; it is derived from the nsec when omitted
    and audited in masked form. The nsec itself only ever travels into the encrypted
    credential blob.
    """
    repository = SocialAccountRepository(session)
    try:
        ProfileRepository(session).require(scope)
    except RecordNotFound as exc:
        raise ProfileNotFoundError(str(exc)) from exc

    normalized_platform = platform.strip().lower()
    if normalized_platform not in KNOWN_PLATFORMS:
        from heraldesk.plugins import plugin_adapters

        if normalized_platform not in plugin_adapters():
            known = ", ".join(sorted(KNOWN_PLATFORMS))
            raise UnknownPlatformError(
                f"unknown platform '{platform}'; known platforms: {known}; "
                "install a plugin to add another platform"
            )

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

    normalized_handle = handle.strip()
    if normalized_platform == "nostr":
        normalized_handle = _nostr_handle(normalized_handle, clean)
    if normalized_platform == "threads" and not clean.get("access_token", "").strip():
        raise AccountError(
            "threads accounts require an 'access_token' credential; "
            "pass it with --credential-from-env so the token is never typed on the command line"
        )
    if not normalized_handle:
        raise AccountError("handle must not be empty")

    normalized_label = (label or "").strip() or None
    if normalized_label is not None and len(normalized_label) > MAX_LABEL_LENGTH:
        raise AccountError(f"label must be at most {MAX_LABEL_LENGTH} characters")

    try:
        account = repository.add(
            scope,
            platform=normalized_platform,
            handle=normalized_handle,
            label=normalized_label,
            credentials_encrypted=cipher.encrypt_json(clean),
        )
    except DuplicateRecord as exc:
        raise AccountExistsError(str(exc)) from exc

    audit_handle = mask_npub(account.handle) if account.platform == "nostr" else account.handle
    AuditService(session).record(
        scope,
        actor=actor,
        action="account.added",
        entity="social_account",
        entity_id=account.id,
        payload={
            "platform": account.platform,
            "handle": audit_handle,
            "label": account.label,
        },
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
