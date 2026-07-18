import subprocess

import pytest

from audit_insight_agent import developer_tools
from audit_insight_agent.developer_tools import _patch_paths


def test_patch_allowlist_accepts_rules_and_rejects_ground_truth():
    allowed = """diff --git a/rules/ovp/check.yaml b/rules/ovp/check.yaml
--- a/rules/ovp/check.yaml
+++ b/rules/ovp/check.yaml
@@ -1 +1 @@
-enabled: false
+enabled: true
"""
    assert _patch_paths(allowed) == {"rules/ovp/check.yaml"}

    protected = """diff --git a/ground_truth/truth.json b/ground_truth/truth.json
--- a/ground_truth/truth.json
+++ b/ground_truth/truth.json
@@ -1 +1 @@
-{}
+{"answer": 1}
"""
    with pytest.raises(PermissionError):
        _patch_paths(protected)


def test_patch_allowlist_accepts_only_config_templates():
    template = """--- a/configs/config.example.yaml
+++ b/configs/config.example.yaml
"""
    runtime = """--- a/configs/logging.yaml
+++ b/configs/logging.yaml
"""
    assert _patch_paths(template) == {"configs/config.example.yaml"}
    with pytest.raises(PermissionError):
        _patch_paths(runtime)


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
    preview = developer_tools.preview_improvement("RUN-DEV")
    assert preview["changed_paths"] == ["docs/improvement.md"]
    assert preview["merged"] is False
    assert (tmp_path / "outputs/RUN-DEV/development/improvement.patch").is_file()
