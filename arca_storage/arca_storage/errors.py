"""
Structured error types for Arca Storage.

Provides a unified error hierarchy that maps cleanly to HTTP status codes
and gRPC codes (for CSI driver compatibility). Every error carries a
machine-readable code so that Go/Python consumers can switch on codes
instead of parsing error text.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Optional


class ErrorCode(str, Enum):
    """Machine-readable error codes shared with CSI driver via JSON API."""

    ALREADY_EXISTS = "ALREADY_EXISTS"
    UNAUTHORIZED = "UNAUTHORIZED"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    PRECONDITION_FAILED = "PRECONDITION_FAILED"
    INVALID_ARGUMENT = "INVALID_ARGUMENT"
    RESOURCE_EXHAUSTED = "RESOURCE_EXHAUSTED"
    INTERNAL = "INTERNAL"
    TIMEOUT = "TIMEOUT"
    UNAVAILABLE = "UNAVAILABLE"


_HTTP_STATUS_MAP: dict[ErrorCode, int] = {
    ErrorCode.ALREADY_EXISTS: 409,
    ErrorCode.UNAUTHORIZED: 401,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.CONFLICT: 409,
    ErrorCode.PRECONDITION_FAILED: 412,
    ErrorCode.INVALID_ARGUMENT: 400,
    ErrorCode.RESOURCE_EXHAUSTED: 429,
    ErrorCode.INTERNAL: 500,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.UNAVAILABLE: 503,
}


class ArcaError(Exception):
    """Base exception for all Arca Storage errors.

    Attributes:
        code: Machine-readable error code.
        message: Human-readable description.
        details: Optional structured context (serialised in JSON responses).
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        self.code = code
        self.message = message
        self.details = details or {}
        super().__init__(message)

    @property
    def http_status(self) -> int:
        return _HTTP_STATUS_MAP.get(self.code, 500)

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "details": self.details,
        }


# Convenience subclasses

class AlreadyExistsError(ArcaError):
    def __init__(self, resource: str, name: str) -> None:
        super().__init__(
            ErrorCode.ALREADY_EXISTS,
            f"{resource} '{name}' already exists",
            {"resource": resource, "name": name},
        )


class UnauthorizedError(ArcaError):
    def __init__(self, message: str = "Unauthorized") -> None:
        super().__init__(ErrorCode.UNAUTHORIZED, message)


class NotFoundError(ArcaError):
    def __init__(self, resource: str, name: str) -> None:
        super().__init__(
            ErrorCode.NOT_FOUND,
            f"{resource} '{name}' not found",
            {"resource": resource, "name": name},
        )


class ConflictError(ArcaError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(ErrorCode.CONFLICT, message, details)


class CreateLeaseLostError(ConflictError):
    def __init__(self, resource: str, name: str) -> None:
        super().__init__(
            f"{resource} '{name}' create lease was lost",
            {"resource": resource, "name": name},
        )


class PreconditionFailedError(ArcaError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(ErrorCode.PRECONDITION_FAILED, message, details)


class InvalidArgumentError(ArcaError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(ErrorCode.INVALID_ARGUMENT, message, details)


class InternalError(ArcaError):
    def __init__(self, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(ErrorCode.INTERNAL, message, details)


class TimeoutError(ArcaError):
    def __init__(self, operation: str, timeout_seconds: int) -> None:
        super().__init__(
            ErrorCode.TIMEOUT,
            f"Operation '{operation}' timed out after {timeout_seconds}s",
            {"operation": operation, "timeout_seconds": timeout_seconds},
        )


class SubprocessError(ArcaError):
    """Wraps a failed subprocess call with structured context."""

    def __init__(self, cmd: list[str], returncode: int, stderr: str) -> None:
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(
            ErrorCode.INTERNAL,
            f"Command failed (rc={returncode})",
            {"returncode": returncode},
        )
