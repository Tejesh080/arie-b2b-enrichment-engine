from __future__ import annotations

import pytest

from arie_mcp.errors import (
    DbUnavailableError,
    ErrorCode,
    InternalToolError,
    InvalidInputError,
    NotFoundError,
    TimeoutToolError,
    ToolError,
    UpstreamUnavailableError,
)


@pytest.mark.parametrize(
    ("exc_cls", "expected_code"),
    [
        (NotFoundError, ErrorCode.NOT_FOUND),
        (InvalidInputError, ErrorCode.INVALID_INPUT),
        (TimeoutToolError, ErrorCode.TIMEOUT),
        (DbUnavailableError, ErrorCode.DB_UNAVAILABLE),
        (UpstreamUnavailableError, ErrorCode.UPSTREAM_UNAVAILABLE),
        (InternalToolError, ErrorCode.INTERNAL),
    ],
)
def test_each_error_carries_its_own_code(
    exc_cls: type[ToolError], expected_code: ErrorCode
) -> None:
    exc = exc_cls("something specific went wrong")
    assert exc.error_code == expected_code
    assert exc.message == "something specific went wrong"
    assert isinstance(exc, ToolError)
