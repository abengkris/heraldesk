"""Safe message formatting for Telegram notifications and inbox replies.

Bodies are previews, chat and sender ids are partially masked, and every text passes
through the caller's :class:`~heraldesk.core.redact.SecretRegistry` before storage or
printing. Full chat ids stay only in ``telegram_messages`` for routing.
"""

from __future__ import annotations

from heraldesk.telegram.base import MAX_MESSAGE_LENGTH

PREVIEW_LIMIT = 180
ERROR_LIMIT = 160


def mask_chat_id(chat_id: int | str) -> str:
    """Partially mask a chat id for audit and CLI output."""
    text = str(chat_id)
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:2]}***{text[-2:]}"


def mask_sender_id(sender_id: int | str | None) -> str:
    """Partially mask a sender id, or return ``-`` when it is unknown."""
    if sender_id is None:
        return "-"
    return mask_chat_id(sender_id)


def preview(text: str, *, limit: int = PREVIEW_LIMIT) -> str:
    """Collapse whitespace and truncate text for a notification or CLI line."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[: limit - 3]}..."


def _clip(text: str) -> str:
    return text[:MAX_MESSAGE_LENGTH]


def approval_message(
    *,
    item_id: int,
    platform: str,
    risk_level: str | None,
    body: str,
) -> str:
    """Build the message for a pending approval."""
    risk = risk_level or "unknown"
    return _clip(
        "Approval needed\n"
        f"Content #{item_id} on {platform} (risk {risk})\n"
        f"Preview: {preview(body)}\n"
        f"Reply /approve {item_id} or /reject {item_id}."
    )


def published_message(*, item_id: int, platform: str, external_id: str | None) -> str:
    """Build the message for a published item."""
    suffix = f" (external id {external_id})" if external_id else ""
    return _clip(f"Published: content #{item_id} on {platform}{suffix}.")


def failed_message(
    *,
    item_id: int,
    platform: str,
    attempts: int,
    error: str | None,
) -> str:
    """Build the message for a publish that exhausted every retry."""
    reason = preview(error, limit=ERROR_LIMIT) if error else "no reason recorded"
    return _clip(
        f"Publish failed after {attempts} attempts: content #{item_id} on {platform}.\n"
        f"Reason: {reason}\n"
        f"Run 'heraldesk publish {item_id}' after fixing it."
    )


def killswitch_message(*, slug: str | None, enabled: bool) -> str:
    """Build the message for a kill-switch change."""
    state = "engaged" if enabled else "cleared"
    if slug is None:
        return f"Global kill switch {state}."
    return f"Kill switch {state} for {slug}."


def help_text() -> str:
    """Return the deterministic help reply for unknown or help commands."""
    return (
        "Heraldesk commands:\n"
        "/queue - show the approval queue\n"
        "/approve <id> - approve a pending item\n"
        "/reject <id> [reason] - reject a pending item\n"
        "/help - show this message"
    )


def queue_line(
    *,
    item_id: int,
    platform: str,
    state: str,
    risk_level: str | None,
    body: str,
) -> str:
    """Format one queue entry for an inbox reply."""
    risk = risk_level or "-"
    return f"#{item_id} {platform} {state} risk={risk}\n  {preview(body, limit=100)}"


def queue_reply(lines: list[str]) -> str:
    """Build a bounded queue reply."""
    if not lines:
        return "The approval queue is empty."
    body = "Approval queue:\n" + "\n".join(lines[:10])
    if len(lines) > 10:
        body += f"\n... and {len(lines) - 10} more."
    return _clip(body)
