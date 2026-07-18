from pathlib import Path

from audit_insight_agent.agent import AuditInsightAgent
from audit_insight_agent.document_loader import load_document_chunks
from audit_insight_agent.workspace import discover_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_trading_robot_rule_finds_logged_limit_bypass(tmp_path):
    result, paths = AuditInsightAgent().run_workspace(
        discover_workspace(PROJECT_ROOT),
        "Проверить лимиты, код роботов и требования Confluence",
        tmp_path,
    )

    assert len(result.findings) == 1
    finding = result.findings[0]
    assert finding.object_id == "ORD-1002"
    assert finding.severity.value == "CRITICAL"
    assert finding.facts["calculated_notional_usd"] == 12500.0
    assert result.finding_reviews[0].verdict == "CONFIRMED"
    assert any(
        "robot_orders.csv" in location
        for location in result.audit_plan[0].source_locations
    )
    report = paths["report"].read_text(encoding="utf-8")
    assert "ORD-1002" in report
    assert "## Prioritized audit plan" in report


def test_cpp_java_and_confluence_are_readable_document_sources():
    workspace = discover_workspace(PROJECT_ROOT)
    documents = [
        source for source in workspace.sources.sources if source.source_type == "document"
    ]
    by_path = {
        source.metadata["relative_path"]: source
        for source in documents
        if source.metadata.get("origin") == "data"
    }
    config = workspace.source_config_path

    cpp_text = "\n".join(
        chunk.text
        for chunk in load_document_chunks(by_path["robots/cpp/src/risk_guard.cpp"], config)
    )
    java_text = "\n".join(
        chunk.text
        for chunk in load_document_chunks(
            by_path["robots/java/src/main/java/com/example/trading/RiskGuard.java"], config
        )
    )
    confluence_text = "\n".join(
        chunk.text
        for chunk in load_document_chunks(
            by_path["robots/docs/confluence-risk-controls.html"], config
        )
    )

    assert "quantity / 100" in cpp_text
    assert "CENTS_PER_DOLLAR" in java_text
    assert "notional_usd = quantity * (price_cents / 100.0)" in confluence_text
