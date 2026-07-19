"""Public boundary of Audit Insight Agent."""

from .config import load_application_settings, load_source_catalog
from .data_loader import DuckDBTableStore
from .ingestion import ingest_catalog
from .agent import AuditInsightAgent
from .workspace import discover_workspace
from .models import DataProfile, DocumentChunk, IngestionResult, SourceConfig

__all__ = [
    "DataProfile",
    "DocumentChunk",
    "DuckDBTableStore",
    "IngestionResult",
    "SourceConfig",
    "AuditInsightAgent",
    "ingest_catalog",
    "load_application_settings",
    "discover_workspace",
    "load_source_catalog",
]
