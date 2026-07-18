"""Validated logging configuration loader."""
from __future__ import annotations

import logging.config
from pathlib import Path

import yaml


def configure_logging(config_path: str | Path) -> None:
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Logging configuration not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError("Logging configuration root must be a mapping")
    if raw.get("version") != 1:
        raise ValueError("Logging configuration version must be 1")
    logging.config.dictConfig(raw)
