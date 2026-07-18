"""Client and orchestration for a separately running Ouroboros server."""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import load_application_settings
from .evaluator_adapter import run_external_evaluator
from .models import AgentRunResult, ApplicationSettings
from .ouroboros_tools import (
    PROJECT_ROOT,
    _output_root,
    _resolve_inside,
    _run_payload,
    _validate_run_id,
)


class OuroborosConnectionError(RuntimeError):
    """Raised when the external Ouroboros gateway cannot serve a task."""


class OuroborosHTTPClient:
    """Minimal client for the Ouroboros headless task API."""

    def __init__(self, base_url: str, password: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.password = password

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        timeout: float = 30,
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self.password:
            headers["X-Ouroboros-Password"] = self.password
        request = Request(
            f"{self.base_url}{path}", data=body, headers=headers, method=method
        )
        try:
            with urlopen(request, timeout=timeout) as response:
                decoded = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise OuroborosConnectionError(
                f"Ouroboros HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError, json.JSONDecodeError) as error:
            raise OuroborosConnectionError(
                f"Ouroboros недоступен по адресу {self.base_url}: {error}"
            ) from error
        if not isinstance(decoded, dict):
            raise OuroborosConnectionError("Ouroboros вернул некорректный JSON")
        return decoded

    def health(self) -> dict[str, Any]:
        return self._request("GET", "/api/health", timeout=10)

    def create_task(
        self, description: str, workspace: str, timeout_seconds: int
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            "/api/tasks",
            {
                "description": description,
                "workspace_root": workspace,
                "workspace_mode": "external",
                "memory_mode": "forked",
                "attachments": [],
                "source": "audit-insight-web",
                "timeout_sec": timeout_seconds,
                "metadata": {
                    "source": "audit-insight-web",
                    "delegation_role": "root",
                },
            },
        )

    def get_task(self, task_id: str) -> dict[str, Any]:
        return self._request("GET", f"/api/tasks/{task_id}")


class OuroborosOrchestrator:
    """Runs Audit Insight through the external Ouroboros task gateway."""

    TERMINAL_STATUSES = {"completed", "failed", "cancelled", "error"}
    RUN_ID_PATTERN = re.compile(r"\bRUN-[A-Za-z0-9_.-]{1,76}\b")

    def __init__(
        self,
        settings: ApplicationSettings | None = None,
        client: OuroborosHTTPClient | None = None,
        result_loader: Callable[[str], dict[str, Any]] | None = None,
    ) -> None:
        config_path = Path(
            os.getenv("AUDIT_INSIGHT_CONFIG", str(PROJECT_ROOT / "configs/config.yaml"))
        )
        self.settings = settings or load_application_settings(config_path)
        password = os.getenv(self.settings.ouroboros.password_env) or None
        self.client = client or OuroborosHTTPClient(
            self.settings.ouroboros.url, password=password
        )
        self.result_loader = result_loader or self._load_result

    def run(self, user_request: str, case_name: str) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        for event in self.run_with_updates(user_request, case_name):
            if event["kind"] == "result":
                result = event["result"]
        if result is None:
            raise RuntimeError("Ouroboros не вернул результат аудита")
        return result

    def run_with_updates(
        self, user_request: str, case_name: str
    ) -> Iterator[dict[str, Any]]:
        yield {"kind": "status", "message": "Проверка соединения с Ouroboros…"}
        self.client.health()
        request_path = self._write_task_request(user_request, case_name)
        prompt = self._task_prompt(request_path)
        created = self.client.create_task(
            prompt,
            str(Path(self.settings.ouroboros.workspace).expanduser().resolve()),
            self.settings.ouroboros.timeout_seconds,
        )
        task_id = str(created.get("task_id") or "")
        if not task_id:
            raise OuroborosConnectionError("Ouroboros не вернул task_id")

        yield {
            "kind": "status",
            "message": f"Ouroboros выполняет задачу `{task_id}`…",
        }
        deadline = time.monotonic() + self.settings.ouroboros.timeout_seconds
        previous_status = ""
        while time.monotonic() < deadline:
            task = self.client.get_task(task_id)
            status = str(task.get("status") or "unknown").lower()
            if status != previous_status:
                yield {
                    "kind": "status",
                    "message": f"Ouroboros: **{status}** (`{task_id}`)",
                }
                previous_status = status
            if status in self.TERMINAL_STATUSES:
                if status != "completed":
                    detail = task.get("error") or task.get("result") or "без описания"
                    raise RuntimeError(f"Задача Ouroboros завершилась как {status}: {detail}")
                task_result = task.get("result")
                run_id = self._extract_run_id(task_result)
                result = self.result_loader(run_id)
                result["ouroboros_answer"] = self._extract_auditor_answer(task_result)
                run_dir = Path(result["candidate_findings_path"]).parent
                result["evaluation"] = run_external_evaluator(
                    case_name=case_name,
                    run_id=run_id,
                    candidate_findings=result["candidate_findings_path"],
                    run_dir=run_dir,
                )
                result["answer"] = self._answer(result)
                yield {"kind": "result", "result": result}
                return
            time.sleep(self.settings.ouroboros.poll_interval_seconds)
        raise TimeoutError(
            f"Ouroboros не завершил задачу {task_id} за "
            f"{self.settings.ouroboros.timeout_seconds} секунд"
        )

    def _write_task_request(self, user_request: str, case_name: str) -> Path:
        request_root = PROJECT_ROOT / "outputs" / "requests"
        request_root.mkdir(parents=True, exist_ok=True)
        path = request_root / f"REQ-{uuid.uuid4().hex}.json"
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {"case_name": case_name, "auditor_query": user_request},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def _task_prompt(self, request_path: Path) -> str:
        workspace = Path(self.settings.ouroboros.workspace).expanduser().resolve()
        python_path = Path(self.settings.ouroboros.python_executable).expanduser()
        if not python_path.is_absolute():
            python_path = workspace / python_path
        relative_request = request_path.resolve().relative_to(PROJECT_ROOT)
        command = [
            str(python_path.resolve()),
            "scripts/ouroboros_audit.py",
            "--request",
            str(relative_request),
        ]
        return f"""Ты выступаешь как аудитор и оркестратор Audit Insight Agent.
Это боевой запуск: не изменяй код, правила, конфигурацию и evaluator.
Выполни команду в workspace точно с таким argv:
{json.dumps(command, ensure_ascii=False)}
Команда запустит табличные правила, RAG по документам и создаст evidence, candidate_findings.json и report.md.
После запуска прочитай только созданные report.md и candidate_findings.json.
Дай краткий аудиторский вывод, основанный только на этих артефактах.
Последняя строка ответа обязана иметь вид AUDIT_RUN_ID=<run_id>.
Не придумывай факты, run_id и не читай закрытый ground truth."""

    @staticmethod
    def _extract_auditor_answer(task_result: Any) -> str:
        text = (
            json.dumps(task_result, ensure_ascii=False)
            if isinstance(task_result, dict)
            else str(task_result or "")
        )
        return re.sub(r"(?m)^AUDIT_RUN_ID=.*$", "", text).strip()

    @classmethod
    def _extract_run_id(cls, task_result: Any) -> str:
        if isinstance(task_result, dict):
            direct = task_result.get("run_id")
            text = json.dumps(task_result, ensure_ascii=False)
            if direct:
                return str(direct)
        else:
            text = str(task_result or "")
        match = cls.RUN_ID_PATTERN.search(text)
        if not match:
            raise RuntimeError("В результате Ouroboros отсутствует AUDIT_RUN_ID")
        return match.group(0)

    @staticmethod
    def _load_result(run_id: str) -> dict[str, Any]:
        validated = _validate_run_id(run_id)
        if validated is None:
            raise ValueError("run_id обязателен")
        root = _output_root()
        run_dir = _resolve_inside(root / validated, root)
        candidate_path = run_dir / "candidate_findings.json"
        if not candidate_path.is_file():
            raise FileNotFoundError(f"Результат аудита не найден: {candidate_path}")
        result = AgentRunResult.model_validate_json(candidate_path.read_text("utf-8"))
        paths = {
            "candidate_findings": candidate_path,
            "report": run_dir / "report.md",
            "run_manifest": run_dir / "run_manifest.json",
        }
        rag_context = run_dir / "rag_context.json"
        if rag_context.is_file():
            paths["rag_context"] = rag_context
        return _run_payload(result, paths)

    @staticmethod
    def _answer(result: dict[str, Any]) -> str:
        findings = result.get("findings", [])
        severity_counts = Counter(item.get("severity", "UNKNOWN") for item in findings)
        severity_text = ", ".join(
            f"{severity}: {count}"
            for severity, count in sorted(severity_counts.items())
        ) or "нет"
        titles = [item.get("title", "Без названия") for item in findings[:5]]
        lines = []
        if result.get("ouroboros_answer"):
            lines.extend([str(result["ouroboros_answer"]), "---"])
        lines.extend([
            f"Анализ завершён со статусом {result['status']}.",
            f"Сформировано выводов: {len(findings)}; по критичности: {severity_text}.",
        ])
        if titles:
            lines.append("Основные выводы: " + "; ".join(titles) + ".")
        errors = result.get("execution_errors", [])
        if errors:
            lines.append(f"Ошибок выполнения: {len(errors)}. Подробности сохранены в отчёте.")
        evaluation = result.get("evaluation", {})
        if evaluation.get("status") == "NOT_CONFIGURED":
            lines.append("Внешний audit-evaluator не настроен для этого окружения.")
        elif evaluation.get("status") == "COMPLETED":
            lines.append("Внешняя оценка завершена; обезличенная обратная связь доступна developer API.")
        return "\n\n".join(lines)
