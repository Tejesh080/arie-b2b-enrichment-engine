"""The typed error model (spec § 10).

Every tool failure is one of these codes, never a bare exception message.
A tool body raises a :class:`ToolError` subclass; ``envelope.run_tool``
(the single place every tool call passes through) catches it and builds the
``ToolResult`` the caller actually sees. Nothing else may construct a
``ToolResult`` with ``ok=False`` directly, so there is exactly one place in
this codebase that decides what an error looks like on the wire.
"""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    NOT_FOUND = "NOT_FOUND"
    INVALID_INPUT = "INVALID_INPUT"
    TIMEOUT = "TIMEOUT"
    DB_UNAVAILABLE = "DB_UNAVAILABLE"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    INTERNAL = "INTERNAL"


class ToolError(Exception):
    """Base for every typed tool failure. ``message`` must never contain a
    connection string, credential, or raw exception text — see each
    subclass's raise site for what it's allowed to say."""

    error_code: ErrorCode

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(ToolError):
    error_code = ErrorCode.NOT_FOUND


class InvalidInputError(ToolError):
    error_code = ErrorCode.INVALID_INPUT


class TimeoutToolError(ToolError):
    error_code = ErrorCode.TIMEOUT


class DbUnavailableError(ToolError):
    error_code = ErrorCode.DB_UNAVAILABLE


class UpstreamUnavailableError(ToolError):
    error_code = ErrorCode.UPSTREAM_UNAVAILABLE


class InternalToolError(ToolError):
    error_code = ErrorCode.INTERNAL
