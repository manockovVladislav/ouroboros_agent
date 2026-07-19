import json
from pathlib import Path

import pytest

from audit_insight_agent.agent_system import AuditAgentSystem
from audit_insight_agent import developer_orchestrator as developer_module
from audit_insight_agent import ouroboros as ouroboros_module
from audit_insight_agent.developer_orchestrator import OuroborosDeveloperOrchestrator
from audit_insight_agent.ouroboros import (
    OuroborosConnectionError,
    OuroborosOrchestrator,
)
from audit_insight_agent.models import ApplicationSettings, OuroborosSettings
from audit_insight_agent.report_generator import write_narrative_report
from audit_insight_agent.ouroboros_tools import (
    generate_report,
    list_data_sources,
    profile_data_source,
    run_rule,
)
from audit_insight_agent.web import build_interface
from audit_insight_agent.web import _activity_log


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
    ovp_finding_ids = {
        item["finding_id"]
        for item in result["findings"]
        if item["check_id"] == "OVP_LIMIT_EXCEEDED"
    }
    assert all(
        item["verdict"] == "CONFIRMED"
        for item in result["finding_reviews"]
        if item["finding_id"] in ovp_finding_ids
    )
    report_path = Path(generate_report("RUN-API")["report_path"])
    report = report_path.read_text(encoding="utf-8")
    assert report.startswith("# Аудиторское заключение\n\n## Главный вывод")
    assert "## Ключевые основания" in report
    assert "## Рекомендуемые действия" in report
    assert report.index("## Главный вывод") < report.index("## Evidence critique")
    assert "## Evidence critique" in report
    assert "## Prioritized audit plan" in report
    if any(item["verdict"] == "CONFIRMED" for item in result["finding_reviews"]):
        assert "## Confirmed findings" in report
        assert "Document/location:" in report


def test_judge_narrative_becomes_canonical_report_without_losing_appendix(tmp_path):
    report_path = tmp_path / "report.md"
    report_path.write_text(
        "# Аудиторское заключение\n\n## Главный вывод\n\nЧерновик.\n\n"
        "## Техническое приложение\n\n- Run ID: `RUN-1`\n",
        encoding="utf-8",
    )

    write_narrative_report(
        report_path,
        "## Главный вывод\n\nПодтверждённых нарушений нет.\n\n"
        "## Ключевые основания\n\nДоказательств недостаточно.\n"
        "AUDIT_RUN_ID=RUN-1",
    )
    first = report_path.read_text(encoding="utf-8")
    write_narrative_report(report_path, "## Главный вывод\n\nОбновлённый вывод.")
    second = report_path.read_text(encoding="utf-8")

    assert first.startswith("# Аудиторское заключение\n\n## Главный вывод")
    assert "AUDIT_RUN_ID" not in first
    assert second.count("## Техническое приложение") == 1
    assert "Обновлённый вывод." in second
    assert "Черновик." not in second
    assert "- Run ID: `RUN-1`" in second


def test_truncated_judge_response_does_not_replace_complete_report(tmp_path):
    report_path = tmp_path / "report.md"
    complete = "# Аудиторское заключение\n\n## Главный вывод\n\nПолный вывод.\n"
    report_path.write_text(complete, encoding="utf-8")

    write_narrative_report(
        report_path,
        "## Главный вывод\n\nОборванный ответ…[truncated]",
    )

    assert report_path.read_text(encoding="utf-8") == complete


def test_web_answer_summary():
    answer = OuroborosOrchestrator._answer(
        {
            "status": "COMPLETED",
            "findings": [{"severity": "HIGH", "title": "Test finding"}],
            "execution_errors": [],
        }
    )
    assert "## Главный вывод" in answer
    assert "внимания требует" in answer
    assert "COMPLETED" not in answer

    many = OuroborosOrchestrator._answer(
        {
            "status": "COMPLETED",
            "findings": [
                {"severity": "MEDIUM", "title": f"Finding {index}"}
                for index in range(30)
            ],
            "execution_errors": [],
        }
    )
    assert "30 нарушений" in many

    authored = OuroborosOrchestrator._answer(
        {
            "status": "COMPLETED",
            "findings": [],
            "ouroboros_answer": "Главный вывод. Проверка завершена.",
        }
    )
    assert authored == "Главный вывод. Проверка завершена."


