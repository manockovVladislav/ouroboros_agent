from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AgentRunResult

"""Формирование отчётов из проверенных наблюдений и доказательств.

Первая версия должна применять шаблоны, сохранять ссылки между выводами и
Evidence и поддерживать минимум один человекочитаемый формат. Генератор не
должен самостоятельно выполнять проверки или изменять их результаты.
"""

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

    lines: list[str] = [
        "# Audit Insight Agent Report",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Status: `{result.status.value}`",
        f"- Agent version: `{result.agent_version}`",
        f"- Started: `{result.started_at.isoformat()}`",
        f"- Completed: `{result.completed_at.isoformat()}`",
        f"- Data sources: `{len(result.data_sources)}`",
        f"- Candidate findings: `{len(result.findings)}`",
        f"- Confirmed findings: `{confirmed_count}`",
        f"- Potential risks: `{potential_count}`",
        "",
    ]

    if result.case_name:
        lines.insert(3, f"- Case: `{result.case_name}`")
    if result.auditor_query:
        lines.extend(["## Auditor request", "", result.auditor_query, ""])

    if result.execution_errors:

        lines.extend([
            "## Execution warnings",
            "",
        ])

        for error in result.execution_errors:
            lines.append(
                f"- {error}"
            )

        lines.append("")

    review_by_id = {item.finding_id: item for item in result.finding_reviews}
    confirmed_findings = [
        finding
        for finding in result.findings
        if review_by_id.get(finding.finding_id)
        and review_by_id[finding.finding_id].verdict == "CONFIRMED"
    ]

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
    ):
        if (root / filename).is_file():
            files[name] = filename
    _write_text_atomic(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return manifest_path
