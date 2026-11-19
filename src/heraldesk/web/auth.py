"""Single-admin authentication for the local Web UI and REST API.

The admin token is resolved from ``HERALDESK_ADMIN_TOKEN`` in the environment, then
``$HERALDESK_HOME/.env``, then a deterministic HMAC of ``secret.key``. It is never stored in
``heraldesk.yaml``, a log line, an audit payload, an HTML page or a cookie.

The API accepts only ``Authorization: Bearer <token>``. The UI uses a signed HttpOnly
session cookie plus a CSRF token. Cookies are ``Secure`` whenever HTTPS proxy mode is on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final
from urllib.parse import parse_qs

from dotenv import dotenv_values
from fastapi import Request

from heraldesk import paths
from heraldesk.core.crypto import CryptoError, load_key

ADMIN_TOKEN_ENV: Final = "HERALDESK_ADMIN_TOKEN"
TRUST_PROXY_ENV: Final = "HERALDESK_TRUST_PROXY"
WEBHOOK_SECRET_ENV: Final = "HERALDESK_TELEGRAM_WEBHOOK_SECRET"
SESSION_COOKIE: Final = "heraldesk_session"
LOGIN_CSRF_COOKIE: Final = "heraldesk_login_csrf"
SESSION_TTL_SECONDS: Final = 12 * 60 * 60
LOGIN_ATTEMPTS: Final = 5
LOGIN_WINDOW_SECONDS: Final = 60.0
QUERY_TOKEN_KEYS: Final = frozenset({"token", "access_token", "admin_token"})
_DERIVATION_LABEL: Final = b"heraldesk-admin-v1"
_SESSION_LABEL: Final = b"heraldesk-session-v1"

logger = logging.getLogger("heraldesk.web.auth")


class AuthConfigError(Exception):
    """Raised when no admin token can be resolved safely."""


class AuthError(Exception):
    """Base class for authentication and authorisation failures."""

    status_code = 401
    code = "unauthorized"

    def __init__(self, message: str = "authentication required") -> None:
        super().__init__(message)
        self.message = message


class Unauthorized(AuthError):
    """Missing or invalid credentials."""


class Forbidden(AuthError):
    """Valid credentials without the required CSRF token or permission."""

    status_code = 403
    code = "forbidden"


@dataclass(frozen=True, slots=True)
class UISession:
    """The signed values carried by a UI session cookie."""

    csrf: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class TokenResolution:
    """The resolved admin token and a non-secret source label."""

    token: str
    source: str


def environment_flag(name: str, *, environ: Mapping[str, str] | None = None) -> bool:
    """Return ``True`` for 1/true/yes/on, case-insensitively."""
    source = os.environ if environ is None else environ
    return source.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _dotenv_value(layout: paths.Layout, name: str) -> str | None:
    if not layout.env_path.is_file():
        return None
    try:
        values = dotenv_values(layout.env_path)
    except OSError as exc:
        raise AuthConfigError(f"cannot read {layout.env_path}: {exc}") from exc
    return _clean(values.get(name))


def _dotenv_token(layout: paths.Layout) -> str | None:
    return _dotenv_value(layout, ADMIN_TOKEN_ENV)


def explicit_admin_token(layout: paths.Layout) -> TokenResolution | None:
    """Return a token supplied by the environment or ``.env``, never the derived one."""
    from_environment = _clean(os.environ.get(ADMIN_TOKEN_ENV))
    if from_environment is not None:
        return TokenResolution(from_environment, "environment")
    from_dotenv = _dotenv_token(layout)
    if from_dotenv is not None:
        return TokenResolution(from_dotenv, ".env")
    return None


def resolve_webhook_secret(layout: paths.Layout) -> str | None:
    """Return the Telegram webhook secret from the environment or ``.env``."""
    value = _clean(os.environ.get(WEBHOOK_SECRET_ENV))
    if value is None:
        value = _dotenv_value(layout, WEBHOOK_SECRET_ENV)
    return _clean(value)


def trust_proxy_enabled(layout: paths.Layout) -> bool:
    """Return ``True`` when HTTPS proxy mode is enabled by env or ``.env``.

    A value present in the process environment wins even when it is false, matching the
    documented environment-over-``.env`` precedence.
    """
    if TRUST_PROXY_ENV in os.environ:
        return environment_flag(TRUST_PROXY_ENV)
    value = _dotenv_value(layout, TRUST_PROXY_ENV)
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def derive_admin_token(layout: paths.Layout) -> str:
    """Derive a stable admin token from ``secret.key`` when no explicit token is set."""
    try:
        key = load_key(layout.key_path)
    except CryptoError as exc:
        raise AuthConfigError(
            "no admin token is configured; set HERALDESK_ADMIN_TOKEN in the environment "
            "or .env, or run 'heraldesk init' to create secret.key"
        ) from exc
    return hmac.new(key, _DERIVATION_LABEL, hashlib.sha256).hexdigest()


def resolve_admin_token(layout: paths.Layout) -> TokenResolution:
    """Resolve the admin token with environment > .env > derived precedence."""
    explicit = explicit_admin_token(layout)
    if explicit is not None:
        return explicit
    return TokenResolution(derive_admin_token(layout), "secret.key")


def mask_token(token: str | None) -> str:
    """Return a short fingerprint safe for CLI output."""
    if not token:
        return "not configured"
    if len(token) < 8:
        return "[REDACTED]"
    return f"****{token[-4:]}"


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


@dataclass
class LoginRateLimiter:
    """In-memory failed-login limiter, one window per client key."""

    limit: int = LOGIN_ATTEMPTS
    window_seconds: float = LOGIN_WINDOW_SECONDS
    clock: Callable[[], float] = time.monotonic
    _failures: dict[str, list[float]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _recent(self, key: str, now: float) -> list[float]:
        cutoff = now - self.window_seconds
        return [stamp for stamp in self._failures.get(key, []) if stamp >= cutoff]

    def allowed(self, key: str) -> bool:
        """Return ``False`` when *key* has exhausted its window."""
        with self._lock:
            return len(self._recent(key, self.clock())) < self.limit

    def record_failure(self, key: str) -> None:
        """Record one failed attempt."""
        with self._lock:
            now = self.clock()
            recent = self._recent(key, now)
            recent.append(now)
            self._failures[key] = recent

    def reset(self, key: str) -> None:
        """Clear the window after a successful login."""
        with self._lock:
            self._failures.pop(key, None)

    def retry_after(self, key: str) -> int:
        """Return whole seconds until the oldest recent failure leaves the window."""
        with self._lock:
            recent = self._recent(key, self.clock())
            if not recent:
                return int(self.window_seconds)
            return max(1, int(self.window_seconds - (self.clock() - min(recent))) + 1)


@dataclass(frozen=True, slots=True)
class AdminAuth:
    """Token verification, signed UI sessions and CSRF checks."""

    token: str
    cookie_secure: bool = False
    trust_proxy: bool = False
    session_ttl_seconds: int = SESSION_TTL_SECONDS
    rate_limiter: LoginRateLimiter = field(default_factory=LoginRateLimiter)

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise AuthConfigError("admin token must not be empty")

    @property
    def signing_key(self) -> bytes:
        return hmac.new(self.token.encode("utf-8"), _SESSION_LABEL, hashlib.sha256).digest()

    def _sign(self, value: str) -> str:
        signature = hmac.new(self.signing_key, value.encode("ascii"), hashlib.sha256).digest()
        return _b64encode(signature)

    def _signed(self, value: str) -> str:
        return f"{value}.{self._sign(value)}"

    def _verified(self, signed: str | None) -> str | None:
        if not signed or "." not in signed:
            return None
        value, _, signature = signed.rpartition(".")
        if not hmac.compare_digest(signature, self._sign(value)):
            return None
        return value

    def verify_bearer(self, header: str | None) -> bool:
        """Return ``True`` for a correct ``Bearer`` header."""
        if not header:
            return False
        scheme, _, value = header.partition(" ")
        if scheme.lower() != "bearer" or not value.strip():
            return False
        return hmac.compare_digest(value.strip().encode(), self.token.encode())

    def verify_webhook_secret(self, header: str | None, expected: str | None) -> bool:
        """Compare an optional Telegram webhook secret in constant time."""
        if expected is None:
            return True
        if not header:
            return False
        return hmac.compare_digest(header.encode(), expected.encode())

    def issue_login_csrf(self) -> tuple[str, str]:
        """Return ``(hidden_form_value, signed_cookie_value)`` for the login form."""
        raw = secrets.token_urlsafe(24)
        return raw, self._signed(raw)

    def verify_login_csrf(self, cookie_value: str | None, form_value: str | None) -> bool:
        """Verify the login CSRF cookie and hidden field."""
        if not cookie_value or not form_value:
            return False
        signed = self._verified(cookie_value)
        return signed is not None and hmac.compare_digest(signed, form_value)

    def issue_session(self, *, now: datetime | None = None) -> tuple[UISession, str]:
        """Create a signed session and return it with the cookie value."""
        current = datetime.now(UTC) if now is None else now
        expires = current + timedelta(seconds=self.session_ttl_seconds)
        session = UISession(
            csrf=secrets.token_urlsafe(24),
            issued_at=current,
            expires_at=expires,
        )
        payload = {
            "v": 1,
            "iat": int(current.timestamp()),
            "exp": int(expires.timestamp()),
            "csrf": session.csrf,
        }
        encoded = _b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return session, self._signed(encoded)

    def read_session(
        self,
        cookie_value: str | None,
        *,
        now: datetime | None = None,
    ) -> UISession | None:
        """Return the session when the cookie is signed, valid and unexpired."""
        value = self._verified(cookie_value)
        if value is None:
            return None
        try:
            payload = json.loads(_b64decode(value))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, Mapping) or payload.get("v") != 1:
            return None
        try:
            issued = datetime.fromtimestamp(int(payload["iat"]), tz=UTC)
            expires = datetime.fromtimestamp(int(payload["exp"]), tz=UTC)
            csrf = str(payload["csrf"])
        except (KeyError, TypeError, ValueError, OverflowError):
            return None
        current = datetime.now(UTC) if now is None else now
        if expires <= current or issued > current:
            return None
        return UISession(csrf=csrf, issued_at=issued, expires_at=expires)

    def verify_csrf(self, session: UISession, form_value: str | None) -> bool:
        """Verify a UI form CSRF value against the signed session."""
        if not form_value:
            return False
        return hmac.compare_digest(session.csrf, form_value)

    def client_key(self, request: Request) -> str:
        """Return the rate-limit key, trusting forwarded IPs only behind a proxy."""
        if self.trust_proxy:
            forwarded = request.headers.get("x-forwarded-for", "")
            first = forwarded.split(",", 1)[0].strip()
            if first:
                return first
        return request.client.host if request.client is not None else "unknown"


def token_in_query(query_string: bytes | str) -> bool:
    """Return ``True`` when a sensitive token key appears in a query string."""
    raw = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
    return any(key in parse_qs(raw, keep_blank_values=True) for key in QUERY_TOKEN_KEYS)
