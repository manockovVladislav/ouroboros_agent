"""One-way adapter to an external evaluator that exclusively owns ground truth."""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any


def run_external_evaluator(
    case_name: str,
    run_id: str,
    candidate_findings: str | Path,
    run_dir: str | Path,
    timeout_seconds: int = 600,
) -> dict[str, Any]:
    """Invoke evaluator without reading evaluation.json or its private case files."""

    command_value = os.getenv("AUDIT_EVALUATOR_COMMAND")
    if not command_value:
        return {"status": "NOT_CONFIGURED"}
    command = shlex.split(command_value)
    if not command:
        raise ValueError("AUDIT_EVALUATOR_COMMAND is empty")
    evaluator_root = Path(
        os.getenv("AUDIT_EVALUATOR_ROOT", "audit-evaluator")
    ).expanduser().resolve()
    output_dir = Path(run_dir).expanduser().resolve() / "evaluation"
    output_dir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            *command,
            "--case",
            case_name,
            "--run-id",
            run_id,
            "--candidate-findings",
            str(Path(candidate_findings).resolve()),
            "--output-dir",
            str(output_dir),
        ],
        cwd=evaluator_root,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
        check=False,
    )
    if completed.returncode != 0:
        return {
            "status": "ERROR",
            "returncode": completed.returncode,
            "message": "External evaluator failed; private stderr is not exposed",
        }
    feedback_path = output_dir / "feedback_for_ouroboros.json"
    return {
        "status": "COMPLETED",
        "feedback_available": feedback_path.is_file(),
    }
