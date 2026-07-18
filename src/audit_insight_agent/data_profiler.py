"""Deterministic structure and data-quality profiling backed by DuckDB."""
from __future__ import annotations

from typing import Any

from .data_loader import DuckDBTableStore, quote_identifier, safe_table_name
from .models import ColumnProfile, DataProfile, SourceConfig


def _scalar(store: DuckDBTableStore, sql: str) -> Any:
    return store.connection.execute(sql).fetchone()[0]


def profile_table(store: DuckDBTableStore, source: SourceConfig) -> DataProfile:
    """Profile any registered table using only its declarative source metadata."""

    table_name = safe_table_name(source)
    table = quote_identifier(table_name)
    schema_rows = store.connection.execute(f"DESCRIBE {table}").fetchall()
    columns_and_types = [(str(row[0]), str(row[1])) for row in schema_rows]
    row_count = int(_scalar(store, f"SELECT COUNT(*) FROM {table}"))
    distinct_rows = int(
        _scalar(store, f"SELECT COUNT(*) FROM (SELECT DISTINCT * FROM {table})")
    )

    column_profiles = []
    for name, data_type in columns_and_types:
        column = quote_identifier(name)
        null_count, distinct_count, min_value, max_value = store.connection.execute(
            f"SELECT COUNT(*) FILTER (WHERE {column} IS NULL), "
            f"COUNT(DISTINCT {column}), MIN({column}), MAX({column}) FROM {table}"
        ).fetchone()
        column_profiles.append(
            ColumnProfile(
                name=name,
                data_type=data_type,
                null_count=int(null_count),
                null_ratio=(float(null_count) / row_count if row_count else 0.0),
                distinct_count=int(distinct_count),
                min_value=min_value,
                max_value=max_value,
            )
        )

    actual_fields = {name for name, _ in columns_and_types}
    missing_fields = [name for name in source.expected_fields if name not in actual_fields]

    primary_key_duplicate_count = None
    if source.primary_key and not any(key not in actual_fields for key in source.primary_key):
        keys = ", ".join(quote_identifier(key) for key in source.primary_key)
        distinct_keys = int(
            _scalar(store, f"SELECT COUNT(*) FROM (SELECT DISTINCT {keys} FROM {table})")
        )
        primary_key_duplicate_count = row_count - distinct_keys

    return DataProfile(
        source_id=source.source_id,
        table_name=table_name,
        row_count=row_count,
        column_count=len(columns_and_types),
        duplicate_row_count=row_count - distinct_rows,
        primary_key_duplicate_count=primary_key_duplicate_count,
        missing_expected_fields=missing_fields,
        columns=column_profiles,
    )
