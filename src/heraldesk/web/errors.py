"""Web/API error types and JSON/text response helpers."""

from __future__ import annotations

from dataclasses import dataclass

from fastapi.responses import JSONResponse


@dataclass
class WebError(Exception):
    """An expected user-facing failure with a stable machine code."""

    status_code: int
    code: str
    message: str

    def __post_init__(self) -> None:
        super().__init__(self.message)


class NotFound(WebError):
    """The requested record does not exist or is not visible."""

    def __init__(self, message: str = "not found") -> None:
        super().__init__(404, "not_found", message)


class BadRequest(WebError):
    """The request is malformed or violates a domain rule."""

    def __init__(self, message: str, code: str = "bad_request") -> None:
        super().__init__(400, code, message)


class Conflict(WebError):
    """The record exists but is not in a state that allows the action."""

    def __init__(self, message: str, code: str = "conflict") -> None:
        super().__init__(409, code, message)


def json_error(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Return the single documented API error shape."""
    return JSONResponse(
        {"error": message, "code": code},
        status_code=status_code,
        headers=headers,
    )
