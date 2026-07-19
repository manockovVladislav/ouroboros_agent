from __future__ import annotations

import pandas as pd
import pytest

from audit_insight_agent import data_loader
from audit_insight_agent.config import load_source_catalog
from audit_insight_agent.data_loader import DuckDBTableStore, _database_query
from audit_insight_agent.document_loader import chunk_text
from audit_insight_agent.ingestion import ingest_catalog
from audit_insight_agent.models import DatabaseSettings, SourceConfig


def test_configured_table_is_loaded_and_profiled(tmp_path):
    (tmp_path / "anything.csv").write_text(
        "record_id,value\n1,10\n1,10\n2,\n", encoding="utf-8"
    )
    config = tmp_path / "sources.yaml"
    config.write_text(
        """version: 1
sources:
  - source_id: arbitrary_source
    source_type: table
    location: anything.csv
    expected_fields: [record_id, value, missing_field]
    primary_key: [record_id]
""",
        encoding="utf-8",
    )

    catalog = load_source_catalog(config)
    assert catalog.sources[0].source_id == "arbitrary_source"

    with DuckDBTableStore() as store:
        result = ingest_catalog(config, store)
        assert store.query("SELECT SUM(value) AS total FROM arbitrary_source").iloc[0, 0] == 20

    profile = result.profiles[0]
    assert profile.row_count == 3
    assert profile.duplicate_row_count == 1
    assert profile.primary_key_duplicate_count == 1
    assert profile.missing_expected_fields == ["missing_field"]
    assert next(column for column in profile.columns if column.name == "value").null_count == 1


def test_document_chunks_have_stable_ids_and_overlap():
    text = "alpha beta gamma delta epsilon zeta eta theta"
    first = chunk_text("policy", text, chunk_size=20, chunk_overlap=5)
    second = chunk_text("policy", text, chunk_size=20, chunk_overlap=5)

    assert len(first) > 1
    assert [chunk.chunk_id for chunk in first] == [chunk.chunk_id for chunk in second]
    assert first[1].start_char < first[0].end_char


def test_registered_postgresql_source_streams_into_duckdb(monkeypatch):
    source = SourceConfig(
        source_id="remote_entries",
        source_type="table",
        location="audit.entries",
        format="postgresql",
        connection="$selected",
    )
    settings = DatabaseSettings(
        connections={
            "replica_a": {
                "host": "db.local",
                "database": "audit",
                "user": "readonly",
                "password_env": "TEST_DB_PASSWORD",
            }
        }
    )

    def batches(config, database_settings, selected_replica, limit=None):
        assert selected_replica == "replica_a"
        yield pd.DataFrame({"id": [1, 2], "amount": [10, 20]})
        yield pd.DataFrame({"id": [3], "amount": [30]})

    monkeypatch.setattr(data_loader, "iter_database_batches", batches)
    with DuckDBTableStore(
        database_settings=settings, selected_replica="replica_a"
    ) as store:
        store.ingest(source, "unused.yaml")
        total = store.query("SELECT SUM(amount) AS total FROM remote_entries")
    assert total.iloc[0, 0] == 60


def test_database_query_rejects_mutation_and_quotes_relation():
    relation = SourceConfig(
        source_id="remote",
        source_type="table",
        location="audit.entries",
        format="greenplum",
        connection="replica_a",
    )
    assert _database_query(relation) == 'SELECT * FROM "audit"."entries"'

    mutation = relation.model_copy(update={"query": "WITH x AS (DELETE FROM t) SELECT 1"})
    with pytest.raises(ValueError, match="read-only"):
        _database_query(mutation)
