import json
from pathlib import Path

import pytest

from audit_insight_agent.agent import AuditInsightAgent
from audit_insight_agent.workspace import discover_workspace, select_relevant_rules
from audit_insight_agent.evidence_store import EvidenceStore
from audit_insight_agent.models import RuleStatus


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_auditor_query_selects_only_relevant_rule_sources():
    workspace = discover_workspace(PROJECT_ROOT)
    selected = select_relevant_rules("Проверить только лимит ОВП", workspace.rules)
    assert [rule.rule_id for rule in selected] == ["OVP_LIMIT_EXCEEDED"]


def test_physical_currency_rules_run_on_discovered_workspace(tmp_path):
    if not (PROJECT_ROOT / "data" / "ovp" / "ovp_snapshots.csv").exists():
        pytest.skip("External synthetic data is not present in data/")
    result, paths = AuditInsightAgent("test").run_workspace(
        workspace=discover_workspace(PROJECT_ROOT),
        auditor_query="Проверить физическую валюту, ОВП, последовательности и аномалии",
        output_root=tmp_path,
        run_id="RUN-INTEGRATION",
    )

    assert not result.execution_errors
    assert {item.status for item in result.rule_results} <= {
        RuleStatus.PASS,
        RuleStatus.FAIL,
    }
    assert {item.rule_id for item in result.rule_results} == {
        "CASH_AMOUNT_ANOMALY",
        "CASH_SHIPMENT_SEQUENCE",
        "OVP_LIMIT_EXCEEDED",
        "PHYSICAL_OVP_RECONCILIATION",
    }
    assert result.findings
    assert paths["candidate_findings"].exists()
    assert paths["report"].exists()
    assert paths["discovered_sources"].exists()
    assert paths["profiles"].exists()
    assert paths["data_dependencies"].exists()
    assert paths["business_analysis"].exists()
    events_path = tmp_path / "RUN-INTEGRATION" / "events.jsonl"
    events = [json.loads(line) for line in events_path.read_text("utf-8").splitlines()]
    event_names = {event["event"] for event in events}
    assert {"audit_started", "rules_selected", "rule_completed"} <= event_names
    manifest = json.loads(paths["run_manifest"].read_text("utf-8"))
    assert manifest["files"]["events"] == "events.jsonl"
    assert manifest["files"]["business_analysis"] == "business_analysis.json"

    reference = result.findings[0].evidence[0]
    stored = EvidenceStore(tmp_path / "RUN-INTEGRATION" / "evidence").get(
        reference.evidence_id
    )
    assert stored.checksum == reference.checksum
    assert stored.rule_hash
