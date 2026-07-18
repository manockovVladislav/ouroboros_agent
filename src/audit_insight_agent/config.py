"""Validated, path-safe YAML source registry."""
from __future__ import annotations

import os
from pathlib import Path

import yaml

from .models import ApplicationSettings, SourceCatalog, SourceConfig


def _load_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Configuration not found: {path}")
    raw_text = os.path.expandvars(path.read_text(encoding="utf-8"))
    raw = yaml.safe_load(raw_text) or {}
    if not isinstance(raw, dict):
        raise ValueError("Configuration root must be a mapping")
    return raw


def load_application_settings(config_path: str | Path) -> ApplicationSettings:
    """Load shared model, Qdrant, DuckDB and chunking settings."""

    path = Path(config_path).expanduser().resolve()
    return ApplicationSettings.model_validate(_load_yaml(path))


def load_source_catalog(config_path: str | Path) -> SourceCatalog:
    """Load a YAML registry and validate it before any source is accessed."""

    path = Path(config_path).expanduser().resolve()
    raw = _load_yaml(path)
    catalog = SourceCatalog.model_validate(raw)
    expanded_sources = []
    for source in catalog.sources:
        values = source.model_dump()
        values["location"] = os.path.expandvars(source.location)
        expanded_sources.append(SourceConfig.model_validate(values))
    return SourceCatalog(version=catalog.version, sources=expanded_sources)


def resolve_source_location(source: SourceConfig, config_path: str | Path) -> Path:
    """Resolve local paths relative to the registry, without cwd coupling."""

    location = Path(source.location).expanduser()
    if not location.is_absolute():
        location = Path(config_path).expanduser().resolve().parent / location
    return location.resolve()
