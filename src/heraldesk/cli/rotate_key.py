"""``heraldesk admin rotate-key``: re-encrypt credentials with a new secret key.

The operation is deliberately conservative:

1. decrypt every account and the encrypted Telegram token with the current key;
2. write a full credential backup;
3. stage a new key and re-encrypted values;
4. update the database, swap the key and token files, then verify;
5. audit the rotation and revoke UI sessions.

If any step before the database commit fails, the old key stays active and nothing is
lost. Once the database commit succeeds, a failure in the file swap or verification is a
disaster-recovery case: the printed backup contains the old key and is the restore source.
Stop ``heraldesk serve`` before rotating.
"""

from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
from typing import Annotated

import typer

from heraldesk import paths
from heraldesk.cli.common import abort, console, session_for
from heraldesk.core.audit import AuditService
from heraldesk.core.backup import BackupError, create_backup
from heraldesk.core.crypto import (
    CredentialCipher,
    CryptoError,
    generate_key,
    load_key,
)
from heraldesk.db.repositories import ProfileCatalog, SocialAccountRepository
from heraldesk.db.scope import ProfileScope
from heraldesk.web.auth import AuthConfigError, rotate_session_epoch, session_epoch_path

_KEY_MODE = 0o600

logger = logging.getLogger("heraldesk.cli.rotate_key")


def _write_private(path: Path, data: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _KEY_MODE)
    with os.fdopen(descriptor, "w", encoding="ascii") as handle:
        handle.write(data)
    if os.name == "posix":
        path.chmod(_KEY_MODE)


