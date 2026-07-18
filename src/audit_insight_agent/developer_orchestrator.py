"""Isolated Ouroboros developer workflow without commit, push, or merge."""
from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .config import load_application_settings
from .developer_tools import (
    create_improvement_branch,
    preview_improvement,
    read_feedback,
)
from .models import ApplicationSettings
from .ouroboros import OuroborosConnectionError, OuroborosHTTPClient
from .ouroboros_tools import PROJECT_ROOT, _output_root, _validate_run_id
from .run_logging import RunEventLogger, utc_now


class OuroborosDeveloperOrchestrator:
    """Give Ouroboros an isolated worktree and retain only a reviewable patch."""

    TERMINAL_STATUSES = {"completed", "failed", "cancelled", "error"}
    logger = logging.getLogger("audit_insight.developer")

    def __init__(
        self,
        settings: ApplicationSettings | None = None,
        client: OuroborosHTTPClient | None = None,
    ) -> None:
        config_path = Path(
            os.getenv("AUDIT_INSIGHT_CONFIG", str(PROJECT_ROOT / "configs/config.yaml"))
        )
        self.settings = settings or load_application_settings(config_path)
        password = os.getenv(self.settings.ouroboros.password_env) or None
        self.client = client or OuroborosHTTPClient(
            self.settings.ouroboros.url, password=password
        )

    def run_with_updates(
        self, user_request: str, run_id: str
    ) -> Iterator[dict[str, Any]]:
        validated = _validate_run_id(run_id)
        if validated is None:
            raise ValueError("run_id обязателен")
        if not user_request.strip() or len(user_request) > 8000:
            raise ValueError("Задача developer mode должна содержать 1–8000 символов")

        run_dir = _output_root() / validated
        event_log = RunEventLogger(run_dir, validated)
        yield {"kind": "status", "message": "Создание изолированного worktree…"}
        worktree = create_improvement_branch(validated)
        event_log.event(
            "developer_worktree_ready",
            branch=worktree["branch"],
            worktree=worktree["worktree"],
            base_commit=worktree["base_commit"],
            status=worktree["status"],
        )

        try:
            feedback = read_feedback(validated)
        except FileNotFoundError:
            feedback = None

        yield {"kind": "status", "message": "Проверка Ouroboros server…"}
        self.client.health()
        prompt = self._task_prompt(
            user_request=user_request,
            run_id=validated,
            worktree=Path(worktree["worktree"]),
            feedback=feedback,
        )
        created = self.client.create_task(
            prompt,
            worktree["worktree"],
            self.settings.ouroboros.timeout_seconds,
        )
        task_id = str(created.get("task_id") or "")
        if not task_id:
            raise OuroborosConnectionError("Ouroboros не вернул task_id")
        event_log.event(
            "developer_task_created",
            task_id=task_id,
            branch=worktree["branch"],
        )

        deadline = time.monotonic() + self.settings.ouroboros.timeout_seconds
        previous_status = ""
        while time.monotonic() < deadline:
            task = self.client.get_task(task_id)
            status = str(task.get("status") or "unknown").lower()
            if status != previous_status:
                event_log.event(
                    "developer_task_status", task_id=task_id, status=status
                )
                yield {
                    "kind": "status",
                    "message": f"Developer task: **{status}** (`{task_id}`)",
                }
                previous_status = status
            if status in self.TERMINAL_STATUSES:
                if status != "completed":
                    detail = task.get("error") or task.get("result") or "без описания"
                    event_log.event(
                        "developer_task_failed",
                        level="ERROR",
                        task_id=task_id,
                        status=status,
                        detail=str(detail),
                    )
                    raise RuntimeError(
                        f"Developer task завершилась как {status}: {detail}"
                    )
                response = str(task.get("result") or "").strip()
                development_dir = run_dir / "development"
                development_dir.mkdir(parents=True, exist_ok=True)
                response_path = development_dir / "ouroboros_response.txt"
                response_path.write_text(response, encoding="utf-8")
                try:
                    preview = preview_improvement(validated)
                except Exception as error:
                    event_log.exception(
                        "developer_preview_rejected", error, task_id=task_id
                    )
                    raise
                if (
                    len(preview["changed_paths"])
                    > self.settings.self_improvement.max_changed_files
                ):
                    raise PermissionError(
                        "Ouroboros changed more files than self_improvement.max_changed_files"
                    )
                preview.update(
                    {
                        "task_id": task_id,
                        "response": response,
                        "response_path": str(response_path),
                        "completed_at": utc_now(),
                    }
                )
                event_log.event(
                    "developer_task_completed",
                    task_id=task_id,
                    branch=preview["branch"],
                    changed_paths=preview["changed_paths"],
                    patch_path=preview["patch_path"],
                    merged=False,
                    committed=False,
                    pushed=False,
                )
                yield {"kind": "result", "result": preview}
                return
            time.sleep(self.settings.ouroboros.poll_interval_seconds)
        raise TimeoutError(
            f"Ouroboros не завершил developer task {task_id} за "
            f"{self.settings.ouroboros.timeout_seconds} секунд"
        )

    def run_after_audit(
        self, audit_result: dict[str, Any], auditor_query: str
    ) -> Iterator[dict[str, Any]]:
        """Improve a detected systemic gap without requiring user-supplied run_id."""

        run_id = str(audit_result.get("run_id") or "")
        findings = [
            {
                "rule_id": item.get("rule_id"),
                "severity": item.get("severity"),
                "title": item.get("title"),
            }
            for item in list(audit_result.get("findings") or [])[:30]
            if isinstance(item, dict)
        ]
        context = {
            "auditor_query": auditor_query,
            "audit_status": audit_result.get("status"),
            "improvement_reason": audit_result.get("improvement_reason"),
            "execution_errors": list(audit_result.get("execution_errors") or [])[:20],
            "findings": findings,
        }
        task = f"""После завершённого аудита Ouroboros обнаружил системный пробел.
Контекс аудита:
{json.dumps(context, ensure_ascii=False, indent=2)}

Проверь, что пробел относится к общей логике, а не к конкретным значениям этого набора.
Если вывод был ошибочным, не меняй файлы и объясни это. Иначе внеси минимальное
универсальное изменение в код, правила, RAG или промпты и добавь тест.
Не зашивай имена файлов, валют, портфелей или ожидаемый ответ текущего кейса."""
        yield from self.run_with_updates(task, run_id)

    def _task_prompt(
        self,
        *,
        user_request: str,
        run_id: str,
        worktree: Path,
        feedback: dict[str, Any] | None,
    ) -> str:
        python_path = Path(self.settings.ouroboros.python_executable).expanduser()
        if not python_path.is_absolute():
            python_path = Path(self.settings.ouroboros.workspace).expanduser() / python_path
        return f"""Ты работаешь в developer mode Audit Insight Agent.
Твой единственный writable workspace: {worktree}
Ветка: improvement/{run_id}

Задача пользователя:
{json.dumps(user_request, ensure_ascii=False)}

Обезличенная обратная связь evaluator:
{json.dumps(feedback, ensure_ascii=False) if feedback else "отсутствует"}

Разрешено изменять только: src/, rules/, cases/, tests/, docs/, prompts/,
templates/, scripts/, README.md, pyproject.toml, requirements.txt.
Запрещено читать или менять ground truth, evaluator, .env, secrets,
configs/config.yaml, production-конфиги, data/ и knowledge/.
Не выполняй git commit, merge, rebase, push, pull, fetch, reset и checkout.
Не меняй исходную рабочую копию вне указанного worktree.

Изучи код, внеси минимальные обоснованные изменения и добавь тесты.
Запусти тесты командой:
{json.dumps([str(python_path.resolve()), "-m", "pytest", "-q"], ensure_ascii=False)}
В финале опиши изменённые файлы, тесты, риски и нерешённые вопросы.
Ничего не коммить и не мержить."""
