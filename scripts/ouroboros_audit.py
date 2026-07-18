"""Stable command boundary used by the external Ouroboros server."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from audit_insight_agent.ouroboros_tools import run_full_audit


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
    case_name = str(raw.get("case_name") or "").strip()
    auditor_query = str(raw.get("auditor_query") or "").strip()
    if not case_name or not auditor_query:
        raise ValueError("case_name and auditor_query are required")
    return {"case_name": case_name, "auditor_query": auditor_query}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    arguments = parser.parse_args()
    request = _load_request(arguments.request)
    result = run_full_audit(request["case_name"], request["auditor_query"])
    print("AUDIT_RESULT=" + json.dumps(result, ensure_ascii=False))
    print(f"AUDIT_RUN_ID={result['run_id']}")


if __name__ == "__main__":
    main()
