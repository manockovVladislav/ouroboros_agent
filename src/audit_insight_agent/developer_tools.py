"""Restricted improvement-worktree operations available only in developer mode."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath
from typing import Any

from .models import FeedbackForOuroboros
from .ouroboros_tools import PROJECT_ROOT, _output_root, _validate_run_id


PROTECTED_PARTS = {
    ".env",
    "audit-evaluator",
    "evaluator",
    "ground_truth",
    "production",
}
PROTECTED_FILES = {
    "configs/config.yaml",
    "configs/data_sources.yaml",
}
ALLOWED_TOP_LEVEL = {
    "src",
    "rules",
    "cases",
    "tests",
    "docs",
    "prompts",
    "templates",
    "scripts",
    "README.md",
    "pyproject.toml",
    "requirements.txt",
}


def _git(repository: Path, *arguments: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _branch_name(run_id: str) -> str:
    validated = _validate_run_id(run_id)
    if validated is None:
        raise ValueError("run_id обязателен")
    return f"improvement/{validated}"


def _worktree_root() -> Path:
    return Path(
        os.getenv("AUDIT_AGENT_DEV_WORKTREE_ROOT", "/tmp/audit-insight-improvements")
    ).expanduser().resolve()


def _worktree_path(run_id: str) -> Path:
    validated = _validate_run_id(run_id)
    if validated is None:
        raise ValueError("run_id обязателен")
    return _worktree_root() / validated


def _metadata_path(run_id: str) -> Path:
    validated = _validate_run_id(run_id)
    if validated is None:
        raise ValueError("run_id обязателен")
    return _worktree_root() / ".metadata" / f"{validated}.json"


def _write_worktree_metadata(run_id: str, value: dict[str, str]) -> None:
    path = _metadata_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _read_worktree_metadata(run_id: str) -> dict[str, str]:
    path = _metadata_path(run_id)
    if not path.is_file():
        raise FileNotFoundError(f"Improvement metadata not found: {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Invalid improvement metadata")
    return {str(key): str(value) for key, value in raw.items()}


def create_improvement_branch(run_id: str) -> dict[str, str]:
    """Create an isolated worktree; never switch or modify the stable checkout."""

    branch = _branch_name(run_id)
    path = _worktree_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _assert_improvement_worktree(path, branch)
        _remove_runtime_links(path)
        try:
            metadata = _read_worktree_metadata(run_id)
        except FileNotFoundError:
            stable_head = _git(PROJECT_ROOT, "rev-parse", "HEAD")
            if stable_head.returncode != 0:
                raise RuntimeError(stable_head.stderr.strip() or "Cannot resolve stable HEAD")
            base = _git(path, "merge-base", "HEAD", stable_head.stdout.strip())
            if base.returncode != 0:
                raise RuntimeError(base.stderr.strip() or "Cannot recover worktree base")
            metadata = {
                "run_id": run_id,
                "branch": branch,
                "worktree": str(path),
                "base_commit": base.stdout.strip(),
            }
            _write_worktree_metadata(run_id, metadata)
        return {
            "branch": branch,
            "worktree": str(path),
            "base_commit": metadata["base_commit"],
            "status": "EXISTS",
        }
    base = _git(PROJECT_ROOT, "rev-parse", "HEAD")
    if base.returncode != 0:
        raise RuntimeError(base.stderr.strip() or "Cannot resolve base commit")
    completed = _git(PROJECT_ROOT, "worktree", "add", "-b", branch, str(path), "HEAD")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git worktree add failed")
    _remove_runtime_links(path)
    metadata = {
        "run_id": run_id,
        "branch": branch,
        "worktree": str(path),
        "base_commit": base.stdout.strip(),
    }
    _write_worktree_metadata(run_id, metadata)
    return {**metadata, "status": "CREATED"}


def _remove_runtime_links(worktree: Path) -> None:
    """Remove legacy links that could write through to stable runtime inputs."""

    candidates = [
        worktree / "configs" / "config.yaml",
        worktree / "configs" / "data_sources.yaml",
    ]
    for root in (worktree / "data", worktree / "knowledge" / "documents"):
        if root.is_dir():
            candidates.extend(path for path in root.iterdir() if path.is_symlink())
    for path in candidates:
        if path.is_symlink():
            path.unlink()


def _assert_improvement_worktree(path: Path, expected_branch: str) -> None:
    branch = _git(path, "branch", "--show-current")
    if branch.returncode != 0 or branch.stdout.strip() != expected_branch:
        raise PermissionError(
            f"Developer changes require branch {expected_branch}, got {branch.stdout.strip()!r}"
        )
    if branch.stdout.strip() in {"main", "develop"}:
        raise PermissionError("Protected branch cannot be modified")


def _patch_paths(patch: str) -> set[str]:
    paths = set()
    for line in patch.splitlines():
        if not line.startswith(("+++ ", "--- ")):
            continue
        value = line[4:].split("\t", 1)[0]
        if value == "/dev/null":
            continue
        if value.startswith(("a/", "b/")):
            value = value[2:]
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise PermissionError(f"Unsafe patch path: {value}")
        normalized = path.as_posix()
        if normalized in PROTECTED_FILES:
            raise PermissionError(f"Protected file cannot be changed: {normalized}")
        lowered_parts = {part.casefold() for part in path.parts}
        if lowered_parts & PROTECTED_PARTS or any(
            any(token in part for token in ("ground_truth", "groundtruth", "ground-truth", "evaluator"))
            for part in lowered_parts
        ):
            raise PermissionError(f"Protected path cannot be changed: {normalized}")
        if path.parts[0] not in ALLOWED_TOP_LEVEL:
            raise PermissionError(f"Path is outside developer allowlist: {normalized}")
        paths.add(normalized)
    if not paths:
        raise ValueError("Patch does not contain file changes")
    return paths


def apply_code_changes(run_id: str, patch: str) -> dict[str, Any]:
    if len(patch.encode("utf-8")) > 2_000_000:
        raise ValueError("Patch exceeds 2 MB limit")
    changed_paths = sorted(_patch_paths(patch))
    worktree = _worktree_path(run_id)
    _assert_improvement_worktree(worktree, _branch_name(run_id))
    check = subprocess.run(
        ["git", "apply", "--check", "-"],
        cwd=worktree,
        input=patch,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if check.returncode != 0:
        raise ValueError(check.stderr.strip() or "Patch validation failed")
    applied = subprocess.run(
        ["git", "apply", "-"],
        cwd=worktree,
        input=patch,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if applied.returncode != 0:
        raise RuntimeError(applied.stderr.strip() or "Patch application failed")
    return {"branch": _branch_name(run_id), "changed_paths": changed_paths}


def run_tests(run_id: str, test_path: str = "tests") -> dict[str, Any]:
    if not re.fullmatch(r"tests(?:/[A-Za-z0-9_.-]+)*", test_path):
        raise ValueError("test_path must stay inside tests/")
    worktree = _worktree_path(run_id)
    _assert_improvement_worktree(worktree, _branch_name(run_id))
    completed = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", test_path],
        cwd=worktree,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )
    return {
        "passed": completed.returncode == 0,
        "returncode": completed.returncode,
        "output": (completed.stdout + completed.stderr)[-20_000:],
    }


def read_feedback(run_id: str) -> dict[str, Any]:
    validated = _validate_run_id(run_id)
    if validated is None:
        raise ValueError("run_id обязателен")
    path = _output_root() / validated / "evaluation" / "feedback_for_ouroboros.json"
    feedback = FeedbackForOuroboros.model_validate_json(path.read_text(encoding="utf-8"))
    return feedback.model_dump(mode="json")


def preview_improvement(run_id: str) -> dict[str, Any]:
    """Validate and export an unmerged patch for manual review."""

    worktree = _worktree_path(run_id)
    branch = _branch_name(run_id)
    _assert_improvement_worktree(worktree, branch)
    metadata = _read_worktree_metadata(run_id)
    base_commit = metadata["base_commit"]
    commits = _git(worktree, "rev-list", "--count", f"{base_commit}..HEAD")
    if commits.returncode != 0:
        raise RuntimeError(commits.stderr.strip() or "Cannot inspect improvement commits")
    if int(commits.stdout.strip() or "0") != 0:
        raise PermissionError("Ouroboros must not create commits in developer mode")

    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard")
    if untracked.returncode != 0:
        raise RuntimeError(untracked.stderr.strip() or "Cannot list untracked files")
    untracked_paths = [line for line in untracked.stdout.splitlines() if line]
    for path in untracked_paths:
        _patch_paths(f"--- /dev/null\n+++ b/{path}\n")
    if untracked_paths:
        intent = _git(worktree, "add", "-N", "--", *untracked_paths)
        if intent.returncode != 0:
            raise RuntimeError(intent.stderr.strip() or "Cannot include new files in preview")

    changed = _git(worktree, "diff", "--name-only", base_commit, "--", ".")
    if changed.returncode != 0:
        raise RuntimeError(changed.stderr.strip() or "Cannot inspect changed paths")
    changed_paths = sorted({line for line in changed.stdout.splitlines() if line})
    for path in changed_paths:
        _patch_paths(f"--- a/{path}\n+++ b/{path}\n")

    diff = _git(worktree, "diff", "--binary", base_commit, "--", ".")
    if diff.returncode != 0:
        raise RuntimeError(diff.stderr.strip() or "Cannot create improvement preview")
    patch = diff.stdout
    if len(patch.encode("utf-8")) > 2_000_000:
        raise ValueError("Generated patch exceeds 2 MB limit")

    development_dir = _output_root() / run_id / "development"
    development_dir.mkdir(parents=True, exist_ok=True)
    patch_path = development_dir / "improvement.patch"
    temporary = patch_path.with_suffix(".patch.tmp")
    temporary.write_text(patch, encoding="utf-8")
    temporary.replace(patch_path)
    summary = {
        "run_id": run_id,
        "branch": branch,
        "worktree": str(worktree),
        "base_commit": base_commit,
        "changed_paths": changed_paths,
        "patch_path": str(patch_path),
        "patch_size_bytes": len(patch.encode("utf-8")),
        "has_changes": bool(changed_paths),
        "merged": False,
        "committed": False,
        "pushed": False,
    }
    summary_path = development_dir / "summary.json"
    temporary_summary = summary_path.with_suffix(".json.tmp")
    temporary_summary.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary_summary.replace(summary_path)
    return summary


def create_patch(
    run_id: str,
    evaluation_run_id: str | None = None,
) -> dict[str, str]:
    feedback = read_feedback(evaluation_run_id or run_id)
    if feedback.get("quality_improved") is not True:
        raise PermissionError(
            "Patch export requires evaluator feedback with quality_improved=true"
        )
    worktree = _worktree_path(run_id)
    branch = _branch_name(run_id)
    _assert_improvement_worktree(worktree, branch)
    untracked = _git(worktree, "ls-files", "--others", "--exclude-standard")
    if untracked.returncode != 0:
        raise RuntimeError(untracked.stderr.strip() or "Cannot list untracked files")
    untracked_paths = [line for line in untracked.stdout.splitlines() if line]
    if untracked_paths:
        for path in untracked_paths:
            _patch_paths(f"--- /dev/null\n+++ b/{path}\n")
        intent = _git(worktree, "add", "-N", "--", *untracked_paths)
        if intent.returncode != 0:
            raise RuntimeError(intent.stderr.strip() or "Cannot include new files in patch")
    completed = _git(worktree, "diff", "--binary", "--", ".")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git diff failed")
    patch = completed.stdout
    if len(patch.encode("utf-8")) > 2_000_000:
        raise ValueError("Generated patch exceeds 2 MB limit")
    return {"branch": branch, "patch": patch}
