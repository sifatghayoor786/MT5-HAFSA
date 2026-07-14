"""Credential redaction.

A process-wide log-record factory wrapper guarantees that no registered secret,
password-shaped assignment, or full account login ever reaches a log handler.
Installed before any MT5 code runs (enforced by tests and the supervisor).
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Any

_PASSWORD_RE = re.compile(
    r"(?i)((?:password|passwd|pwd|secret|token|phrase)\s*[\"']?\s*[=:]\s*[\"']?)([^\s\"',;]+)"
)
_LOGIN_RE = re.compile(r"(?i)(login\D{0,4})(\d{5,})")

_lock = threading.Lock()
_secrets: set[str] = set()
_installed = False


def mask_login(login: int | str) -> str:
    s = str(login)
    if len(s) <= 4:
        return "*" * len(s)
    return "*" * (len(s) - 4) + s[-4:]


def register_secret(value: str | None) -> None:
    """Register a literal secret value; occurrences anywhere in logs are masked."""
    if value and len(value) >= 4:
        with _lock:
            _secrets.add(value)


def clear_secrets() -> None:
    with _lock:
        _secrets.clear()


def redact(text: str) -> str:
    with _lock:
        secrets = list(_secrets)
    for s in secrets:
        if s in text:
            text = text.replace(s, "***")
    text = _PASSWORD_RE.sub(r"\1***", text)
    text = _LOGIN_RE.sub(lambda m: m.group(1) + mask_login(m.group(2)), text)
    return text


def install_redaction() -> None:
    """Wrap the global LogRecord factory so every record is redacted at creation."""
    global _installed
    with _lock:
        if _installed:
            return
        _installed = True
    old_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = redact(message)
        record.args = ()
        return record

    logging.setLogRecordFactory(factory)


def redaction_installed() -> bool:
    return _installed
