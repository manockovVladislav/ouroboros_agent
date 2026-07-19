"""Gradio UI for the local Ouroboros → Audit Insight workflow."""
from __future__ import annotations

import inspect
import logging

from .agent_system import AuditAgentSystem


logger = logging.getLogger("audit_insight.web")


def _activity_log(messages: list[str]) -> str:
    """Render a readable, cumulative view of the latest agent actions."""

    visible = messages[-12:]
    steps = [f"{index}. {message}" for index, message in enumerate(visible, 1)]
    return "## Ход работы\n\n" + "\n\n".join(steps)


def _file_count(value: int) -> str:
    remainder = value % 100
    if 11 <= remainder <= 14:
        form = "файлов"
    elif value % 10 == 1:
        form = "файл"
    elif 2 <= value % 10 <= 4:
        form = "файла"
    else:
        form = "файлов"
    return f"{value} {form}"


def build_interface():
    try:
        import gradio as gr
    except ImportError as error:
        raise RuntimeError("Web UI requires the 'gradio' package") from error

    audit_system = AuditAgentSystem()

    def _complete_result(result, history, activity):
        answer = result["answer"]
        improvement = result.get("self_improvement") or {}
        if improvement.get("status") == "PATCH_READY":
            changed_paths = improvement.get("changed_paths") or []
            answer += (
                "\n\n## Улучшение агента\n\n"
                "После аудита система нашла обоснованную возможность "
                f"улучшения и подготовила изменения, затрагивающие {_file_count(len(changed_paths))}. "
                "Они прошли тесты и остались изолированными, "
                "поэтому не влияют на рабочую версию до ручного решения."
            )
        elif improvement.get("status") == "NO_CHANGES":
            answer += (
                "\n\n## Качество анализа\n\n"
                "После завершения аудита система повторно проверила логику "
                "анализа и не нашла обоснованных изменений."
            )
        elif improvement.get("status") == "TESTS_FAILED":
            answer += (
                "\n\n## Качество анализа\n\n"
                "Система нашла возможность улучшения, но подготовленные изменения "
                "не прошли проверку качества. Они отклонены и не влияют на рабочую версию."
            )
        history.append({"role": "assistant", "content": answer})
        confirmed_ids = {
            item.get("finding_id")
            for item in result.get("finding_reviews", [])
            if item.get("verdict") == "CONFIRMED"
        }
        confirmed_findings = [
            item
            for item in result["findings"]
            if item.get("finding_id") in confirmed_ids
        ][:100]
        visible_findings = {
            "confirmed_findings": confirmed_findings,
            "prioritized_audit_plan": result.get("audit_plan", []),
        }
        status = (
            _activity_log(activity)
            + "\n\n**Работа завершена.** "
            + f"Количество подтверждённых выводов — {len(confirmed_findings)}. "
            + "Агент также сформировал план дальнейшей проверки и сохранил отчёт."
        )
        return (
            history,
            status,
            visible_findings,
            result["report_path"],
            result["run_id"],
            {},
            "",
            {},
        )

    def respond(message, history, pending_request, developer_mode, active_request):
        message = (message or "").strip()
        history = list(history or [])
        pending_request = dict(pending_request or {})
        active_request = dict(active_request or {})
        if not message:
            yield (
                history,
                "Введите задачу аудитора.",
                gr.skip(),
                gr.skip(),
                gr.skip(),
                pending_request,
                "",
                active_request,
            )
            return
        history.append({"role": "user", "content": message})
        if pending_request:
            original_request = str(pending_request.get("user_request") or "")
            effective_request = (
                f"{original_request}\n\nОтвет на блокирующий вопрос: {message}"
            )
        else:
            original_request = message
            effective_request = message
        logger.info(
            "Web audit requested query_length=%s developer_mode=%s",
            len(effective_request),
            bool(developer_mode),
        )
        runner = audit_system if developer_mode else audit_system.audit
        activity = [
            "**Задача принята.** Формирую область аудита и передаю её "
            "агенту для проверки источников, расчётов и доказательств."
        ]
        if developer_mode:
            activity.append(
                "**Включён полный режим.** После аудиторского заключения агент "
                "проверит собственную логику и при необходимости подготовит "
                "изолированное улучшение."
            )
        else:
            activity.append(
                "**Включён быстрый режим.** Результат будет возвращён сразу после "
                "аудиторского анализа, без ожидания самоулучшения."
            )
        yield (
            history,
            _activity_log(activity),
            gr.skip(),
            gr.skip(),
            gr.skip(),
            {},
            "",
            active_request,
        )
        try:
            for event in runner.run_with_updates(effective_request):
                if event.get("request_id"):
                    active_request = {
                        "request_id": str(event["request_id"]),
                        "user_request": original_request,
                        "developer_mode": bool(developer_mode),
                    }
                if event["kind"] == "status":
                    message_text = str(event["message"]).strip()
                    if message_text and (not activity or message_text != activity[-1]):
                        activity.append(message_text)
                    yield (
                        history,
                        _activity_log(activity),
                        gr.skip(),
                        gr.skip(),
                        gr.skip(),
                        {},
                        "",
                        active_request,
                    )
                    continue
                if event["kind"] == "clarification":
                    history.append(
                        {"role": "assistant", "content": event["question"]}
                    )
                    pending = {
                        "user_request": original_request,
                    }
                    yield (
                        history,
                        _activity_log(
                            activity
                            + [
                                "**Анализ приостановлен.** Без одного уточнения "
                                "расчёты могут привести к недостоверному выводу."
                            ]
                        ),
                        gr.skip(),
                        gr.skip(),
                        gr.skip(),
                        pending,
                        "",
                        {},
                    )
                    return
                yield _complete_result(event["result"], history, activity)
        except Exception as error:
            logger.exception("Web audit failed")
            history.append(
                {
                    "role": "assistant",
                    "content": (
                        "Анализ не удалось завершить, поэтому аудиторские выводы "
                        "не сформированы. Причина: "
                        f"{error}"
                    ),
                }
            )
            yield (
                history,
                _activity_log(
                    activity
                    + [
                        "**Анализ остановлен.** Результат не сформирован; "
                        "подробности причины показаны в диалоге."
                    ]
                ),
                gr.skip(),
                gr.skip(),
                gr.skip(),
                {},
                "",
                active_request,
            )

    def restore(active_request, history, pending_request, developer_mode):
        active_request = dict(active_request or {})
        if not active_request.get("request_id"):
            return
        history = list(history or [])
        user_request = str(active_request.get("user_request") or "").strip()
        if user_request and not history:
            history.append({"role": "user", "content": user_request})
        activity = []
        try:
            for event in audit_system.audit.recover_request_with_updates(
                str(active_request["request_id"])
            ):
                if event["kind"] == "status":
                    message_text = str(event["message"]).strip()
                    if message_text and (not activity or activity[-1] != message_text):
                        activity.append(message_text)
                    yield (
                        history,
                        _activity_log(activity),
                        gr.skip(),
                        gr.skip(),
                        gr.skip(),
                        pending_request or {},
                        "",
                        active_request,
                    )
                    continue
                yield _complete_result(event["result"], history, activity)
        except Exception as error:
            logger.exception("Web audit recovery failed")
            history.append(
                {
                    "role": "assistant",
                    "content": f"Не удалось восстановить активный анализ: {error}",
                }
            )
            yield (
                history,
                _activity_log(["**Восстановление не выполнено.** Подробности показаны в диалоге."]),
                gr.skip(),
                gr.skip(),
                gr.skip(),
                pending_request or {},
                "",
                {},
            )

    with gr.Blocks(title="Audit Insight Agent") as interface:
        gr.HTML(
            "<style>.activity-panel {min-height: 220px; max-height: 420px; "
            "overflow-y: auto;}</style>"
        )
        gr.Markdown(
            "# Audit Insight Agent\n"
            "Выберите быстрый аудиторский анализ или полный цикл с контролируемым "
            "самоулучшением. Результаты обоих режимов сохраняются одинаково."
        )

        def add_workspace(*, developer_mode: bool) -> None:
            if developer_mode:
                gr.Markdown(
                    "Проводит аудит, затем ищет системные пробелы и может "
                    "подготовить проверенное изменение в изолированной ветке. "
                    "Этот режим работает дольше."
                )
            else:
                gr.Markdown(
                    "Отвечает на вопрос, анализирует данные и сразу возвращает "
                    "аудиторское заключение. Самоулучшение не запускается."
                )
            pending_request = gr.State({})
            mode = gr.State(developer_mode)
            if hasattr(gr, "BrowserState"):
                active_request = gr.BrowserState(
                    {},
                    storage_key=(
                        "audit-insight-active-developer"
                        if developer_mode
                        else "audit-insight-active-fast"
                    ),
                )
            else:
                active_request = gr.State({})
            run_id = gr.Textbox(label="run_id", interactive=False)
            chatbot_options = {"label": "Диалог", "height": 440}
            if "type" in inspect.signature(gr.Chatbot).parameters:
                chatbot_options["type"] = "messages"
            chatbot = gr.Chatbot(**chatbot_options)
            message = gr.Textbox(
                label="Задача аудитора",
                placeholder="Проанализируй данные в data/ и документы в knowledge/",
                lines=3,
            )
            submit = gr.Button("Отправить", variant="primary")
            status = gr.Markdown(
                "## Ход работы\n\nАгент ожидает задачу.",
                elem_classes=["activity-panel"],
            )
            findings = gr.JSON(
                label="Подтверждённые выводы и ранжированный план"
            )
            report = gr.File(label="Отчёт report.md", interactive=False)

            event_inputs = [message, chatbot, pending_request, mode, active_request]
            event_outputs = [
                chatbot,
                status,
                findings,
                report,
                run_id,
                pending_request,
                message,
                active_request,
            ]
            submit.click(respond, inputs=event_inputs, outputs=event_outputs)
            message.submit(respond, inputs=event_inputs, outputs=event_outputs)
            interface.load(
                restore,
                inputs=[active_request, chatbot, pending_request, mode],
                outputs=event_outputs,
            )

        with gr.Tabs():
            with gr.Tab("Аудитор — быстрый"):
                add_workspace(developer_mode=False)
            with gr.Tab("Аудитор + разработчик"):
                add_workspace(developer_mode=True)
    return interface
