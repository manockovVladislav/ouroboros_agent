from pathlib import Path

from audit_insight_agent.ouroboros import OuroborosOrchestrator
from audit_insight_agent.models import ApplicationSettings, OuroborosSettings
from audit_insight_agent.ouroboros_tools import (
    generate_report,
    list_data_sources,
    profile_data_source,
    run_rule,
)
from audit_insight_agent.web import available_cases, build_interface


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_public_api_lists_profiles_and_runs_one_rule(tmp_path, monkeypatch):
    if not (PROJECT_ROOT / "data/ovp/portfolio_reference.csv").exists():
        return
    monkeypatch.setenv("AUDIT_AGENT_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("AUDIT_AGENT_ALLOWED_DATA_ROOT", str(PROJECT_ROOT / "data"))

    sources = list_data_sources("physical_currency_ovp")
    assert any(item["source_id"] == "ovp_snapshots" for item in sources)
    profile = profile_data_source("physical_currency_ovp", "portfolio_reference")
    assert profile["row_count"] > 0

    result = run_rule(
        "physical_currency_ovp",
        "OVP_LIMIT_EXCEEDED",
        run_id="RUN-API",
    )
    assert result["status"] == "COMPLETED"
    assert [item["rule_id"] for item in result["rule_results"]] == ["OVP_LIMIT_EXCEEDED"]
    assert Path(generate_report("RUN-API")["report_path"]).is_file()


def test_web_case_discovery_and_answer_summary():
    assert "physical_currency_ovp" in available_cases()
    answer = OuroborosOrchestrator._answer(
        {
            "status": "COMPLETED",
            "findings": [{"severity": "HIGH", "title": "Test finding"}],
            "execution_errors": [],
            "evaluation": {"status": "NOT_CONFIGURED"},
        }
    )
    assert "HIGH: 1" in answer
    assert "audit-evaluator не настроен" in answer


def test_gradio_interface_builds_without_starting_server():
    interface = build_interface()
    assert interface.__class__.__name__ == "Blocks"


def test_web_orchestrator_uses_external_ouroboros_task_api(tmp_path):
    class FakeClient:
        def __init__(self):
            self.polls = 0
            self.description = ""

        def health(self):
            return {"ok": True}

        def create_task(self, description, workspace, timeout_seconds):
            self.description = description
            assert workspace == str(tmp_path.resolve())
            assert timeout_seconds == 60
            return {"ok": True, "task_id": "task-1"}

        def get_task(self, task_id):
            assert task_id == "task-1"
            self.polls += 1
            if self.polls == 1:
                return {"status": "running"}
            return {"status": "completed", "result": "AUDIT_RUN_ID=RUN-EXTERNAL"}

    fake = FakeClient()
    settings = ApplicationSettings(
        ouroboros=OuroborosSettings(
            workspace=str(tmp_path),
            timeout_seconds=60,
            poll_interval_seconds=0.1,
        )
    )
    loaded = {
        "run_id": "RUN-EXTERNAL",
        "status": "COMPLETED",
        "findings_count": 0,
        "findings": [],
        "execution_errors": [],
        "candidate_findings_path": str(tmp_path / "candidate_findings.json"),
        "report_path": str(tmp_path / "report.md"),
    }
    orchestrator = OuroborosOrchestrator(
        settings=settings,
        client=fake,
        result_loader=lambda run_id: dict(loaded),
    )
    events = list(orchestrator.run_with_updates("Проверить", "demo"))

    assert events[-1]["result"]["run_id"] == "RUN-EXTERNAL"
    assert "scripts/ouroboros_audit.py" in fake.description
    assert str(tmp_path / ".venv/bin/python") in fake.description