def test_activity_log_keeps_recent_steps_and_explains_the_work():
    messages = [f"Шаг {index}" for index in range(15)]
    rendered = _activity_log(messages)

    assert rendered.startswith("## Ход работы")
    assert "Шаг 0" not in rendered
    assert "Шаг 3" in rendered
    assert "Шаг 14" in rendered


def test_gradio_interface_builds_without_starting_server():
    interface = build_interface()
    assert interface.__class__.__name__ == "Blocks"
    config = interface.get_config_file()
    tab_labels = [
        component.get("props", {}).get("label")
        for component in config["components"]
        if component.get("type") in {"tab", "tabitem"}
    ]
    assert tab_labels == ["Аудитор — быстрый", "Аудитор + разработчик"]
    assert len(config["dependencies"]) == 6


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
    started = next(event for event in events if event.get("request_id"))
    assert started["request_id"].startswith("REQ-")
    assert started["task_id"] == "task-1"
    assert "scripts/ouroboros_audit.py" in fake.description
    assert str(tmp_path / ".venv/bin/python") in fake.description


def test_completed_audit_is_recovered_when_ouroboros_connection_is_lost(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(ouroboros_module, "PROJECT_ROOT", tmp_path)
    run_dir = tmp_path / "outputs" / "runs" / "RUN-RECOVERED"
    run_dir.mkdir(parents=True)
    (run_dir / "report.md").write_text("# Recovered report\n", encoding="utf-8")

    class FakeClient:
        def health(self):
            return {"ok": True}

        def create_task(self, description, workspace, timeout_seconds):
            return {"task_id": "task-recovery"}

        def get_task(self, task_id):
            request_path = next((tmp_path / "outputs" / "requests").glob("REQ-*.json"))
            request_path.with_suffix(".result.json").write_text(
                json.dumps(
                    {
                        "request_id": request_path.stem,
                        "run_id": "RUN-RECOVERED",
                        "status": "COMPLETED",
                        "report_path": str(run_dir / "report.md"),
                    }
                ),
                encoding="utf-8",
            )
            raise OuroborosConnectionError("connection lost")

    loaded = {
        "run_id": "RUN-RECOVERED",
        "status": "COMPLETED",
        "findings": [],
        "finding_reviews": [],
        "audit_plan": [],
        "execution_errors": [],
        "candidate_findings_path": str(run_dir / "candidate_findings.json"),
        "report_path": str(run_dir / "report.md"),
    }
    orchestrator = OuroborosOrchestrator(
        settings=ApplicationSettings(
            ouroboros=OuroborosSettings(
                workspace=str(tmp_path),
                timeout_seconds=10,
                poll_interval_seconds=0.1,
            )
        ),
        client=FakeClient(),
        result_loader=lambda _run_id: dict(loaded),
    )

    events = list(orchestrator.run_with_updates("Проверить данные"))
    result = events[-1]["result"]

    assert result["run_id"] == "RUN-RECOVERED"
    assert result["recovered_after_connection_loss"] is True
    assert Path(result["chat_path"]).is_file()
    assert any("аудит восстановлен" in event.get("message", "") for event in events)


def test_completed_audit_is_recovered_when_outer_task_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(ouroboros_module, "PROJECT_ROOT", tmp_path)
    run_dir = tmp_path / "outputs" / "runs" / "RUN-SAVED"
    run_dir.mkdir(parents=True)

    class FakeClient:
        def health(self):
            return {"ok": True}

        def create_task(self, description, workspace, timeout_seconds):
            return {"task_id": "task-saved"}

        def get_task(self, task_id):
            request_path = next((tmp_path / "outputs" / "requests").glob("REQ-*.json"))
            request_path.with_suffix(".result.json").write_text(
                json.dumps({"run_id": "RUN-SAVED", "status": "COMPLETED"}),
                encoding="utf-8",
            )
            return {"status": "failed", "error": "provider disconnected"}

    loaded = {
        "run_id": "RUN-SAVED",
        "status": "COMPLETED",
        "findings": [],
        "finding_reviews": [],
        "audit_plan": [],
        "execution_errors": [],
        "candidate_findings_path": str(run_dir / "candidate_findings.json"),
        "report_path": str(run_dir / "report.md"),
    }
    events = list(
        OuroborosOrchestrator(
            settings=ApplicationSettings(
                ouroboros=OuroborosSettings(
                    workspace=str(tmp_path),
                    timeout_seconds=10,
                    poll_interval_seconds=0.1,
                )
            ),
            client=FakeClient(),
            result_loader=lambda _run_id: dict(loaded),
        ).run_with_updates("Проверить данные")
    )

    assert events[-1]["result"]["run_id"] == "RUN-SAVED"
    assert events[-1]["result"]["recovered_from_saved_result"] is True
    assert any("Расчёты завершены" in event.get("message", "") for event in events)


def test_browser_session_can_reattach_to_saved_request(tmp_path, monkeypatch):
    monkeypatch.setattr(ouroboros_module, "PROJECT_ROOT", tmp_path)
    request_id = "REQ-" + "a" * 32
    request_root = tmp_path / "outputs" / "requests"
    request_root.mkdir(parents=True)
    request_path = request_root / f"{request_id}.json"
    request_path.write_text(
        json.dumps({"request_id": request_id, "auditor_query": "Проверить"}),
        encoding="utf-8",
    )
    request_path.with_suffix(".events.jsonl").write_text(
        json.dumps(
            {
                "event": "ouroboros_task_created",
                "request_id": request_id,
                "task_id": "task-before-reload",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    request_path.with_suffix(".result.json").write_text(
        json.dumps({"run_id": "RUN-REATTACHED", "status": "COMPLETED"}),
        encoding="utf-8",
    )
    run_dir = tmp_path / "outputs" / "runs" / "RUN-REATTACHED"
    run_dir.mkdir(parents=True)
    loaded = {
        "run_id": "RUN-REATTACHED",
        "status": "COMPLETED",
        "findings": [],
        "finding_reviews": [],
        "audit_plan": [],
        "execution_errors": [],
        "candidate_findings_path": str(run_dir / "candidate_findings.json"),
        "report_path": str(run_dir / "report.md"),
    }
    orchestrator = OuroborosOrchestrator(
        settings=ApplicationSettings(
            ouroboros=OuroborosSettings(workspace=str(tmp_path))
        ),
        client=object(),
        result_loader=lambda _run_id: dict(loaded),
    )

    events = list(orchestrator.recover_request_with_updates(request_id))

    assert events[0]["task_id"] == "task-before-reload"
    assert events[-1]["result"]["run_id"] == "RUN-REATTACHED"


def test_orchestrator_retries_a_temporary_connection_loss(tmp_path, monkeypatch):
    monkeypatch.setattr(ouroboros_module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(ouroboros_module.time, "sleep", lambda _seconds: None)

    class FakeClient:
        polls = 0

        def health(self):
            return {"ok": True}

        def create_task(self, description, workspace, timeout_seconds):
            return {"task_id": "task-retry"}

        def get_task(self, task_id):
            self.polls += 1
            if self.polls == 1:
                raise OuroborosConnectionError("temporary outage")
            return {"status": "completed", "result": "AUDIT_RUN_ID=RUN-RETRY"}

    run_dir = tmp_path / "RUN-RETRY"
    loaded = {
        "run_id": "RUN-RETRY",
        "status": "COMPLETED",
        "findings": [],
        "finding_reviews": [],
        "audit_plan": [],
        "execution_errors": [],
        "candidate_findings_path": str(run_dir / "candidate_findings.json"),
        "report_path": str(run_dir / "report.md"),
    }
    events = list(
        OuroborosOrchestrator(
            settings=ApplicationSettings(
                ouroboros=OuroborosSettings(
                    workspace=str(tmp_path),
                    timeout_seconds=10,
                    poll_interval_seconds=0.1,
                )
            ),
            client=FakeClient(),
            result_loader=lambda _run_id: dict(loaded),
        ).run_with_updates("Проверить данные")
    )

    messages = [event.get("message", "") for event in events]
    assert any("временно потеряна" in message for message in messages)
    assert any("восстановлена" in message for message in messages)
    assert events[-1]["result"]["run_id"] == "RUN-RETRY"


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


def test_local_scope_never_requires_clarification(tmp_path):
    settings = ApplicationSettings(
        self_improvement={"allow_blocking_clarification": True},
        databases={
            "connections": {
                "replica_a": {
                    "host": "db.local",
                    "database": "audit",
                    "user": "readonly",
                    "password_env": "DB_PASSWORD",
                }
            }
        },
        ouroboros=OuroborosSettings(workspace=str(tmp_path)),
    )
    orchestrator = OuroborosOrchestrator(settings=settings, client=object())

    assert not orchestrator._clarification_allowed(
        "Проведи полный аудит data/ и knowledge/", None
    )
    assert not orchestrator._clarification_allowed(
        "Анализируй только локальные файлы", None
    )
    assert orchestrator._clarification_allowed("Проверь операции", None)

    request_path = orchestrator._write_task_request(
        "Проведи полный аудит data/ и knowledge/"
    )
    payload = json.loads(request_path.read_text(encoding="utf-8"))
    assert payload["input_mode"] == "local_files"
    assert payload["scope_complete"] is True
    assert payload["database_access"] is False
    assert "replica_name" not in payload
    prompt = orchestrator._task_prompt(request_path)
    assert "database_access: false" in prompt
    assert "это корректный локальный режим" in prompt


def test_disallowed_clarification_is_retried_without_user_dialog(tmp_path):
    class FakeClient:
        def __init__(self):
            self.created = 0

        def health(self):
            return {"ok": True}

        def create_task(self, description, workspace, timeout_seconds):
            self.created += 1
            if self.created == 2:
                assert "Вопросы пользователю запрещены" in description
            return {"task_id": f"task-{self.created}"}

        def get_task(self, task_id):
            if task_id == "task-1":
                return {
                    "status": "completed",
                    "result": (
                        'AUDIT_CLARIFICATION_REQUIRED={"question":"Какую '
                        'систему проверять?"}'
                    ),
                }
            return {"status": "completed", "result": "AUDIT_RUN_ID=RUN-RETRY"}

    loaded = {
        "run_id": "RUN-RETRY",
        "status": "COMPLETED",
        "findings": [],
        "finding_reviews": [],
        "audit_plan": [],
        "execution_errors": [],
        "candidate_findings_path": str(tmp_path / "candidate_findings.json"),
        "report_path": str(tmp_path / "report.md"),
    }
    client = FakeClient()
    settings = ApplicationSettings(
        self_improvement={"allow_blocking_clarification": True},
        ouroboros=OuroborosSettings(
            workspace=str(tmp_path), timeout_seconds=60, poll_interval_seconds=0.1
        ),
    )
    events = list(
        OuroborosOrchestrator(
            settings=settings,
            client=client,
            result_loader=lambda _run_id: dict(loaded),
        ).run_with_updates("Проведи полный аудит data/ и knowledge/")
    )

    assert client.created == 2
    assert all(event["kind"] != "clarification" for event in events)
    assert events[-1]["result"]["run_id"] == "RUN-RETRY"


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
