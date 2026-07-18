"""Case-agnostic orchestration for tables, profiles and document indexing."""
from __future__ import annotations

from pathlib import Path

from .config import load_source_catalog
from .data_loader import DuckDBTableStore
from .data_profiler import profile_table
from .document_loader import load_document_chunks
from .models import IngestionResult
from .retriever import QdrantRetriever


def ingest_catalog(
    config_path: str | Path,
    table_store: DuckDBTableStore,
    retriever: QdrantRetriever | None = None,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
) -> IngestionResult:
    """Run the complete first-stage flow described by a source registry."""

    catalog = load_source_catalog(config_path)
    registered_tables: dict[str, str] = {}
    profiles = []
    indexed_chunks = 0

    for source in catalog.sources:
        if not source.enabled:
            continue
        if source.source_type == "table":
            table_name = table_store.ingest(source, config_path)
            registered_tables[source.source_id] = table_name
            profiles.append(profile_table(table_store, source))
        elif source.source_type == "document":
            if retriever is None:
                raise ValueError(
                    f"Document source {source.source_id!r} requires a retriever"
                )
            chunks = load_document_chunks(
                source,
                config_path,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
            )
            indexed_chunks += retriever.index(chunks)

    return IngestionResult(
        registered_tables=registered_tables,
        profiles=profiles,
        indexed_chunks=indexed_chunks,
    )
