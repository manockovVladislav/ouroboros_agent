from __future__ import annotations

import pandas as pd

from audit_insight_agent.business_analyzer import (
    analyze_business_logic,
    business_hypothesis_plan_items,
)
from audit_insight_agent.data_loader import DuckDBTableStore
from audit_insight_agent.data_profiler import profile_table
from audit_insight_agent.dependency_analyzer import analyze_data_dependencies
from audit_insight_agent.models import SourceConfig


def _register(store, name, frame):
    temporary = f"_{name}"
    store.connection.register(temporary, frame)
    store.connection.execute(f"CREATE TABLE {name} AS SELECT * FROM {temporary}")
    store.connection.unregister(temporary)


def _source(name):
    return SourceConfig(
        source_id=name,
        source_type="table",
        location=f"{name}.csv",
        table_name=name,
    )


def test_business_analysis_finds_generic_target_attribute_mismatch_and_lineage():
    mapping_source = _source("routing_rules")
    reference_source = _source("account_reference")
    event_source = _source("processed_events")
    with DuckDBTableStore() as store:
        _register(
            store,
            "routing_rules",
            pd.DataFrame(
                {
                    "rule_id": [f"R{i}" for i in range(1, 12)],
                    "target_account_id": [f"A{i}" for i in range(1, 12)],
                    "asset_class": ["PHYSICAL"] * 10 + ["NON_CASH"],
                    "valid_from": ["2026-01-01"] * 11,
                }
            ),
        )
        _register(
            store,
            "account_reference",
            pd.DataFrame(
                {
                    "account_id": [f"A{i}" for i in range(1, 12)],
                    "asset_class": ["PHYSICAL"] * 9 + ["NON_CASH"] * 2,
                }
            ),
        )
        _register(
            store,
            "processed_events",
            pd.DataFrame(
                {
                    "event_id": [f"E{i}" for i in range(1, 12)],
                    "mapping_rule_id": [f"R{i}" for i in range(1, 12)],
                }
            ),
        )
        profiles = [
            profile_table(store, mapping_source),
            profile_table(store, reference_source),
            profile_table(store, event_source),
        ]
        dependencies = analyze_data_dependencies(
            store,
            {
                "routing_rules": "routing_rules",
                "account_reference": "account_reference",
                "processed_events": "processed_events",
            },
            profiles,
            (),
        )
        analysis = analyze_business_logic(store, profiles, dependencies, [])

    hypotheses = analysis["semantic_hypotheses"]
    assert len(hypotheses) == 1
    hypothesis = hypotheses[0]
    assert hypothesis["mapping_source"] == "routing_rules"
    assert hypothesis["reference_source"] == "account_reference"
    assert hypothesis["attribute"] == "asset_class"
    assert hypothesis["mismatch_rows"] == 1
    assert hypothesis["pattern"] == "OUTLIER"
    assert hypothesis["samples"][0]["mapping_target_key"] == "A10"
    assert "IS DISTINCT FROM" in hypothesis["reproduction_query"]
    assert any(
        path["sources"][-1] == "processed_events"
        for path in hypothesis["candidate_impact_paths"]
    )

    plan = business_hypothesis_plan_items(analysis)
    assert len(plan) == 1
    assert plan[0].status == "POTENTIAL_RISK"
    assert plan[0].finding_id is None
