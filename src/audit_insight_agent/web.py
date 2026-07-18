"""Gradio UI for the local Ouroboros → Audit Insight workflow."""
from __future__ import annotations

import inspect
import logging

from .agent_system import AuditAgentSystem
from .ouroboros_tools import DEFAULT_CASES_ROOT


logger = logging.getLogger("audit_insight.web")


def available_cases() -> list[str]:
    if not DEFAULT_CASES_ROOT.exists():
        return []
    return sorted(
        path.name
        for path in DEFAULT_CASES_ROOT.iterdir()
        if path.is_dir() and (path / "data_sources.yaml").is_file()
    )


def build_interface():
    try:
        import gradio as gr
    except ImportError as error:
        raise RuntimeError("Web UI requires the 'gradio' package") from error

    cases = available_cases()
    orchestrator = AuditAgentSystem()

    def respond(message, history, case_name, pending_request):
        message = (message or "").strip()
        history = list(history or [])
        pending_request = dict(pending_request or {})
        if not message:
            yield history, "Введите задачу аудитора.", {}, None, "", pending_request
            return
        history.append({"role": "user", "content": message})
        if pending_request:
            original_request = str(pending_request.get("user_request") or "")
            effective_case = str(pending_request.get("case_name") or case_name)
            effective_request = (
                f"{original_request}\n\nОтвет на блокирующий вопрос: {message}"
            )
        else:
            original_request = message
            effective_request = message
            effective_case = case_name
        logger.info(
            "Web audit requested case=%s query_length=%s",
            effective_case,
            len(effective_request),
        )
        yield history, "Передача задачи в Ouroboros…", {}, None, "", {}
        try:
            for event in orchestrator.run_with_updates(effective_request, effective_case):
                if event["kind"] == "status":
                    yield history, event["message"], {}, None, "", {}
                    continue
                if event["kind"] == "clarification":
                    history.append(
                        {"role": "assistant", "content": event["question"]}
                    )
                    pending = {
                        "user_request": original_request,
                        "case_name": effective_case,
                    }
                    yield (
                        history,
                        "Нужно одно уточнение до начала расчётов.",
                        {},
                        None,
                        "",
                        pending,
                    )
                    return
                result = event["result"]
                answer = result["answer"]
                improvement = result.get("self_improvement") or {}
                if improvement.get("status") == "PATCH_READY":
                    answer += (
                        "\n\nOuroboros подготовил изолированное улучшение: "
                        f"{len(improvement.get('changed_paths') or [])} файлов. "
                        "Изменения не влиты в рабочую ветку."
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
                    f"Статус: **{result['status']}** · "
                    f"подтверждено: **{len(confirmed_findings)}** · "
                    f"run_id: `{result['run_id']}` · "
                    f"self-improvement: **{improvement.get('status', 'DISABLED')}**"
                )
                yield (
                    history,
                    status,
                    visible_findings,
                    result["report_path"],
                    result["run_id"],
                    {},
                )
        except Exception as error:
            logger.exception("Web audit failed case=%s", effective_case)
            history.append(
                {
                    "role": "assistant",
                    "content": f"Запуск завершился ошибкой: {type(error).__name__}: {error}",
                }
            )
            yield history, "Статус: **ERROR**", {}, None, "", {}

    with gr.Blocks(title="Audit Insight Agent") as interface:
        gr.Markdown(
            "# Audit Insight Agent\n"
            "Ouroboros управляет анализом, а при обнаружении системного пробела "
            "готовит изолированное улучшение без merge."
        )
        pending_request = gr.State({})
        with gr.Row():
            case_name = gr.Dropdown(
                choices=cases,
                value=cases[0] if cases else None,
                label="Набор правил",
            )
            run_id = gr.Textbox(label="run_id", interactive=False)
        chatbot_options = {"label": "Диалог", "height": 440}
        if "type" in inspect.signature(gr.Chatbot).parameters:
            chatbot_options["type"] = "messages"
        chatbot = gr.Chatbot(**chatbot_options)
        message = gr.Textbox(
            label="Задача аудитора",
            placeholder="Проанализируй данны в data/ и документы в knowledge/",
            lines=3,
        )
        submit = gr.Button("Запустить агента", variant="primary")
        status = gr.Markdown("Статус: ожидание запроса")
        findings = gr.JSON(
            label="Подтверждённые выводы и ранжированный план"
        )
        report = gr.File(label="Отчёт report.md", interactive=False)

        event_inputs = [message, chatbot, case_name, pending_request]
        event_outputs = [
            chatbot,
            status,
            findings,
            report,
            run_id,
            pending_request,
        ]
        submit.click(respond, inputs=event_inputs, outputs=event_outputs).then(
            lambda: "", outputs=message
        )
        message.submit(respond, inputs=event_inputs, outputs=event_outputs).then(
            lambda: "", outputs=message
        )
    return interface
