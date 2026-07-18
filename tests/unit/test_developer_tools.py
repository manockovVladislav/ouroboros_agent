import json
import subprocess

import pytest

from audit_insight_agent import developer_tools
from audit_insight_agent.developer_tools import _patch_paths, read_feedback


def test_patch_allowlist_accepts_rules_and_rejects_evaluator():
    allowed = """diff --git a/rules/ovp/check.yaml b/rules/ovp/check.yaml
--- a/rules/ovp/check.yaml
+++ b/rules/ovp/check.yaml
@@ -1 +1 @@
-enabled: false
+enabled: true
"""
    assert _patch_paths(allowed) == {"rules/ovp/check.yaml"}

    protected = """diff --git a/audit-evaluator/cases/x/truth.json b/audit-evaluator/cases/x/truth.json
--- a/audit-evaluator/cases/x/truth.json
+++ b/audit-evaluator/cases/x/truth.json
@@ -1 +1 @@
-{}
+{"answer": 1}
"""
    with pytest.raises(PermissionError):
        _patch_paths(protected)


def test_read_feedback_accepts_only_sanitized_contract(tmp_path, monkeypatch):
    monkeypatch.setenv("AUDIT_AGENT_OUTPUT_ROOT", str(tmp_path))
    feedback_dir = tmp_path / "RUN-1" / "evaluation"
    feedback_dir.mkdir(parents=True)
    (feedback_dir / "feedback_for_ouroboros.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "run_id": "RUN-1",
                "case_name": "case",
                "summary": "Недостаточно общих сверок",
                "weaknesses": ["Низкое покрытие связей"],
                "recommendations": ["Расширить декларативные relationships"],
                "aggregate_metrics": {"quality": 0.5},
            }
        ),
        encoding="utf-8",
    )
    assert read_feedback("RUN-1")["summary"] == "Недостаточно общих сверок"

    payload = json.loads((feedback_dir / "feedback_for_ouroboros.json").read_text())
    payload["ground_truth"] = {"exact_answer": "secret"}
    (feedback_dir / "feedback_for_ouroboros.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        read_feedback("RUN-1")


def test_improvement_changes_stay_in_isolated_branch(tmp_path, monkeypatch):
    repository = tmp_path / "stable"
    repository.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repository, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True)
    (repository / "README.md").write_text("stable\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repository, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=repository, check=True, capture_output=True)

    monkeypatch.setattr(developer_tools, "PROJECT_ROOT", repository)
    monkeypatch.setenv("AUDIT_AGENT_DEV_WORKTREE_ROOT", str(tmp_path / "worktrees"))
    monkeypatch.setenv("AUDIT_AGENT_OUTPUT_ROOT", str(tmp_path / "outputs"))
    created = developer_tools.create_improvement_branch("RUN-DEV")
    assert created["branch"] == "improvement/RUN-DEV"

    patch = """diff --git a/docs/improvement.md b/docs/improvement.md
new file mode 100644
--- /dev/null
+++ b/docs/improvement.md
@@ -0,0 +1 @@
+improved
"""
    developer_tools.apply_code_changes("RUN-DEV", patch)
    assert not (repository / "docs/improvement.md").exists()

    feedback_dir = tmp_path / "outputs/RUN-EVAL/evaluation"
    feedback_dir.mkdir(parents=True)
    (feedback_dir / "feedback_for_ouroboros.json").write_text(
        json.dumps(
            {
                "run_id": "RUN-EVAL",
                "case_name": "case",
                "summary": "quality improved",
                "quality_improved": True,
            }
        ),
        encoding="utf-8",
    )
    exported = developer_tools.create_patch("RUN-DEV", "RUN-EVAL")
    assert "docs/improvement.md" in exported["patch"]
