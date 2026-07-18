from pathlib import Path

import pytest

from audit_insight_agent.agent_system import AuditAgentSystem
from audit_insight_agent import developer_orchestrator as developer_module
from audit_insight_agent.developer_orchestrator import OuroborosDeveloperOrchestrator
from audit_insight_agent.ouroboros import OuroborosOrchestrator
from audit_insight_agent.models import ApplicationSettings, OuroborosSettings
from audit_insight_agent.ouroboros_tools import (
    generate_report,
    list_data_sources,
    profile_data_source,
    run_rule,
)
from audit_insight_agent.web import build_interface


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_public_api_lists_profiles_and_runs_one_rule(tmp_path, monkeypatch):
    if not (PROJECT_ROOT / "data/ovp/portfolio_reference.csv").exists():
        return
    monkeypatch.setenv("AUDIT_AGENT_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv("AUDIT_AGENT_ALLOWED_DATA_ROOT", str(PROJECT_ROOT / "data"))

    sources = list_data_sources()
    assert any(item["source_id"] == "ovp_snapshots" for item in sources)
    profile = profile_data_source("portfolio_reference")
    assert profile["row_count"] > 0

    result = run_rule(
        "OVP_LIMIT_EXCEEDED",
        run_id="RUN-API",
    )
    assert result["status"] == "COMPLETED"
    assert [item["rule_id"] for item in result["rule_results"]] == ["OVP_LIMIT_EXCEEDED"]
    assert "finding_reviews" in result
    assert "audit_plan" in result
    assert all(item["verdict"] == "CONFIRMED" for item in result["finding_reviews"])
    report_path = Path(generate_report("RUN-API")["report_path"])
    report = report_path.read_text(encoding="utf-8")
    assert "## Evidence critique" in report
    assert "## Prioritized audit plan" in report
    if result["finding_reviews"]:
        assert "## Confirmed findings" in report
        assert "Document/location:" in report


def test_web_answer_summary():
    answer = OuroborosOrchestrator._answer(
        {
            "status": "COMPLETED",
            "findings": [{"severity": "HIGH", "title": "Test finding"}],
            "execution_errors": [],
        }
    )
    assert "HIGH: 1" in answer


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
    events = list(orchestrator.run_with_updates("Проверить"))

    assert events[-1]["result"]["run_id"] == "RUN-EXTERNAL"
    assert "scripts/ouroboros_audit.py" in fake.description
    assert str(tmp_path / ".venv/bin/python") in fake.description


def test_developer_orchestrator_uses_isolated_worktree_without_merge(
    tmp_path, monkeypatch
):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    class FakeClient:
        def __init__(self):
            self.description = ""
            self.workspace = ""

        def health(self):
            return {"ok": True}

        def create_task(self, description, workspace, timeout_seconds):
            self.description = description
            self.workspace = workspace
            return {"task_id": "dev-task-1"}

        def get_task(self, task_id):
            assert task_id == "dev-task-1"
            return {"status": "completed", "result": "Tests passed"}

    monkeypatch.setenv("AUDIT_AGENT_OUTPUT_ROOT", str(tmp_path / "outputs"))
    monkeypatch.setattr(
        developer_module,
        "create_improvement_branch",
        lambda run_id: {
            "run_id": run_id,
            "branch": f"improvement/{run_id}",
            "worktree": str(worktree),
            "base_commit": "abc123",
            "status": "CREATED",
        },
    )
    monkeypatch.setattr(
        developer_module,
        "preview_improvement",
        lambda run_id: {
            "run_id": run_id,
            "branch": f"improvement/{run_id}",
            "worktree": str(worktree),
            "base_commit": "abc123",
            "changed_paths": ["prompts/auditor.md"],
            "patch_path": str(tmp_path / "improvement.patch"),
            "merged": False,
            "committed": False,
            "pushed": False,
        },
    )
    monkeypatch.setattr(
        developer_module,
        "run_tests",
        lambda run_id: {"passed": True, "returncode": 0, "output": "ok"},
    )
    fake = FakeClient()
    settings = ApplicationSettings(
        ouroboros=OuroborosSettings(
            workspace=str(tmp_path),
            python_executable=".venv/bin/python",
            timeout_seconds=60,
            poll_interval_seconds=0.1,
        )
    )
    orchestrator = OuroborosDeveloperOrchestrator(settings=settings, client=fake)
    events = list(orchestrator.run_with_updates("Improve the prompt", "RUN-DEV"))

    result = events[-1]["result"]
    assert fake.workspace == str(worktree)
    assert "improvement/RUN-DEV" in fake.description
    assert "git commit, merge" in fake.description
    assert result["merged"] is False
    assert result["committed"] is False
    assert result["pushed"] is False


def test_agent_system_improves_only_when_a_gap_is_detected():
    class FakeAudit:
        def __init__(self, improvement_needed):
            self.settings = ApplicationSettings(
                self_improvement={"enabled": True, "require_detected_gap": True}
            )
            self.improvement_needed = improvement_needed

        def run_with_updates(self, user_request):
            yield {"kind": "status", "message": "Analyzing"}
            yield {
                "kind": "result",
                "result": {
                    "run_id": "RUN-AUTO",
                    "status": "COMPLETED",
                    "findings": [],
                    "execution_errors": [],
                    "improvement_needed": self.improvement_needed,
                    "improvement_reason": "Missing generic reconciliation",
                },
            }

    class FakeDeveloper:
        def __init__(self):
            self.calls = 0

        def run_after_audit(self, audit_result, user_request):
            self.calls += 1
            yield {
                "kind": "result",
                "result": {
                    "branch": "improvement/RUN-AUTO",
                    "changed_paths": ["src/audit_insight_agent/reconciliation.py"],
                    "patch_path": "/tmp/improvement.patch",
                    "has_changes": True,
                    "tests_passed": True,
                    "merged": False,
                    "committed": False,
                    "pushed": False,
                },
            }

    no_gap_developer = FakeDeveloper()
    no_gap = AuditAgentSystem(
        audit=FakeAudit(False), developer=no_gap_developer
    )
    no_gap_result = list(no_gap.run_with_updates("Audit"))[-1]["result"]
    assert no_gap_developer.calls == 0
    assert no_gap_result["self_improvement"]["status"] == "NOT_REQUIRED"

    gap_developer = FakeDeveloper()
    with_gap = AuditAgentSystem(
        audit=FakeAudit(True), developer=gap_developer
    )
    gap_result = list(with_gap.run_with_updates("Audit"))[-1]["result"]
    assert gap_developer.calls == 1
    assert gap_result["self_improvement"]["status"] == "PATCH_READY"
    assert gap_result["self_improvement"]["merged"] is False


def test_agent_system_reviews_every_audit_when_enabled():
    class FakeAudit:
        settings = ApplicationSettings(
            self_improvement={
                "enabled": True,
                "review_after_every_audit": True,
                "require_detected_gap": True,
            }
        )

        def run_with_updates(self, user_request):
            yield {
                "kind": "result",
                "result": {
                    "run_id": "RUN-REVIEW",
                    "status": "COMPLETED",
                    "findings": [],
                    "execution_errors": [],
                    "improvement_needed": False,
                },
            }

    class FakeDeveloper:
        calls = 0

        def run_after_audit(self, audit_result, user_request):
            self.calls += 1
            yield {
                "kind": "result",
                "result": {
                    "has_changes": False,
                    "tests_passed": None,
                    "merged": False,
                },
            }

    developer = FakeDeveloper()
    result = list(
        AuditAgentSystem(audit=FakeAudit(), developer=developer).run_with_updates("Audit")
    )[-1]["result"]
    assert developer.calls == 1
    assert result["self_improvement"]["status"] == "NO_CHANGES"


def test_ouroboros_protocol_supports_one_blocking_clarification():
    value = OuroborosOrchestrator._protocol_value(
        'AUDIT_CLARIFICATION_REQUIRED={"question":"What does column X mean?"}',
        OuroborosOrchestrator.CLARIFICATION_PREFIX,
    )
    assert value == {"question": "What does column X mean?"}

    answer = OuroborosOrchestrator._extract_auditor_answer(
        "Done\nAUDIT_IMPROVEMENT_NEEDED=false\nAUDIT_RUN_ID=RUN-1"
    )
    assert answer == "Done"


def test_ouroboros_selects_only_exact_registered_replica_name(tmp_path):
    settings = ApplicationSettings(
        databases={
            "connections": {
                "replica_a": {
                    "host": "db.local",
                    "database": "audit",
                    "user": "readonly",
                    "password_env": "DB_PASSWORD",
                },
                "replica_archive": {
                    "engine": "greenplum",
                    "host": "gp.local",
                    "database": "archive",
                    "user": "readonly",
                    "password_env": "GP_PASSWORD",
                },
            }
        },
        ouroboros=OuroborosSettings(workspace=str(tmp_path)),
    )
    orchestrator = OuroborosOrchestrator(settings=settings, client=object())
    assert orchestrator._replica_from_query("Check replica_a balances") == "replica_a"
    assert orchestrator._replica_from_query("Check replica_a_extra") is None
    with pytest.raises(ValueError, match="несколько реплик"):
        orchestrator._replica_from_query("Compare replica_a and replica_archive")
