from __future__ import annotations

import pandas as pd

from audit_insight_agent.data_loader import DuckDBTableStore
from audit_insight_agent.data_profiler import profile_table
from audit_insight_agent.dependency_analyzer import analyze_data_dependencies
from audit_insight_agent.models import AuditRule, SourceConfig


def _register(store, name, frame):
    store.connection.register(f"_{name}", frame)
    store.connection.execute(f"CREATE TABLE {name} AS SELECT * FROM _{name}")
    store.connection.unregister(f"_{name}")


def test_dependency_analysis_infers_links_and_checks_rule_vocabulary():
    left_source = SourceConfig(
        source_id="orders",
        source_type="table",
        location="orders.csv",
        table_name="orders",
    )
    right_source = SourceConfig(
        source_id="events",
        source_type="table",
        location="events.csv",
        table_name="events",
    )
    rule = AuditRule.model_validate(
        {
            "rule_id": "EVENT_SEQUENCE",
            "kind": "timeline",
            "source_ids": ["events"],
            "timeline": {
                "source_id": "events",
                "entity_fields": ["order_id"],
                "timestamp_field": "event_ts",
                "event_field": "event_type",
                "expected_order": ["CREATED", "SETTLED"],
            },
            "finding": {
                "title": "Unexpected event",
                "summary": "Unexpected event",
                "issue_type": "sequence",
                "root_cause": "Unknown",
                "criterion": "Events follow the lifecycle",
                "risk": "Incomplete processing",
                "severity": "MEDIUM",
            },
        }
    )
    with DuckDBTableStore() as store:
        _register(store, "orders", pd.DataFrame({"order_id": ["A", "B", "C"]}))
        _register(
            store,
            "events",
            pd.DataFrame(
                {
                    "order_id": ["A", "B", "C"],
                    "event_ts": ["2026-01-01", "2026-01-02", "2026-01-03"],
                    "event_type": ["CREATED", "SETTLED", "LEGACY_CODE"],
                }
            ),
        )
        profiles = [
            profile_table(store, left_source),
            profile_table(store, right_source),
        ]
        analysis = analyze_data_dependencies(
            store,
            {"orders": "orders", "events": "events"},
            profiles,
            (rule,),
        )

    relationship = analysis["inferred_relationships"][0]
    assert relationship["left_column"] == "order_id"
    assert relationship["right_column"] == "order_id"
    assert relationship["dependency_type"] == "join_key_candidate"
    assert relationship["relationship"] == "one_to_one"
    applicability = analysis["rule_applicability"][0]
    assert applicability["status"] == "PARTIAL"
    assert applicability["row_coverage"] == 0.666667
    assert {item["value"] for item in applicability["observed_values"]} == {
        "CREATED",
        "SETTLED",
        "LEGACY_CODE",
    }