def rotate_key_command(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Decrypt and count credentials without changing anything."),
    ] = False,
) -> None:
    """Generate a new secret.key and re-encrypt every stored credential."""
    _ensure_stage_logging()
    layout = paths.layout_for(paths.resolve_home())
    if layout.telegram_token_path.exists() and not layout.telegram_token_path.is_file():
        abort(
            f"{layout.telegram_token_path} exists but is not a regular file; "
            "secret.key was not changed."
        )
    try:
        old_key = load_key(layout.key_path)
        old_cipher = CredentialCipher(old_key)
    except CryptoError as exc:
        abort(str(exc))

    accounts: list[tuple[ProfileScope, int, str, dict[str, str]]] = []
    with session_for() as (_, session):
        for summary in ProfileCatalog(session).list():
            scope = ProfileScope(summary.id)
            for account in SocialAccountRepository(session).list(scope):
                try:
                    plaintext = old_cipher.decrypt_json(account.credentials_encrypted)
                except CryptoError as exc:
                    abort(
                        f"cannot decrypt account #{account.id} ({account.platform}): {exc}. "
                        "secret.key was not changed."
                    )
                accounts.append((scope, account.id, account.platform, plaintext))

    token_file = layout.telegram_token_path.is_file()
    telegram_token: str | None = None
    if token_file:
        try:
            telegram_token = _read_persisted_token(layout, old_cipher)
        except CryptoError as exc:
            abort(
                f"cannot decrypt the persisted Telegram token at "
                f"{layout.telegram_token_path}: {exc}. secret.key was not changed."
            )

    logger.info(
        "key rotation started accounts=%d telegram_file=%s",
        len(accounts),
        token_file,
    )
    if dry_run:
        console.print(
            f"[bold]Would rotate:[/bold] {len(accounts)} account(s)"
            f"{' and the persisted Telegram token' if telegram_token is not None else ''}."
        )
        console.print("No files or database rows were changed.")
        return

    try:
        backup = create_backup(layout, include_credentials=True)
    except BackupError as exc:
        abort(f"cannot create the required pre-rotation backup; aborted: {exc}")
    logger.info("credential backup written path=%s", backup.path)

    # Revoke sessions before touching credentials. If this fails, nothing is rotated.
    try:
        rotate_session_epoch(session_epoch_path(layout))
    except AuthConfigError as exc:
        abort(
            f"cannot revoke existing sessions: {exc}. Rotation aborted before any "
            "credential changed. Fix the data-folder permissions and retry."
        )
    logger.info("session epoch rotated")

    new_key = generate_key()
    new_cipher = CredentialCipher(new_key)
    new_accounts = [
        (scope, account_id, new_cipher.encrypt_json(plaintext))
        for scope, account_id, _platform, plaintext in accounts
    ]
    new_token = new_cipher.encrypt(telegram_token) if telegram_token is not None else None

    temporary_key = layout.key_path.with_name(f".{layout.key_path.name}.new")
    temporary_token = layout.telegram_token_path.with_name(
        f".{layout.telegram_token_path.name}.new"
    )
    try:
        _write_private(temporary_key, new_key.decode("ascii"))
        if new_token is not None:
            _write_private(temporary_token, new_token)
    except OSError as exc:
        _cleanup(temporary_key, temporary_token)
        abort(f"cannot stage the new key; aborted: {exc}")

    try:
        with session_for() as (_, session):
            repository = SocialAccountRepository(session)
            for scope, account_id, encrypted in new_accounts:
                if (
                    repository.set_credentials(
                        scope,
                        account_id,
                        credentials_encrypted=encrypted,
                    )
                    is None
                ):
                    raise CryptoError(f"account #{account_id} disappeared during rotation")
    except (CryptoError, OSError) as exc:
        _cleanup(temporary_key, temporary_token)
        abort(f"database update failed; secret.key was not changed: {exc}")

    key_replaced = False
    try:
        os.replace(temporary_key, layout.key_path)
        key_replaced = True
        if new_token is not None:
            os.replace(temporary_token, layout.telegram_token_path)
    except OSError as exc:
        if key_replaced:
            try:
                _write_private(temporary_key, old_key.decode("ascii"))
                os.replace(temporary_key, layout.key_path)
            except OSError as rollback_exc:
                _cleanup(temporary_key, temporary_token)
                abort(
                    f"key swap failed ({exc}) and restoring the old key failed "
                    f"({rollback_exc}). Restore the backup at {backup.path}."
                )
        with session_for() as (_, session):
            repository = SocialAccountRepository(session)
            for scope, account_id, _platform, plaintext in accounts:
                repository.set_credentials(
                    scope,
                    account_id,
                    credentials_encrypted=old_cipher.encrypt_json(plaintext),
                )
        _cleanup(temporary_key, temporary_token)
        abort(f"key swap failed and the database was rolled back: {exc}. Backup: {backup.path}")

    try:
        verified = CredentialCipher.from_key_file(layout.key_path)
        with session_for() as (_, session):
            repository = SocialAccountRepository(session)
            for scope, account_id, _platform, _plaintext in accounts:
                stored = repository.get(scope, account_id)
                if stored is None:
                    raise CryptoError(f"account #{account_id} disappeared after rotation")
                verified.decrypt_json(stored.credentials_encrypted)
        if new_token is not None:
            verified.decrypt(new_token)
    except CryptoError as exc:
        abort(f"rotation verification failed: {exc}. Restore the backup at {backup.path}.")

    try:
        with session_for() as (_, session):
            AuditService(session).record(
                None,
                actor="user",
                action="system.key_rotated",
                entity="system",
                payload={
                    "accounts": len(accounts),
                    "telegram_reencrypted": new_token is not None,
                    "backup": backup.path.name,
                },
            )
    except Exception as exc:  # rotation succeeded; auditing must not hide that
        console.print(
            f"[yellow]Warning:[/yellow] rotation succeeded but the audit entry failed "
            f"({type(exc).__name__})."
        )

    logger.info(
        "key rotation completed accounts=%d telegram_reencrypted=%s",
        len(accounts),
        new_token is not None,
    )
    console.print(
        f"[green]Key rotated:[/green] {len(accounts)} account(s) re-encrypted"
        f"{' and the persisted Telegram token' if new_token is not None else ''}. "
        "Existing UI sessions were revoked."
    )
    console.print(f"Backup: {backup.path}")
    console.print(
        "If you rely on the secret.key-derived admin token, run 'heraldesk admin token --show' "
        "for the new value. Explicit tokens in the environment or .env are unchanged."
    )


def _cleanup(*paths_to_remove: Path) -> None:
    for path in paths_to_remove:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


def _ensure_stage_logging() -> None:
    """Show rotation stage logs on stderr when the CLI has no logging configuration."""
    root = logging.getLogger()
    if root.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)


def _read_persisted_token(layout: paths.Layout, cipher: CredentialCipher) -> str:
    """Decrypt the token file directly. Environment and .env overrides are ignored."""
    path = layout.telegram_token_path
    try:
        ciphertext = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise CryptoError(f"cannot read {path}: {exc}") from exc
    if not ciphertext:
        raise CryptoError("the persisted token file is empty")
    return cipher.decrypt(ciphertext)
