"""Helpers for safe OpenStack driver HTTP error reporting."""

from __future__ import annotations

import re
from typing import Any

MAX_ERROR_DETAIL_LENGTH = 512

_SENSITIVE_KEY_PARTS = ("authorization", "token", "password", "secret", "client_key")
_BEARER_RE = re.compile(r"(?i)\b(authorization\s*:\s*bearer|bearer)\s+([^\s,;]+)")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?token|auth[_-]?token|token|password|secret|client[_-]?key)=([^\s,;]+)"
)


def redact_sensitive(value: Any) -> Any:
    """Return value with common credential fields redacted."""
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key).lower()
            if any(part in key_text for part in _SENSITIVE_KEY_PARTS):
                redacted[key] = "<redacted>"
            else:
                redacted[key] = redact_sensitive(item)
        return redacted
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return redact_text(value)
    return value


def redact_text(text: str) -> str:
    """Redact credentials embedded in text."""
    text = _BEARER_RE.sub(r"\1 <redacted>", text)
    return _ASSIGNMENT_RE.sub(r"\1=<redacted>", text)


def safe_error_detail(value: Any) -> str:
    """Return a redacted, bounded error detail string."""
    if not isinstance(value, str):
        value = str(redact_sensitive(value))
    detail = redact_text(value).strip()
    if len(detail) > MAX_ERROR_DETAIL_LENGTH:
        return f"{detail[:MAX_ERROR_DETAIL_LENGTH]}..."
    return detail


def response_error_message(response: Any) -> tuple[str, Any | None]:
    """Extract a safe error message and sanitized response JSON."""
    try:
        error_data = redact_sensitive(response.json())
        error_msg = None
        if isinstance(error_data, dict):
            error_msg = error_data.get("detail")
            if not error_msg:
                error = error_data.get("error")
                if isinstance(error, dict):
                    error_msg = error.get("message")
        if not error_msg:
            error_msg = getattr(response, "text", "")
        return safe_error_detail(error_msg), error_data
    except Exception:
        return safe_error_detail(getattr(response, "text", "")), None
