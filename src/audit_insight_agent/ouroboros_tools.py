from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .agent import AuditInsightAgent


"""Ограниченный публичный шлюз между Ouroboros и аудиторским ядром.

Планируемые инструменты:
- profile_dataset();
- run_audit_rule();
- search_documents();
- get_evidence();
- build_finding();
- generate_report().

Ouroboros не должен получать прямой файловый доступ или возможность выполнять
произвольный Python. TODO: определить строгие входные модели, allowlist путей,
лимиты ресурсов, аудит вызовов и безопасные сообщения об ошибках.
"""

PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parents[2]
)

DEFAULT_DATA_ROOT = (
    PROJECT_ROOT
    / "data"
)

DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "outputs"
    / "runs"
)


def _allowed_data_root() -> Path:

    return Path(
        os.getenv(
            "AUDIT_AGENT_ALLOWED_DATA_ROOT",
            str(DEFAULT_DATA_ROOT),
        )
    ).expanduser().resolve()


def _output_root() -> Path:

    return Path(
        os.getenv(
            "AUDIT_AGENT_OUTPUT_ROOT",
            str(DEFAULT_OUTPUT_ROOT),
        )
    ).expanduser().resolve()


def _resolve_inside(
    candidate: str | Path,
    allowed_root: Path,
) -> Path:

    resolved = Path(
        candidate
    ).expanduser().resolve()

    if (
        resolved != allowed_root
        and allowed_root
        not in resolved.parents
    ):
        raise PermissionError(
            f"Путь находится вне разрешенного "
            f"каталога: {resolved}"
        )

    return resolved


def _validate_run_id(
    run_id: str | None,
) -> str | None:

    if run_id is None:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,80}",
        run_id,
    ):
        raise ValueError(
            "Некорректный run_id"
        )

    return run_id


def run_audit(
    data_dir: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Запускает полный анализ публичных данных.

    Доступен Ouroboros.
    """

    allowed_root = (
        _allowed_data_root()
    )

    resolved_data_dir = (
        _resolve_inside(
            candidate=data_dir,
            allowed_root=allowed_root,
        )
    )

    validated_run_id = (
        _validate_run_id(
            run_id
        )
    )

    agent = AuditInsightAgent()

    result, paths = agent.run(
        data_dir=resolved_data_dir,
        output_root=_output_root(),
        run_id=validated_run_id,
    )

    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "findings_count": len(
            result.findings
        ),
        "execution_errors_count": len(
            result.execution_errors
        ),
        "candidate_findings_path": str(
            paths[
                "candidate_findings"
            ]
        ),
        "report_path": str(
            paths["report"]
        ),
        "run_manifest_path": str(
            paths["run_manifest"]
        ),
    }


def get_run_summary(
    run_id: str,
) -> dict[str, Any]:
    """
    Возвращает только безопасную сводку запуска.
    """

    validated_run_id = (
        _validate_run_id(
            run_id
        )
    )

    if validated_run_id is None:
        raise ValueError(
            "run_id обязателен"
        )

    manifest_path = (
        _output_root()
        / validated_run_id
        / "run_manifest.json"
    )

    resolved_manifest = (
        _resolve_inside(
            candidate=manifest_path,
            allowed_root=_output_root(),
        )
    )

    if not resolved_manifest.exists():
        raise FileNotFoundError(
            f"Запуск не найден: {run_id}"
        )

    return json.loads(
        resolved_manifest.read_text(
            encoding="utf-8",
        )
    )

