"""Persistent, redacted event history for audit runs."""
from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


_WRITE_LOCK = threading.Lock()
_SENSITIVE_PARTS = ("password", "secret", "token", "api_key", "authorization")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize(value: Any, key: str = "") -> Any:
    """Redact secrets and bound values before they enter durable logs."""

    normalized_key = key.casefold()
    if any(part in normalized_key for part in _SENSITIVE_PARTS):
        return "***REDACTED***"
    if isinstance(value, dict):
        return {str(item_key): sanitize(item, str(item_key)) for item_key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [sanitize(item) for item in value]
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, str):
        return value if len(value) <= 4000 else value[:4000] + "…[truncated]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return sanitize(str(value), key)


def append_jsonl(path: str | Path, payload: dict[str, Any]) -> Path:
    target = Path(path).expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(sanitize(payload), ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    with _WRITE_LOCK:
        descriptor = os.open(target, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, encoded)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return target


class RunEventLogger:
    """Append-only JSONL history correlated by run_id."""

    def __init__(self, run_dir: str | Path, run_id: str) -> None:
        self.run_dir = Path(run_dir).expanduser().resolve()
        self.run_id = run_id
        self.path = self.run_dir / "events.jsonl"
        self.application_logger = logging.getLogger("audit_insight.run")

    def event(self, event: str, level: str = "INFO", **details: Any) -> Path:
        record = {
            "timestamp": utc_now(),
            "level": level.upper(),
            "run_id": self.run_id,
            "event": event,
            "details": details,
        }
        path = append_jsonl(self.path, record)
        numeric_level = getattr(logging, level.upper(), logging.INFO)
        self.application_logger.log(
            numeric_level,
            "run_id=%s event=%s details=%s",
            self.run_id,
            event,
            json.dumps(sanitize(details), ensure_ascii=False, default=str),
        )
        return path

    def exception(self, event: str, error: BaseException, **details: Any) -> Path:
        return self.event(
            event,
            level="ERROR",
            error_type=type(error).__name__,
            error=str(error),
            **details,
        )


def write_chat_history(
    run_dir: str | Path,
    *,
    run_id: str,
    task_id: str,
    user_request: str,
    ouroboros_answer: str,
) -> Path:
    path = Path(run_dir).expanduser().resolve() / "chat.json"
    payload = {
        "run_id": run_id,
        "ouroboros_task_id": task_id,
        "saved_at": utc_now(),
        "messages": [
            {"role": "user", "content": user_request},
            {"role": "assistant", "content": ouroboros_answer},
        ],
    }
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(sanitize(payload), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path
