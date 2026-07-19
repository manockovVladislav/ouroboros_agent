"""Safe boundary for feedback produced by the private audit evaluator."""
from __future__ import annotations

import json
import re
from typing import Any

from .ouroboros_tools import _output_root, _validate_run_id


ALLOWED_CATEGORIES = {
    "ROOT_CAUSE",
    "CROSS_SOURCE_LINKAGE",
    "TEMPORAL_CHAIN",
    "BALANCE_RECONCILIATION",
    "OVP_IMPACT",
    "HEDGE_IMPACT",
    "ACCOUNTING_TRACE",
    "REPORT_CLARITY",
    "FALSE_POSITIVES",
}
SAFE_TARGETS = {
    "ROOT_CAUSE": ("finding_builder.py", "Проверить общую логику первопричины."),
    "CROSS_SOURCE_LINKAGE": ("reconciliation.py", "Проверить общую логику связей между источниками."),
    "TEMPORAL_CHAIN": ("analysis_tools.py", "Проверить восстановление временной цепочки."),
    "BALANCE_RECONCILIATION": ("reconciliation.py", "Проверить общую логику сверки показателей."),
    "OVP_IMPACT": ("rule_engine.py", "Проверить общую логику расчёта влияния операций."),
    "HEDGE_IMPACT": ("analysis_tools.py", "Проверить общую логику связи с хеджирующими операциями."),
    "ACCOUNTING_TRACE": ("reconciliation.py", "Проверить общую трассировку до бухгалтерских проводок."),
    "REPORT_CLARITY": ("report_generator.py", "Проверить связь отчёта с evidence."),
    "FALSE_POSITIVES": ("rule_engine.py", "Проверить общие исключения ложных срабатываний."),
}
MAX_FEEDBACK_BYTES = 64 * 1024


def _short_strings(value: Any, *, limit: int = 20) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()[:1000]
        for item in value[:limit]
        if isinstance(item, str) and item.strip()
    ]


def _safe_identifier(value: Any) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{0,100}", text):
        return ""
    return text


def load_evaluator_feedback(run_id: str) -> dict[str, Any]:
    """Load only the sanitized feedback contract for one exact audit run."""

    validated = _validate_run_id(run_id)
    if validated is None:
        raise ValueError("run_id обязателен")
    path = _output_root() / validated / "evaluation" / "feedback.json"
    if not path.is_file():
        raise FileNotFoundError(f"Обратная связь evaluator не найдена: {path}")
    if path.stat().st_size > MAX_FEEDBACK_BYTES:
        raise ValueError("Файл feedback превышает допустимый размер")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Feedback evaluator должен быть JSON-объектом")
    if raw.get("source") != "audit-evaluator":
        raise ValueError("Неизвестный источник feedback")
    if raw.get("source_run_id") != validated:
        raise ValueError("source_run_id в feedback не совпадает с аудитом")

    score = float(raw.get("score", 0.0))
    if not 0.0 <= score <= 100.0:
        raise ValueError("score в feedback должен быть от 0 до 100")
    categories = [
        item
        for item in _short_strings(raw.get("missed_categories"))
        if item in ALLOWED_CATEGORIES
    ]
    targets = [
        {
            "module": SAFE_TARGETS[category][0],
            "goal": SAFE_TARGETS[category][1],
        }
        for category in categories
    ]

    return {
        "source_run_id": validated,
        "evaluation_run_id": _safe_identifier(raw.get("evaluation_run_id")),
        "case_name": _safe_identifier(raw.get("case_name")),
        "status": (
            raw.get("status")
            if raw.get("status") in {"FOUND", "PARTIAL", "NOT_FOUND"}
            else "UNKNOWN"
        ),
        "score": score,
        "missed_categories": categories,
        "improvement_targets": targets,
        "feedback_path": str(path),
    }


def build_evaluator_review_task(feedback: dict[str, Any]) -> str:
    """Turn the allowlisted contract into a developer-review request."""

    context = {
        key: value
        for key, value in feedback.items()
        if key != "feedback_path"
    }
    return f"""Проведи post-evaluation review Audit Insight Agent.
Ниже только санитизированная обратная связь закрытого evaluator:
{json.dumps(context, ensure_ascii=False, indent=2)}

Не ищи и не читай ground truth, private benchmark или код evaluator.
Сопоставь категории пробелов с артефактами и общей логикой агента.
Если пробел подтверждается, внеси минимальное универсальное изменение и тест.
Не зашивай кейс, ID, валюты, суммы или ожидаемый ответ.
Если обратная связь не доказывает дефект общей логики, не вноси изменений.
""".strip()
