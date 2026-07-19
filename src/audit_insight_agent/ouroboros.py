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
from .models import AgentRunResult, ApplicationSettings
from .report_generator import refresh_run_manifest_files, write_narrative_report
from .run_logging import RunEventLogger, append_jsonl, utc_now, write_chat_history
from .ouroboros_tools import (
    PROJECT_ROOT,
    _output_root,
    _resolve_inside,
    _run_payload,
    _validate_run_id,
)


def _counted(value: int, one: str, few: str, many: str) -> str:
    remainder = value % 100
    if 11 <= remainder <= 14:
        form = many
    elif value % 10 == 1:
        form = one
    elif 2 <= value % 10 <= 4:
        form = few
    else:
        form = many
    return f"{value} {form}"


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
    REQUEST_ID_PATTERN = re.compile(r"REQ-[a-f0-9]{32}")
    RESULT_FINALIZATION_GRACE_SECONDS = 15.0
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

    def run(self, user_request: str) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        for event in self.run_with_updates(user_request):
            if event["kind"] == "result":
                result = event["result"]
        if result is None:
            raise RuntimeError("Ouroboros не вернул результат аудита")
        return result

    def run_with_updates(self, user_request: str) -> Iterator[dict[str, Any]]:
        yield {
            "kind": "status",
            "message": (
                "**Проверяю готовность агента.** Убеждаюсь, что сервис "
                "доступен и может принять задачу без потери контекста."
            ),
        }
        self.logger.info("Checking Ouroboros health at %s", self.settings.ouroboros.url)
        self.client.health()
        request_path = self._write_task_request(user_request)
        request_payload = json.loads(request_path.read_text(encoding="utf-8"))
        clarification_allowed = self._clarification_allowed(
            user_request, request_payload.get("replica_name")
        )
        request_events = request_path.with_suffix(".events.jsonl")
        append_jsonl(
            request_events,
            {
                "timestamp": utc_now(),
                "event": "web_request_received",
                "request_id": request_path.stem,
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
            "message": (
                "**Анализ запущен.** Агент определяет доступные источники, "
                "выполняет правила аудита и собирает доказательства для каждого вывода."
            ),
            "request_id": request_path.stem,
            "task_id": task_id,
        }
        deadline = time.monotonic() + self.settings.ouroboros.timeout_seconds
        previous_status = ""
        event_cursor = 0
        previous_progress = ""
        clarification_retry_used = False
        server_restart_retry_used = False
        connection_lost = False
        last_keepalive = time.monotonic()
        saved_result_seen_at: float | None = None
        while time.monotonic() < deadline:
            try:
                task = self.client.get_task(task_id)
            except OuroborosConnectionError as error:
                recovered = self._recover_completed_audit(
                    request_path=request_path,
                    user_request=user_request,
                    task_id=task_id,
                )
                if recovered is not None:
                    append_jsonl(
                        request_events,
                        {
                            "timestamp": utc_now(),
                            "event": "audit_recovered_after_connection_loss",
                            "request_id": request_path.stem,
                            "task_id": task_id,
                            "run_id": recovered["run_id"],
                        },
                    )
                    yield {
                        "kind": "status",
                        "message": (
                            "**Связь с сервером потеряна, но аудит восстановлен.** "
                            "Расчёты и отчёт уже были сохранены; повторный запуск не требуется."
                        ),
                    }
                    yield {"kind": "result", "result": recovered}
                    return
                if not connection_lost:
                    self.logger.warning(
                        "Ouroboros connection lost task_id=%s: %s", task_id, error
                    )
                    append_jsonl(
                        request_events,
                        {
                            "timestamp": utc_now(),
                            "event": "ouroboros_connection_lost",
                            "request_id": request_path.stem,
                            "task_id": task_id,
                            "error": str(error),
                        },
                    )
                    yield {
                        "kind": "status",
                        "message": (
                            "**Связь с сервером временно потеряна.** Интерфейс "
                            "сохраняет текущие результаты и продолжает попытки восстановления."
                        ),
                    }
                    connection_lost = True
                if time.monotonic() - last_keepalive >= 15:
                    yield {
                        "kind": "status",
                        "message": (
                            "**Продолжаю восстановление связи.** Интерфейс активен, "
                            "расчёты не запускаются повторно."
                        ),
                    }
                    last_keepalive = time.monotonic()
                time.sleep(max(1.0, self.settings.ouroboros.poll_interval_seconds))
                continue
            if connection_lost:
                append_jsonl(
                    request_events,
                    {
                        "timestamp": utc_now(),
                        "event": "ouroboros_connection_restored",
                        "request_id": request_path.stem,
                        "task_id": task_id,
                    },
                )
                yield {
                    "kind": "status",
                    "message": (
                        "**Связь с сервером восстановлена.** Продолжаю задачу "
                        "с сохранённого состояния."
                    ),
                }
                connection_lost = False
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
                    "message": self._status_message(status),
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
                    recovered = self._recover_completed_audit(
                        request_path=request_path,
                        user_request=user_request,
                        task_id=task_id,
                    )
                    if recovered is not None:
                        append_jsonl(
                            request_events,
                            {
                                "timestamp": utc_now(),
                                "event": "audit_recovered_after_task_failure",
                                "request_id": request_path.stem,
                                "task_id": task_id,
                                "task_status": status,
                                "run_id": recovered["run_id"],
                            },
                        )
                        yield {
                            "kind": "status",
                            "message": (
                                "**Расчёты завершены и восстановлены.** Внешний "
                                "агент потерял связь уже после сохранения аудита; "
                                "повторный запуск не требуется."
                            ),
                        }
                        yield {"kind": "result", "result": recovered}
                        return
                    detail = task.get("error") or task.get("result") or "без описания"
                    detail_text = str(detail)
                    if (
                        status == "cancelled"
                        and "Server shut down" in detail_text
                        and not server_restart_retry_used
                    ):
                        server_restart_retry_used = True
                        yield {
                            "kind": "status",
                            "message": (
                                "**Сервер Ouroboros был перезапущен.** Прежняя "
                                "задача подтверждённо остановлена; один раз повторяю "
                                "тот же аудит по сохранённому запросу."
                            ),
                        }
                        restarted = self.client.create_task(
                            self._task_prompt(request_path),
                            str(
                                Path(self.settings.ouroboros.workspace)
                                .expanduser()
                                .resolve()
                            ),
                            self.settings.ouroboros.timeout_seconds,
                        )
                        previous_task_id = task_id
                        task_id = str(restarted.get("task_id") or "")
                        if not task_id:
                            raise OuroborosConnectionError(
                                "Ouroboros не вернул task_id после перезапуска"
                            )
                        append_jsonl(
                            request_events,
                            {
                                "timestamp": utc_now(),
                                "event": "ouroboros_task_created",
                                "request_id": request_path.stem,
                                "task_id": task_id,
                                "replaces_task_id": previous_task_id,
                                "reason": "server_shutdown_retry",
                            },
                        )
                        self.logger.warning(
                            "Ouroboros task restarted after server shutdown "
                            "previous_task_id=%s task_id=%s request_id=%s",
                            previous_task_id,
                            task_id,
                            request_path.stem,
                        )
                        deadline = (
                            time.monotonic()
                            + self.settings.ouroboros.timeout_seconds
                        )
                        previous_status = ""
                        event_cursor = 0
                        previous_progress = ""
                        connection_lost = False
                        saved_result_seen_at = None
                        continue
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
                    if not clarification_allowed:
                        if clarification_retry_used:
                            raise RuntimeError(
                                "Ouroboros повторно запросил ненужное "
                                "уточнение вместо запуска аудита"
                            )
                        clarification_retry_used = True
                        append_jsonl(
                            request_events,
                            {
                                "timestamp": utc_now(),
                                "event": "clarification_rejected",
                                "request_id": request_path.stem,
                                "task_id": task_id,
                                "question": question,
                                "reason": "audit_scope_is_already_defined",
                            },
                        )
                        yield {
                            "kind": "status",
                            "message": (
                                "Ouroboros запросил лишнее уточнение; "
                                "область уже задана, повторяю запуск…"
                            ),
                        }
                        retry = self.client.create_task(
                            self._task_prompt(request_path, force_execution=True),
                            str(
                                Path(self.settings.ouroboros.workspace)
                                .expanduser()
                                .resolve()
                            ),
                            self.settings.ouroboros.timeout_seconds,
                        )
                        task_id = str(retry.get("task_id") or "")
                        if not task_id:
                            raise OuroborosConnectionError(
                                "Ouroboros не вернул task_id для повторного запуска"
                            )
                        previous_status = ""
                        event_cursor = 0
                        previous_progress = ""
                        continue
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
                result["answer"] = self._answer(result)
                report_path = write_narrative_report(
                    result.get("report_path") or run_dir / "report.md",
                    result["answer"],
                )
                chat_path = write_chat_history(
                    run_dir,
                    run_id=run_id,
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
                    report_path=str(report_path),
                )
                if (run_dir / "run_manifest.json").is_file():
                    refresh_run_manifest_files(run_dir)
                result["chat_path"] = str(chat_path)
                yield {"kind": "result", "result": result}
                return
            sidecar = request_path.with_suffix(".result.json")
            if sidecar.is_file():
                if saved_result_seen_at is None:
                    saved_result_seen_at = time.monotonic()
                    yield {
                        "kind": "status",
                        "message": (
                            "**Расчёты и отчёт готовы.** Даю внешнему агенту "
                            "короткое время на финальную сборку вывода."
                        ),
                    }
                elif (
                    time.monotonic() - saved_result_seen_at
                    >= self.RESULT_FINALIZATION_GRACE_SECONDS
                ):
                    recovered = self._recover_completed_audit(
                        request_path=request_path,
                        user_request=user_request,
                        task_id=task_id,
                    )
                    if recovered is not None:
                        append_jsonl(
                            request_events,
                            {
                                "timestamp": utc_now(),
                                "event": "audit_returned_while_task_finalizing",
                                "request_id": request_path.stem,
                                "task_id": task_id,
                                "task_status": status,
                                "run_id": recovered["run_id"],
                            },
                        )
                        yield {
                            "kind": "status",
                            "message": (
                                "**Вывод собран из сохранённого аудита.** "
                                "Внешняя финализация затянулась, поэтому интерфейс "
                                "возвращает уже готовый результат."
                            ),
                        }
                        yield {"kind": "result", "result": recovered}
                        return
            if time.monotonic() - last_keepalive >= 15:
                yield {
                    "kind": "status",
                    "message": (
                        "**Анализ продолжается.** Соединение с интерфейсом "
                        "активно; итоговый результат ещё не сформирован."
                    ),
                }
                last_keepalive = time.monotonic()
            time.sleep(self.settings.ouroboros.poll_interval_seconds)
        raise TimeoutError(
            f"Ouroboros не завершил задачу {task_id} за "
            f"{self.settings.ouroboros.timeout_seconds} секунд"
        )

    def recover_request_with_updates(
        self, request_id: str
    ) -> Iterator[dict[str, Any]]:
        """Reattach a new browser session to a task created before a reload."""

        request_id = str(request_id or "").strip()
        if not self.REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ValueError("Некорректный request_id для восстановления")
        request_path = PROJECT_ROOT / "outputs" / "requests" / f"{request_id}.json"
        if not request_path.is_file():
            raise FileNotFoundError("Сохранённый web-запрос не найден")
        payload = json.loads(request_path.read_text(encoding="utf-8"))
        user_request = str(payload.get("auditor_query") or "").strip()
        task_id = self._saved_task_id(request_path)
        if not user_request or not task_id:
            raise RuntimeError("В сохранённом запросе нет данных для восстановления")

        yield {
            "kind": "status",
            "message": (
                "**Восстанавливаю активный анализ.** Страница была перезагружена, "
                "но задача не запускается повторно; жду её сохранённый результат."
            ),
            "request_id": request_id,
            "task_id": task_id,
        }
        deadline = time.monotonic() + self.settings.ouroboros.timeout_seconds
        last_keepalive = time.monotonic()
        while time.monotonic() < deadline:
            recovered = self._recover_completed_audit(
                request_path=request_path,
                user_request=user_request,
                task_id=task_id,
            )
            if recovered is not None:
                append_jsonl(
                    request_path.with_suffix(".events.jsonl"),
                    {
                        "timestamp": utc_now(),
                        "event": "web_session_reattached",
                        "request_id": request_id,
                        "task_id": task_id,
                        "run_id": recovered["run_id"],
                    },
                )
                yield {"kind": "result", "result": recovered}
                return
            try:
                task = self.client.get_task(task_id)
            except OuroborosConnectionError:
                task = {}
            status = str(task.get("status") or "").lower()
            if status in self.TERMINAL_STATUSES and status != "completed":
                detail = task.get("error") or task.get("result") or "без описания"
                raise RuntimeError(
                    f"Задача Ouroboros завершилась как {status}: {detail}"
                )
            if time.monotonic() - last_keepalive >= 15:
                yield {
                    "kind": "status",
                    "message": (
                        "**Анализ продолжается.** Новая web-сессия подключена "
                        "к прежней задаче и ждёт её результат."
                    ),
                    "request_id": request_id,
                    "task_id": task_id,
                }
                last_keepalive = time.monotonic()
            time.sleep(max(1.0, self.settings.ouroboros.poll_interval_seconds))
        raise TimeoutError(
            f"Не удалось восстановить результат задачи {task_id} за "
            f"{self.settings.ouroboros.timeout_seconds} секунд"
        )

    @staticmethod
    def _saved_task_id(request_path: Path) -> str:
        events_path = request_path.with_suffix(".events.jsonl")
        if not events_path.is_file():
            return ""
        task_id = ""
        for line in events_path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "ouroboros_task_created" and event.get("task_id"):
                task_id = str(event["task_id"])
        return task_id

    def _recover_completed_audit(
        self,
        *,
        request_path: Path,
        user_request: str,
        task_id: str,
    ) -> dict[str, Any] | None:
        sidecar = request_path.with_suffix(".result.json")
        if not sidecar.is_file():
            return None
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            run_id = str(payload.get("run_id") or "")
            if not self.RUN_ID_PATTERN.fullmatch(run_id):
                return None
            result = self.result_loader(run_id)
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            self.logger.exception(
                "Could not recover completed audit request_id=%s", request_path.stem
            )
            return None
        result["ouroboros_answer"] = ""
        result["answer"] = self._answer(result)
        result["improvement_needed"] = False
        result["improvement_reason"] = ""
        result["recovered_after_connection_loss"] = True
        result["recovered_from_saved_result"] = True
        run_dir = Path(result["candidate_findings_path"]).parent
        chat_path = write_chat_history(
            run_dir,
            run_id=run_id,
            task_id=task_id,
            user_request=user_request,
            ouroboros_answer=result["answer"],
        )
        RunEventLogger(run_dir, run_id).event(
            "ouroboros_result_recovered",
            task_id=task_id,
            request_id=request_path.stem,
            sidecar_path=str(sidecar),
            chat_path=str(chat_path),
        )
        if (run_dir / "run_manifest.json").is_file():
            refresh_run_manifest_files(run_dir)
        result["chat_path"] = str(chat_path)
        return result

    def _write_task_request(self, user_request: str) -> Path:
        request_root = PROJECT_ROOT / "outputs" / "requests"
        request_root.mkdir(parents=True, exist_ok=True)
        path = request_root / f"REQ-{uuid.uuid4().hex}.json"
        replica_name = self._replica_from_query(user_request)
        payload = {
            "request_id": path.stem,
            "created_at": utc_now(),
            "auditor_query": user_request,
            "input_mode": (
                "local_files_and_replica" if replica_name else "local_files"
            ),
            "scope_complete": True,
            "local_roots": ["data/", "knowledge/"],
            "database_access": bool(replica_name),
        }
        if replica_name:
            payload["replica_name"] = replica_name
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
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

    def _clarification_allowed(
        self, user_request: str, replica_name: str | None
    ) -> bool:
        if not self.settings.self_improvement.allow_blocking_clarification:
            return False
        if not self.settings.databases.connections:
            return False
        normalized = user_request.casefold()
        fixed_scope_markers = (
            "data/",
            "knowledge/",
            "локальн",
            "только по ним",
            "все файл",
            "полный аудит",
        )
        if any(marker in normalized for marker in fixed_scope_markers):
            return False
        return replica_name is None

    def _task_prompt(
        self, request_path: Path, *, force_execution: bool = False
    ) -> str:
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
        selected_replica = request_payload.get("replica_name") or "не используется"
        input_mode = request_payload.get("input_mode") or "local_files"
        database_access = bool(request_payload.get("database_access"))
        clarification_enabled = self._clarification_allowed(
            str(request_payload.get("auditor_query") or ""),
            request_payload.get("replica_name"),
        ) and not force_execution
        clarification_instruction = (
            """Один вопрос допустим только если ни один источник не удаётся идентифицировать
и любая трактовка сделает расчёты недостоверными. Тогда не запускай расчёты и верни:
AUDIT_CLARIFICATION_REQUIRED={{"question":"один конкретный вопрос","reason":"почему без ответа нельзя"}}"""
            if clarification_enabled
            else """Вопросы пользователю запрещены: область аудита уже задана. Не возвращай
AUDIT_CLARIFICATION_REQUIRED. Не спрашивай о реплике, source_system, выборе подпапки или системы."""
        )
        return f"""Ты выступаешь как аудитор и главный оркестратор Audit Insight Agent.
Это боевой запуск: не изменяй код, правила или конфигурацию.
Машинный контракт области уже проверен приложением:
- input_mode: {input_mode}
- scope_complete: true
- database_access: {str(database_access).lower()}
- replica: {selected_replica}
Этот контракт приоритетнее любых общих знаний или прежних инструкций об обязательной реплике.
Если database_access=false, отсутствие replica_name — это корректный локальный режим, а не ошибка и не блокер.
Область по умолчанию: все поддерживаемые локальные файлы из data/ и все документы knowledge/.
Наличие нескольких подпапок, доменов, source_system или таблиц не является неоднозначностью: анализируй их все.
Реплику БД используй только если в запросе точно назван её зарегистрированный alias. Выбранная реплика: {selected_replica}.
Если указано «не использовать БД» или alias не назван, анализируй только локальные источники и не спрашивай о реплике.
Считай, что пользователь передал минимально достаточный набор для всех возможных проверок. Отсутствующие необязательные источники укажи как ограничение в отчёте, но не останавливай запуск.
{clarification_instruction}

Не выбирай одну систему вместо других. Сразу выполни команду в workspace точно с таким argv:
{json.dumps(command, ensure_ascii=False)}
Команда запустит табличные правила, RAG по документам, профилирование, поиск зависимостей и анализ бизнес-lineage. Она создаст evidence, candidate_findings.json, report.md, profiles.json, relationships.json, selected_rules.json, data_dependencies.json и business_analysis.json.
После запуска изучи report.md, candidate_findings.json, profiles.json, relationships.json, selected_rules.json, data_dependencies.json и business_analysis.json. Для существенных или противоречивых сигналов прочитай также конфигурацию сработавшего правила и релевантный код его исполнения. Это разрешённое read-only исследование, а не изменение кода.
Гипотезы из business_analysis.json и candidate_impact_paths используй для навигации по источникам. Они не доказывают причинность и не могут сами по себе повысить POTENTIAL_RISK до CONFIRMED. При этом finding с тегом material_business_hypothesis запрещено игнорировать: выполни указанный reproduction query, проверь нормативное evidence и явно сообщи его вердикт. Перед завершением проверь metrics.business_hypothesis_coverage: для каждой существенной гипотезы должен существовать finding и review. Если verdict REQUIRES_VALIDATION, укажи недостающее доказательство; если CONFIRMED — включи её в главный вывод и проследи доступные последствия. Не утверждай, что бизнес-гипотезы согласуются с выводом, если они не разобраны по отдельности.
Перед тем как назвать сигнал нарушением, проверь: (1) покрывает ли словарь правила фактические значения; (2) подтверждены ли связи между источниками; (3) не спутана ли техническая воспроизводимость с семантической достоверностью. Никогда не объявляй нарушение CONFIRMED, если rule_applicability имеет статус PARTIAL или INCOMPATIBLE.
Весь ответ напиши на русском языке по принципу пирамиды Барбары Минто и уложи в 800 слов. Первая строка ответа — `## Главный вывод`; до неё запрещены Evidence critique, описание выполненных шагов и любые технические оговорки. В первом абзаце сразу ответь, что установлено, насколько вывод доказан и что это означает для аудитора. Затем используй разделы `## Ключевые основания`, `## Что это означает для аудита`, `## Рекомендуемые действия` и `## Ограничения и качество доказательств`. Аргументы должны быть непересекающимися, ранжированными по значимости и подтверждать главный вывод сверху вниз. Дай не более четырёх ключевых оснований и пяти действий. Сгруппируй однотипные сигналы: не повторяй десятки одинаковых карточек и не превращай заключение в журнал работы. Ответ обязан быть завершённым: не обрывай предложения и не пропускай обязательные разделы ради перечисления однотипных случаев.
Пиши связным текстом, понятным руководителю без технического контекста: расшифровывай необходимые термины при первом употреблении, не выводи служебные статусы, run_id, имена внутренних инструментов или пути к файлам. Не выдавай POTENTIAL_RISK за найденное нарушение. Для каждого CONFIRMED сообщи, что найдено, где проявилось, какой независимый факт это подтверждает и каков эффект. Ранжированный план действий формулируй как решение: действие, ожидаемый результат и приоритет. Техническую оценку доказательств помести только в последний раздел и изложи человеческим языком.
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
                "run_shell": (
                    "**Выполняю аудиторские расчёты.** Проверяю данные по заданным "
                    "правилам и сохраняю воспроизводимые результаты."
                ),
                "read_file": (
                    "**Изучаю источники.** Сопоставляю структуру данных и содержание "
                    "документов, чтобы привязать выводы к конкретным фактам."
                ),
                "list_files": (
                    "**Определяю область проверки.** Составляю перечень доступных "
                    "таблиц и документов, чтобы не пропустить существенный источник."
                ),
            }
            if tool:
                return labels.get(
                    tool,
                    "**Выполняю следующий шаг анализа.** Агент обрабатывает "
                    "полученные данные и готовит основание для итогового вывода.",
                )
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

    @staticmethod
    def _status_message(status: str) -> str:
        messages = {
            "pending": (
                "**Задача поставлена в очередь.** Агент получил контекст и начнёт "
                "анализ, как только освободятся ресурсы."
            ),
            "running": (
                "**Агент работает над задачей.** Сейчас он собирает и проверяет "
                "доказательства; итоговые выводы ещё не сформированы."
            ),
            "completed": (
                "**Основной анализ завершён.** Агент переходит к сборке итога, "
                "отделяя подтверждённые нарушения от направлений, которые требуют дополнительной проверки."
            ),
        }
        return messages.get(
            status,
            "**Состояние задачи изменилось.** Агент продолжает работу; "
            "новые результаты появятся в этой ленте по мере готовности.",
        )

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
        for name in (
            "discovered_sources",
            "profiles",
            "relationships",
            "selected_rules",
            "data_dependencies",
            "business_analysis",
        ):
            artifact = run_dir / f"{name}.json"
            if artifact.is_file():
                paths[name] = artifact
        return _run_payload(result, paths)

    @staticmethod
    def _answer(result: dict[str, Any]) -> str:
        narrative = str(result.get("ouroboros_answer") or "").strip()
        if narrative:
            return narrative
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
        high_count = severity_counts.get("CRITICAL", 0) + severity_counts.get("HIGH", 0)
        lines = ["## Главный вывод"]
        if confirmed:
            priority = confirmed[0].get("title", "первое по приоритету нарушение")
            if len(confirmed) == 1:
                conclusion = "Аудит подтвердил одно нарушение."
            else:
                finding_count = _counted(
                    len(confirmed), "нарушение", "нарушения", "нарушений"
                )
                conclusion = f"Аудит подтвердил {finding_count}."
            if high_count == 1:
                urgency = (
                    " Оно имеет высокую или критическую значимость."
                    if len(confirmed) == 1
                    else " Одно из них имеет высокую или критическую значимость."
                )
            elif high_count:
                urgency = f" {high_count} из них имеют высокую или критическую значимость."
            else:
                urgency = ""
            lines.append(
                f"{conclusion}{urgency} Первоочередного внимания требует «{priority}»."
            )
        else:
            lines.append(
                "Аудит не выявил нарушений, для которых собраны достаточные "
                "доказательства. Это снижает текущую оценку риска, но не доказывает полное отсутствие нарушений."
            )
        if confirmed:
            lines.append("## Обоснование")
            for index, finding in enumerate(confirmed[:5], 1):
                title = finding.get("title", "Без названия")
                summary = str(finding.get("summary") or "").strip()
                risk = str(finding.get("risk") or "").strip()
                paragraph = f"{index}. **{title}.** {summary}"
                if risk:
                    paragraph += f" Это создаёт риск: {risk}"
                lines.append(paragraph)
        potential_count = sum(
            item.get("status") == "POTENTIAL_RISK"
            for item in result.get("audit_plan", [])
        )
        plan = result.get("audit_plan", [])
        if plan:
            lines.append("## Что делать дальше")
            for index, item in enumerate(plan[:5], 1):
                title = item.get("title", "Дополнительная проверка")
                next_steps = [str(step) for step in item.get("next_steps", []) if step]
                action = next_steps[0] if next_steps else item.get("rationale", "")
                lines.append(f"{index}. **{title}.** {action}".strip())
        if potential_count:
            risk_count = _counted(
                potential_count, "потенциальный риск", "потенциальных риска", "потенциальных рисков"
            )
            lines.append(
                f"План содержит {risk_count}. "
                "Они не считаются нарушениями, пока дополнительная проверка не даст "
                "достаточных доказательств."
            )
        errors = result.get("execution_errors", [])
        if errors:
            failed_checks = _counted(
                len(errors), "проверку", "проверки", "проверок"
            )
            lines.extend([
                "## Ограничения",
                f"Во время анализа не удалось выполнить {failed_checks}. Поэтому итог нужно трактовать с учётом неполного покрытия; подробности сохранены в отчёте."
            ])
        return "\n\n".join(lines)
