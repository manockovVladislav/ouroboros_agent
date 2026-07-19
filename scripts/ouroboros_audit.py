"""Stable command boundary used by the external Ouroboros server."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from audit_insight_agent.ouroboros_tools import run_full_audit
from audit_insight_agent.logging_config import configure_logging


def _load_request(path_value: str) -> dict[str, str]:
    allowed_root = (PROJECT_ROOT / "outputs" / "requests").resolve()
    path = Path(path_value).expanduser()
    if not path.is_absolute():
        path = PROJECT_ROOT / path
    path = path.resolve()
    if path.parent != allowed_root:
        raise PermissionError("Audit request must be inside outputs/requests")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Audit request must be a JSON object")
    auditor_query = str(raw.get("auditor_query") or "").strip()
    replica_name = str(raw.get("replica_name") or "").strip()
    input_mode = str(raw.get("input_mode") or "local_files").strip()
    database_access = bool(raw.get("database_access", False))
    if not auditor_query:
        raise ValueError("auditor_query is required")
    if input_mode == "local_files" and (database_access or replica_name):
        raise ValueError("local_files mode cannot request database access")
    if database_access and not replica_name:
        raise ValueError("database_access requires an exact replica_name")
    return {
        "auditor_query": auditor_query,
        "replica_name": replica_name,
        "input_mode": input_mode,
        "request_path": str(path),
    }


def _write_request_result(request_path: str, result: dict) -> Path:
    """Persist audit completion before the outer judge prepares its response."""

    request = Path(request_path).resolve()
    path = request.with_suffix(".result.json")
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(
            {
                "request_id": request.stem,
                "run_id": result["run_id"],
                "status": result["status"],
                "report_path": result["report_path"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


def main() -> None:
    configure_logging(PROJECT_ROOT / "configs" / "logging.yaml")
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args()
    request = _load_request(arguments.request)
    try:
        result = run_full_audit(
            request["auditor_query"],
            replica_name=request["replica_name"] or None,
        )
        _write_request_result(request["request_path"], result)
    except Exception:
        logging.getLogger("audit_insight.ouroboros_entrypoint").exception(
            "Audit execution failed"
        )
        raise
    print("AUDIT_RESULT=" + json.dumps(result, ensure_ascii=False))
    print(f"AUDIT_RUN_ID={result['run_id']}")


if __name__ == "__main__":
    main()
