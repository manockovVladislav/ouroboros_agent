"""Gradio UI for the local Ouroboros → Audit Insight workflow."""
from __future__ import annotations

import inspect
import logging

from .ouroboros import OuroborosOrchestrator
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
    orchestrator = OuroborosOrchestrator()

    def respond(message, history, case_name):
        message = (message or "").strip()
        history = list(history or [])
        if not message:
            yield history, "Введите задачу аудитора.", {}, None, ""
            return
        history.append({"role": "user", "content": message})
        logger.info(
            "Web audit requested case=%s query_length=%s",
            case_name,
            len(message),
        )
        yield history, "Передача задачи в Ouroboros…", {}, None, ""
        try:
            for event in orchestrator.run_with_updates(message, case_name):
                if event["kind"] == "status":
                    yield history, event["message"], {}, None, ""
                    continue
                result = event["result"]
                history.append({"role": "assistant", "content": result["answer"]})
                visible_findings = result["findings"][:100]
                status = (
                    f"Статус: **{result['status']}** · "
                    f"findings: **{result['findings_count']}** · "
                    f"run_id: `{result['run_id']}`"
                )
                yield (
                    history,
                    status,
                    visible_findings,
                    result["report_path"],
                    result["run_id"],
                )
        except Exception as error:
            logger.exception("Web audit failed case=%s", case_name)
            history.append(
                {
                    "role": "assistant",
                    "content": f"Запуск завершился ошибкой: {type(error).__name__}: {error}",
                }
            )
            yield history, "Статус: **ERROR**", {}, None, ""

    with gr.Blocks(title="Audit Insight Agent") as interface:
        gr.Markdown("# Audit Insight Agent\nЛокальный чат: Ouroboros → аудит → findings → отчёт")
        with gr.Row():
            case_name = gr.Dropdown(
                choices=cases,
                value=cases[0] if cases else None,
                label="Сценарий правил",
            )
            run_id = gr.Textbox(label="run_id", interactive=False)
        chatbot_options = {"label": "Диалог", "height": 420}
        if "type" in inspect.signature(gr.Chatbot).parameters:
            chatbot_options["type"] = "messages"
        chatbot = gr.Chatbot(**chatbot_options)
        message = gr.Textbox(
            label="Задача аудитора",
            placeholder="Например: проверить лимиты ОВП и сверку физической валюты",
            lines=3,
        )
        submit = gr.Button("Запустить анализ", variant="primary")
        status = gr.Markdown("Статус: ожидание запроса")
        findings = gr.JSON(label="Аудиторские выводы (не более 100 в интерфейсе)")
        report = gr.File(label="Отчёт report.md", interactive=False)

        submit.click(
            respond,
            inputs=[message, chatbot, case_name],
            outputs=[chatbot, status, findings, report, run_id],
        ).then(lambda: "", outputs=message)
        message.submit(
            respond,
            inputs=[message, chatbot, case_name],
            outputs=[chatbot, status, findings, report, run_id],
        ).then(lambda: "", outputs=message)
    return interface
