"""Restricted improvement-worktree operations available only in developer mode."""
from __future__ import annotations

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


def create_improvement_branch(run_id: str) -> dict[str, str]:
    """Create an isolated worktree; never switch or modify the stable checkout."""

    branch = _branch_name(run_id)
    path = _worktree_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        _assert_improvement_worktree(path, branch)
        _link_runtime_inputs(path)
        return {"branch": branch, "worktree": str(path), "status": "EXISTS"}
    completed = _git(PROJECT_ROOT, "worktree", "add", "-b", branch, str(path), "HEAD")
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or "git worktree add failed")
    _link_runtime_inputs(path)
    return {"branch": branch, "worktree": str(path), "status": "CREATED"}


def _link_runtime_inputs(worktree: Path) -> None:
    """Expose allowed ignored runtime inputs without copying them into Git."""

    for relative_root in (Path("data"), Path("knowledge/documents")):
        source_root = PROJECT_ROOT / relative_root
        target_root = worktree / relative_root
        if not source_root.is_dir() or not target_root.is_dir():
            continue
        for source in source_root.iterdir():
            if source.name == ".gitkeep":
                continue
            target = target_root / source.name
            if not target.exists() and not target.is_symlink():
                target.symlink_to(source, target_is_directory=source.is_dir())
    for relative in (Path("configs/config.yaml"), Path("configs/data_sources.yaml")):
        source = PROJECT_ROOT / relative
        target = worktree / relative
        if source.exists() and not target.exists() and not target.is_symlink():
            target.symlink_to(source)


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
