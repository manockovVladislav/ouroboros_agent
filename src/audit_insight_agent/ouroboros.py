"""Client and orchestration for a separately running Ouroboros server."""
from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .config import load_application_settings
from .evaluator_adapter import run_external_evaluator
from .models import AgentRunResult, ApplicationSettings
from .report_generator import refresh_run_manifest_files
from .run_logging import RunEventLogger, append_jsonl, utc_now, write_chat_history
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

    def get_task_events(
        self, task_id: str, cursor: int = 0, wait_seconds: int = 0
    ) -> list[dict[str, Any]]:
        query = urlencode({"cursor": max(0, cursor), "wait": max(0, wait_seconds)})
        headers = {"Accept": "text/event-stream"}
        if self.password:
            headers["X-Ouroboros-Password"] = self.password
        request = Request(
            f"{self.base_url}/api/tasks/{task_id}/events?{query}",
            headers=headers,
            method="GET",
        )
        try:
            with urlopen(request, timeout=max(10, wait_seconds + 5)) as response:
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[:500]
            raise OuroborosConnectionError(
                f"Ouroboros events HTTP {error.code}: {detail}"
            ) from error
        except (URLError, TimeoutError) as error:
            raise OuroborosConnectionError(
                f"Ouroboros events недоступны: {error}"
            ) from error
        events = []
        for line in body.splitlines():
            if not line.startswith("data: "):
                continue
            try:
                event = json.loads(line[6:])
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


