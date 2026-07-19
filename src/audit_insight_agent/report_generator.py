from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .models import AgentRunResult

"""Формирование отчётов из проверенных наблюдений и доказательств.

Первая версия должна применять шаблоны, сохранять ссылки между выводами и
Evidence и поддерживать минимум один человекочитаемый формат. Генератор не
должен самостоятельно выполнять проверки или изменять их результаты.
"""


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


def _priority_label(value: str) -> str:
    return {
        "CRITICAL": "критический приоритет",
        "HIGH": "высокий приоритет",
        "MEDIUM": "средний приоритет",
        "LOW": "низкий приоритет",
    }.get(value, value.lower())

def _write_text_atomic(
    path: Path,
    content: str,
) -> None:

    path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    temporary_path = (
        path.with_suffix(
            path.suffix + ".tmp"
        )
    )

    temporary_path.write_text(
        content,
        encoding="utf-8",
    )

    temporary_path.replace(
        path
    )


def render_markdown_report(
    result: AgentRunResult,
) -> str:

    confirmed_count = sum(
        item.verdict == "CONFIRMED" for item in result.finding_reviews
    )
    potential_count = sum(
        item.status == "POTENTIAL_RISK" for item in result.audit_plan
    )

    review_by_id = {item.finding_id: item for item in result.finding_reviews}
    confirmed_findings = [
        finding
        for finding in result.findings
        if review_by_id.get(finding.finding_id)
        and review_by_id[finding.finding_id].verdict == "CONFIRMED"
    ]
    validation_count = sum(
        item.verdict == "REQUIRES_VALIDATION" for item in result.finding_reviews
    )
    rejected_count = sum(
        item.verdict == "REJECTED" for item in result.finding_reviews
    )
    severity_rank = {"CRITICAL": 0, "HIGH": 1, "MEDIUM": 2, "LOW": 3}
    confirmed_findings.sort(
        key=lambda item: (severity_rank.get(item.severity.value, 9), -item.confidence)
    )

    lines: list[str] = ["# Аудиторское заключение", "", "## Главный вывод", ""]
    if confirmed_findings:
        priority = confirmed_findings[0]
        lines.append(
            f"Проверка подтвердила "
            f"{_counted(confirmed_count, 'существенное наблюдение', 'существенных наблюдения', 'существенных наблюдений')} "
            f"из {_counted(len(result.findings), 'первоначального сигнала', 'первоначальных сигналов', 'первоначальных сигналов')}. "
            f"Первоочередного внимания требует «{priority.title}»: "
            f"{priority.risk or priority.summary}"
        )
    elif result.findings:
        lines.append(
            f"Проверка выявила {_counted(len(result.findings), 'сигнал', 'сигнала', 'сигналов')}, однако ни один из "
            "них пока не обеспечен достаточными независимыми доказательствами, "
            "чтобы назвать его подтверждённым нарушением. Поэтому результат следует "
            "использовать как основание для адресной проверки, а не как готовое "
            "обвинительное заключение."
        )
    else:
        lines.append(
            "Проверка не выявила подтверждённых нарушений или сигналов, требующих "
            "разбора. Этот результат относится только к фактически выполненным "
            "процедурам и не доказывает отсутствие риска за их пределами."
        )

    lines.extend(["", "## Ключевые основания", ""])
    lines.append(
        f"1. **Качество доказательств.** Подтверждено: {confirmed_count}; "
        f"требует дополнительной проверки: {validation_count}; "
        f"отклонено после критики доказательств: {rejected_count}."
    )
    completed_rules = sum(item.status.value != "ERROR" for item in result.rule_results)
    lines.append(
        f"2. **Охват проверки.** Проанализировано "
        f"{_counted(len(result.data_sources), 'источник', 'источника', 'источников')} "
        f"и выполнено {completed_rules} из {len(result.rule_results)} "
        "запланированных контрольных процедур."
    )
    if confirmed_findings:
        high_count = sum(
            item.severity.value in {"CRITICAL", "HIGH"}
            for item in confirmed_findings
        )
        lines.append(
            f"3. **Значимость результата.** {high_count} подтверждённых "
            "наблюдений имеют высокий или критический приоритет; выводы ниже "
            "ранжированы по значимости, а не по порядку обнаружения."
        )
    else:
        lines.append(
            "3. **Граница вывода.** Наличие воспроизводимого срабатывания правила "
            "подтверждает расчёт, но само по себе не доказывает бизнес-нарушение "
            "или заявленную первопричину."
        )

    if confirmed_findings:
        lines.extend(["", "## Подтверждённые наблюдения", ""])
        for index, finding in enumerate(confirmed_findings, start=1):
            evidence_summary = "; ".join(
                reference.description for reference in finding.evidence[:2]
            )
            lines.extend(
                [
                    f"### {index}. {finding.title}",
                    "",
                    f"**Что установлено.** {finding.summary}",
                    "",
                    f"**Почему это важно.** {finding.risk or 'Риск не указан.'}",
                    "",
                    f"**Чем подтверждается.** {evidence_summary}",
                    "",
                    (
                        f"**Что сделать.** {finding.recommendation}"
                        if finding.recommendation
                        else "**Что сделать.** Установить владельца корректирующего действия и срок устранения."
                    ),
                    "",
                ]
            )

    lines.extend(["", "## Что это означает для аудита", ""])
    if confirmed_findings:
        lines.append(
            "Подтверждённые наблюдения можно включать в аудиторское заключение. "
            "При этом масштаб последствий и первопричину следует формулировать "
            "только в пределах приведённых доказательств."
        )
    elif result.findings:
        lines.append(
            "Формулировать нарушение в итоговом заключении преждевременно. "
            "Сначала необходимо проверить общую причину однотипных сигналов, а "
            "затем подтвердить оставшиеся исключения независимыми источниками."
        )
    else:
        lines.append(
            "Дополнительная эскалация по выполненным процедурам не требуется; "
            "решение о расширении охвата принимается исходя из общего плана аудита."
        )

    lines.extend(["", "## Рекомендуемые действия", ""])
    if result.audit_plan:
        finding_by_id = {item.finding_id: item for item in result.findings}
        grouped_actions: dict[tuple[str, ...], list[Any]] = {}
        for item in result.audit_plan:
            action = item.next_steps[0] if item.next_steps else item.rationale
            finding = finding_by_id.get(item.finding_id or "")
            title = item.title
            if finding and finding.object_id:
                title = title.replace(finding.object_id, "").rstrip(" :-")
            key = (item.priority.value, item.status, title, item.rationale, action)
            grouped_actions.setdefault(key, []).append(item)
        for index, (key, items) in enumerate(grouped_actions.items(), start=1):
            priority, _status, title, _rationale, action = key
            scope = ""
            if len(items) > 1:
                object_ids = [
                    finding_by_id[item.finding_id].object_id
                    for item in items[:3]
                    if item.finding_id in finding_by_id
                    and finding_by_id[item.finding_id].object_id
                ]
                examples = ", ".join(f"`{value}`" for value in object_ids)
                scope = f" Объём: {len(items)} случаев."
                if examples:
                    scope += f" Примеры: {examples}."
            lines.append(
                f"{index}. **{_priority_label(priority).capitalize()}: {title}.** "
                f"{action}{scope} Ожидаемый результат — подтвердить нарушение "
                "либо снять технически обусловленный сигнал."
            )
    else:
        lines.append(
            "Дополнительные действия по результатам выполненных процедур не сформированы."
        )
    lines.append("")

    limitations = []
    for review in result.finding_reviews:
        for limitation in review.limitations:
            if limitation not in limitations:
                limitations.append(limitation)
    lines.extend(["## Ограничения и качество анализа", ""])
    if limitations or result.execution_errors:
        for limitation in limitations:
            lines.append(f"- {limitation}")
        for error in result.execution_errors:
            lines.append(f"- Не выполнена часть расчётов: {error}")
    else:
        lines.append(
            "Существенных технических ограничений в рамках выполненных процедур не зафиксировано."
        )
    lines.extend(
        [
            "",
            "---",
            "",
            "## Техническое приложение",
            "",
            f"- Run ID: `{result.run_id}`",
            f"- Status: `{result.status.value}`",
            f"- Agent version: `{result.agent_version}`",
            f"- Started: `{result.started_at.isoformat()}`",
            f"- Completed: `{result.completed_at.isoformat()}`",
            "",
        ]
    )
    if result.auditor_query:
        lines.extend(["### Запрос аудитора", "", result.auditor_query, ""])

    lines.extend(["## Evidence critique", ""])
    if result.finding_reviews:
        for review in result.finding_reviews:
            lines.append(
                f"- `{review.finding_id}` — **{review.verdict}**: {review.rationale}"
            )
            for limitation in review.limitations:
                lines.append(f"  - Limitation: {limitation}")
    else:
        lines.append(
            "Кандидатов для критики нет: выполненные правила не вернули строк-исключений."
        )
    lines.append("")

    if not confirmed_findings:

        lines.extend([
            "## Result",
            "",
            "Подтверждённые аудиторские выводы не сформированы.",
            "",
            "Это не означает отсутствие нарушений. "
            "Сигналы, не прошедшие критику доказательств, вынесены в план проверки.",
            "",
        ])

    if confirmed_findings:
        lines.extend([
            "## Confirmed findings",
            "",
        ])

    for finding in confirmed_findings:

        lines.extend([
            (
                f"### {finding.finding_id}: "
                f"{finding.title}"
            ),
            "",
            (
                f"- Check: "
                f"`{finding.check_id}`"
            ),
            (
                f"- Issue type: "
                f"`{finding.issue_type}`"
            ),
            (
                f"- Severity: "
                f"`{finding.severity.value}`"
            ),
            (
                f"- Confidence: "
                f"`{finding.confidence:.2f}`"
            ),
        ])

        lines.extend([
            "",
            "#### Summary",
            "",
            finding.summary,
            "",
            "#### Criterion",
            "",
            finding.criterion or "Не указан.",
            "",
            "#### Risk",
            "",
            finding.risk or "Не указан.",
            "",
            "#### Root cause",
            "",
            finding.root_cause,
            "",
            "#### Evidence",
            "",
        ])

        for evidence in finding.evidence:

            object_suffix = ""

            if evidence.object_id:
                object_suffix = (
                    f", object: "
                    f"`{evidence.object_id}`"
                )

            lines.append(
                f"- `{evidence.source_name}`"
                f"{object_suffix}: "
                f"{evidence.description}"
            )
            metadata = evidence.fields.get("metadata") or {}
            location = metadata.get("location") or metadata.get("relative_path")
            if location:
                lines.append(f"  - Location: `{location}`")

        if finding.facts:
            lines.extend([
                "",
                "#### Facts",
                "",
                "```json",
                json.dumps(finding.facts, ensure_ascii=False, indent=2, default=str),
                "```",
            ])

        if finding.recommendation:

            lines.extend([
                "",
                "#### Recommendation",
                "",
                finding.recommendation,
            ])

        lines.extend([
            "",
            "---",
            "",
        ])

    lines.extend(["## Prioritized audit plan", ""])
    if result.audit_plan:
        for item in result.audit_plan:
            lines.extend(
                [
                    f"### {item.plan_id}: {item.title}",
                    "",
                    f"- Priority: `{item.priority.value}`",
                    f"- Status: `{item.status}`",
                    f"- Basis: {item.rationale}",
                ]
            )
            if item.sources:
                lines.append("- Sources: " + ", ".join(f"`{x}`" for x in item.sources))
            for location in item.source_locations:
                lines.append(f"- Document/location: `{location}`")
            if item.next_steps:
                lines.extend(["", "Next steps:", ""])
                lines.extend(
                    f"{index}. {step}"
                    for index, step in enumerate(item.next_steps, start=1)
                )
            lines.extend(["", "---", ""])
    else:
        lines.extend(
            [
                "Доказательно обоснованных дополнительных направлений нет.",
                "Это не доказывает отсутствие риска за пределами выполненных правил.",
                "",
            ]
        )

    return "\n".join(lines)


