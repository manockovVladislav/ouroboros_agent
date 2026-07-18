from pathlib import Path

import pytest

from audit_insight_agent.agent import AuditInsightAgent
from audit_insight_agent.case_package import load_case_package, select_relevant_rules
from audit_insight_agent.evidence_store import EvidenceStore
from audit_insight_agent.models import RuleStatus


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_auditor_query_selects_only_relevant_rule_sources():
    package = load_case_package(PROJECT_ROOT / "cases" / "physical_currency_ovp")
    selected = select_relevant_rules("Проверить только лимит ОВП", package.rules)
    assert [rule.rule_id for rule in selected] == ["OVP_LIMIT_EXCEEDED"]


def test_physical_currency_case_runs_without_case_logic_in_core(tmp_path):
    if not (PROJECT_ROOT / "data" / "ovp" / "ovp_snapshots.csv").exists():
        pytest.skip("External synthetic data is not present in data/")
    result, paths = AuditInsightAgent("test").run_case(
        case_dir=PROJECT_ROOT / "cases" / "physical_currency_ovp",
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

    reference = result.findings[0].evidence[0]
    stored = EvidenceStore(tmp_path / "RUN-INTEGRATION" / "evidence").get(
        reference.evidence_id
    )
    assert stored.checksum == reference.checksum
    assert stored.rule_hash