class OuroborosOrchestrator:
    """Runs Audit Insight through the external Ouroboros task gateway."""

    TERMINAL_STATUSES = {"completed", "failed", "cancelled", "error"}
    RUN_ID_PATTERN = re.compile(r"\bRUN-[A-Za-z0-9_.-]{1,76}\b")
    CLARIFICATION_PREFIX = "AUDIT_CLARIFICATION_REQUIRED="
    IMPROVEMENT_PREFIX = "AUDIT_IMPROVEMENT_NEEDED="
    logger = logging.getLogger("audit_insight.ouroboros")

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
        self.logger.info("Checking Ouroboros health at %s", self.settings.ouroboros.url)
        self.client.health()
        request_path = self._write_task_request(user_request, case_name)
        request_events = request_path.with_suffix(".events.jsonl")
        append_jsonl(
            request_events,
            {
                "timestamp": utc_now(),
                "event": "web_request_received",
                "request_id": request_path.stem,
                "case_name": case_name,
                "auditor_query_length": len(user_request),
            },
        )
        prompt = self._task_prompt(request_path)
        created = self.client.create_task(
            prompt,
            str(Path(self.settings.ouroboros.workspace).expanduser().resolve()),
            self.settings.ouroboros.timeout_seconds,
        )
        task_id = str(created.get("task_id") or "")
        if not task_id:
            raise OuroborosConnectionError("Ouroboros не вернул task_id")
        append_jsonl(
            request_events,
            {
                "timestamp": utc_now(),
                "event": "ouroboros_task_created",
                "request_id": request_path.stem,
                "task_id": task_id,
            },
        )
        self.logger.info(
            "Ouroboros task created task_id=%s request_id=%s",
            task_id,
            request_path.stem,
        )

        yield {
            "kind": "status",
            "message": f"Ouroboros выполняет задачу `{task_id}`…",
        }
        deadline = time.monotonic() + self.settings.ouroboros.timeout_seconds
        previous_status = ""
        event_cursor = 0
        previous_progress = ""
        while time.monotonic() < deadline:
            task = self.client.get_task(task_id)
            status = str(task.get("status") or "unknown").lower()
            if status != previous_status:
                append_jsonl(
                    request_events,
                    {
                        "timestamp": utc_now(),
                        "event": "ouroboros_task_status",
                        "request_id": request_path.stem,
                        "task_id": task_id,
                        "status": status,
                    },
                )
                yield {
                    "kind": "status",
                    "message": f"Ouroboros: **{status}** (`{task_id}`)",
                }
                previous_status = status
            event_reader = getattr(self.client, "get_task_events", None)
            if callable(event_reader):
                try:
                    task_events = event_reader(task_id, event_cursor, 0)
                except OuroborosConnectionError:
                    task_events = []
                for task_event in task_events:
                    event_cursor = max(
                        event_cursor, int(task_event.get("seq") or 0)
                    )
                    progress = self._meaningful_progress(task_event)
                    if progress and progress != previous_progress:
                        yield {"kind": "status", "message": progress}
                        previous_progress = progress
            if status in self.TERMINAL_STATUSES:
                if status != "completed":
                    detail = task.get("error") or task.get("result") or "без описания"
                    self.logger.error(
                        "Ouroboros task failed task_id=%s status=%s detail=%s",
                        task_id,
                        status,
                        detail,
                    )
                    raise RuntimeError(f"Задача Ouroboros завершилась как {status}: {detail}")
                task_result = task.get("result")
                clarification = self._protocol_value(
                    task_result, self.CLARIFICATION_PREFIX
                )
                if clarification:
                    question = (
                        str(clarification.get("question") or "").strip()
                        if isinstance(clarification, dict)
                        else str(clarification).strip()
                    )
                    if not question:
                        raise RuntimeError("Ouroboros вернул пустой вопрос")
                    yield {
                        "kind": "clarification",
                        "question": question,
                        "task_id": task_id,
                    }
                    return
                run_id = self._extract_run_id(task_result)
                result = self.result_loader(run_id)
                result["ouroboros_answer"] = self._extract_auditor_answer(task_result)
                improvement = self._protocol_value(
                    task_result, self.IMPROVEMENT_PREFIX
                )
                result["improvement_needed"] = bool(improvement)
                result["improvement_reason"] = (
                    str(improvement.get("reason") or "").strip()
                    if isinstance(improvement, dict)
                    else ""
                )
                run_dir = Path(result["candidate_findings_path"]).parent
                result["evaluation"] = run_external_evaluator(
                    case_name=case_name,
                    run_id=run_id,
                    candidate_findings=result["candidate_findings_path"],
                    run_dir=run_dir,
                )
                result["answer"] = self._answer(result)
                chat_path = write_chat_history(
                    run_dir,
                    run_id=run_id,
                    case_name=case_name,
                    task_id=task_id,
                    user_request=user_request,
                    ouroboros_answer=result["answer"],
                )
                event_log = RunEventLogger(run_dir, run_id)
                event_log.event(
                    "ouroboros_task_completed",
                    task_id=task_id,
                    request_id=request_path.stem,
                    chat_path=str(chat_path),
                )
                if (run_dir / "run_manifest.json").is_file():
                    refresh_run_manifest_files(run_dir)
                result["chat_path"] = str(chat_path)
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
        replica_name = self._replica_from_query(user_request)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "request_id": path.stem,
                    "created_at": utc_now(),
                    "case_name": case_name,
                    "auditor_query": user_request,
                    "replica_name": replica_name,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        temporary.replace(path)
        return path

    def _replica_from_query(self, user_request: str) -> str | None:
        matches = [
            alias
            for alias in self.settings.databases.connections
            if re.search(
                rf"(?<![\w.-]){re.escape(alias)}(?![\w.-])",
                user_request,
                re.IGNORECASE,
            )
        ]
        if len(matches) > 1:
            raise ValueError(
                "В запросе указано несколько реплик: " + ", ".join(matches)
            )
        return matches[0] if matches else None

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
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        selected_replica = request_payload.get("replica_name") or "не указана"
        clarification_enabled = self.settings.self_improvement.allow_blocking_clarification
        return f"""Ты выступаешь как аудитор и главный оркестратор Audit Insight Agent.
Это боевой запуск: не изменяй код, правила, конфигурацию и evaluator.
Снача изучи описания источников, имена файлов, схемы и документы knowledge.
Не задавай вопросы о формате отчёта, методе или несущественных деталях: прими разумное
допущение и продолжай. Один вопрос разрешён только до расчётов, если назначение данных нельзя установить
и ошибочная трактовка изменит смысл аудита. Вопросы разрешены: {str(clarification_enabled).lower()}.
Точно названная в запросе зарегистрированная реплика: {selected_replica}.
В этом крайнем случае не запускай расчёты и верни одну последнюю строку:
AUDIT_CLARIFICATION_REQUIRED={{"question":"один конкретный вопрос","reason":"почему без ответа нельзя"}}

Выполни команду в workspace точно с таким argv:
{json.dumps(command, ensure_ascii=False)}
Команда запустит табличные правила, RAG по документам и создаст evidence, candidate_findings.json и report.md.
После запуска прочитай только созданные report.md и candidate_findings.json.
Дай краткий аудиторский вывод только после раздела Evidence critique. Не выдавай POTENTIAL_RISK за
найденное нарушение. Для каждого CONFIRMED укажи, что найдено, в какой таблице/документе и какой
факт это подтверждает. Затем кратко изложи ранжированный Prioritized audit plan. Опирайся только на эти артефакты.
Отдельно оцени, не помешал ли
системный пробел в общем коде, правилах, RAG или промптах. Не считай пробелом отсутствие нарушений или
нехватку данных. Если общая возможность реального аудита отсутствовала, добавь строку:
AUDIT_IMPROVEMENT_NEEDED={{"reason":"краткое описание общего пробела"}}
Иначе добавь AUDIT_IMPROVEMENT_NEEDED=false.
Последняя строка ответа обязана иметь вид AUDIT_RUN_ID=<run_id>.
Не придумывай факты, run_id и не читай закрытый ground truth."""

    @staticmethod
    def _extract_auditor_answer(task_result: Any) -> str:
        text = (
            json.dumps(task_result, ensure_ascii=False)
            if isinstance(task_result, dict)
            else str(task_result or "")
        )
        return re.sub(
            r"(?m)^AUDIT_(?:RUN_ID|IMPROVEMENT_NEEDED|CLARIFICATION_REQUIRED)=.*$",
            "",
            text,
        ).strip()

    @staticmethod
    def _protocol_value(task_result: Any, prefix: str) -> Any:
        text = (
            json.dumps(task_result, ensure_ascii=False)
            if isinstance(task_result, dict)
            else str(task_result or "")
        )
        for line in text.splitlines():
            if not line.strip().startswith(prefix):
                continue
            raw = line.strip()[len(prefix) :].strip()
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return None

    @staticmethod
    def _meaningful_progress(event: dict[str, Any]) -> str:
        event_type = str(event.get("type") or "")
        data = event.get("data") if isinstance(event.get("data"), dict) else {}
        if event_type == "tool_call":
            tool = str(data.get("tool") or data.get("name") or "").strip()
            labels = {
                "run_shell": "Запускаю аудиторские расчёты…",
                "read_file": "Изучаю структуру данных и документы…",
                "list_files": "Определяю доступные источники…",
            }
            if tool:
                return labels.get(tool, f"Ouroboros выполняет: `{tool}`…")
        if event_type in {"progress", "message"}:
            value = next(
                (
                    str(data.get(key) or "").strip()
                    for key in ("content", "message", "text")
                    if str(data.get(key) or "").strip()
                ),
                "",
            )
            value = re.sub(r"\s+", " ", value)
            if value and len(value) <= 240:
                return value
        return ""

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
        reviews = result.get("finding_reviews", [])
        confirmed_ids = {
            item.get("finding_id")
            for item in reviews
            if item.get("verdict") == "CONFIRMED"
        }
        confirmed = (
            [item for item in findings if item.get("finding_id") in confirmed_ids]
            if reviews
            else findings
        )
        severity_counts = Counter(item.get("severity", "UNKNOWN") for item in confirmed)
        severity_text = ", ".join(
            f"{severity}: {count}"
            for severity, count in sorted(severity_counts.items())
        ) or "нет"
        titles = [item.get("title", "Без названия") for item in confirmed[:5]]
        lines = []
        if result.get("ouroboros_answer"):
            lines.extend([str(result["ouroboros_answer"]), "---"])
        lines.extend([
            f"Анализ завершён со статусом {result['status']}.",
            f"Подтверждённых выводов: {len(confirmed)}; по критичности: {severity_text}.",
        ])
        if titles:
            lines.append("Подтверждёно: " + "; ".join(titles) + ".")
        potential_count = sum(
            item.get("status") == "POTENTIAL_RISK"
            for item in result.get("audit_plan", [])
        )
        if potential_count:
            lines.append(
                f"Потенциальных направлений для дополнительной проверки: {potential_count}."
            )
        errors = result.get("execution_errors", [])
        if errors:
            lines.append(f"Ошибок выполнения: {len(errors)}. Подробности сохранены в отчёте.")
        evaluation = result.get("evaluation", {})
        if evaluation.get("status") == "NOT_CONFIGURED":
            lines.append("Внешний audit-evaluator не настроен для этого окружения.")
        elif evaluation.get("status") == "COMPLETED":
            lines.append("Внешняя оценка завершена; обезличенная обратная связь доступна developer API.")
        return "\n\n".join(lines)