def write_narrative_report(report_path: str | Path, narrative: str) -> Path:
    """Put the judge's readable conclusion first while retaining audit traceability."""

    path = Path(report_path).expanduser().resolve()
    cleaned = re.sub(
        r"(?m)^AUDIT_(?:RUN_ID|IMPROVEMENT_NEEDED|CLARIFICATION_REQUIRED)=.*$",
        "",
        str(narrative or ""),
    ).strip()
    if not cleaned or re.search(r"(?:\u2026|\.\.\.)?\[truncated\]", cleaned, re.IGNORECASE):
        return path

    existing = path.read_text(encoding="utf-8") if path.is_file() else ""
    marker = "## Техническое приложение"
    appendix = ""
    if marker in existing:
        appendix = existing[existing.index(marker) :].strip()
    elif existing:
        appendix = f"{marker}\n\n{existing.strip()}"

    cleaned = re.sub(r"\A#\s+Аудиторское заключение\s*", "", cleaned).strip()
    content = f"# Аудиторское заключение\n\n{cleaned}"
    if appendix:
        content += f"\n\n---\n\n{appendix}"
    _write_text_atomic(path, content.rstrip() + "\n")
    return path


def write_run_outputs(
    result: AgentRunResult,
    output_dir: str | Path,
) -> dict[str, Path]:

    output_path = Path(
        output_dir
    ).resolve()

    output_path.mkdir(
        parents=True,
        exist_ok=True,
    )

    json_path = (
        output_path
        / "candidate_findings.json"
    )

    report_path = (
        output_path
        / "report.md"
    )

    manifest_path = (
        output_path
        / "run_manifest.json"
    )

    serialized_result = (
        result.model_dump_json(
            indent=2,
        )
    )

    _write_text_atomic(
        json_path,
        serialized_result,
    )

    _write_text_atomic(
        report_path,
        render_markdown_report(
            result
        ),
    )

    manifest: dict[str, Any] = {
        "schema_version": (
            result.schema_version
        ),
        "run_id": result.run_id,
        "status": result.status.value,
        "findings_count": len(
            result.findings
        ),
        "confirmed_findings_count": sum(
            item.verdict == "CONFIRMED" for item in result.finding_reviews
        ),
        "potential_risks_count": sum(
            item.status == "POTENTIAL_RISK" for item in result.audit_plan
        ),
        "audit_plan_items_count": len(result.audit_plan),
        "execution_errors_count": len(
            result.execution_errors
        ),
        "files": {
            "candidate_findings": (
                json_path.name
            ),
            "report": report_path.name,
            "evidence": "evidence/",
        },
    }

    for name, filename in (
        ("events", "events.jsonl"),
        ("rag_context", "rag_context.json"),
        ("chat", "chat.json"),
        ("discovered_sources", "discovered_sources.json"),
        ("relationships", "relationships.json"),
        ("selected_rules", "selected_rules.json"),
        ("profiles", "profiles.json"),
        ("data_dependencies", "data_dependencies.json"),
        ("business_analysis", "business_analysis.json"),
    ):
        if (output_path / filename).is_file():
            manifest["files"][name] = filename

    _write_text_atomic(
        manifest_path,
        json.dumps(
            manifest,
            ensure_ascii=False,
            indent=2,
        ),
    )

    return {
        "candidate_findings": json_path,
        "report": report_path,
        "run_manifest": manifest_path,
    }


def refresh_run_manifest_files(run_dir: str | Path) -> Path:
    """Refresh optional history artifacts after web/Ouroboros completion."""

    root = Path(run_dir).expanduser().resolve()
    manifest_path = root / "run_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    files = manifest.setdefault("files", {})
    for name, filename in (
        ("events", "events.jsonl"),
        ("rag_context", "rag_context.json"),
        ("chat", "chat.json"),
        ("discovered_sources", "discovered_sources.json"),
        ("relationships", "relationships.json"),
        ("selected_rules", "selected_rules.json"),
        ("profiles", "profiles.json"),
        ("data_dependencies", "data_dependencies.json"),
        ("business_analysis", "business_analysis.json"),
    ):
        if (root / filename).is_file():
            files[name] = filename
    _write_text_atomic(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return manifest_path
