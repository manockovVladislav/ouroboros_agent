"""Validated logging configuration loader."""
from __future__ import annotations

import logging.config
import os
from pathlib import Path

import yaml


_CONFIGURED = False


def configure_logging(config_path: str | Path, force: bool = False) -> None:
    global _CONFIGURED
    if _CONFIGURED and not force:
        return
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Logging configuration not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Logging configuration root must be a mapping")
    if raw.get("version") != 1:
        raise ValueError("Logging configuration version must be 1")
    project_root = path.parent.parent
    log_root = Path(os.getenv("AUDIT_LOG_DIR", str(project_root / "logs"))).expanduser().resolve()
    for handler in (raw.get("handlers") or {}).values():
        if not isinstance(handler, dict) or "filename" not in handler:
            continue
        filename = Path(str(handler["filename"])).expanduser()
        if not filename.is_absolute():
            filename = log_root / filename.name
        filename.parent.mkdir(parents=True, exist_ok=True)
        handler["filename"] = str(filename.resolve())
    logging.config.dictConfig(raw)
    _CONFIGURED = True
