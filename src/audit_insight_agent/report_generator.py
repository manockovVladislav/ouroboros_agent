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

    lines: list[str] = [
        "# Audit Insight Agent Report",
        "",
        f"- Run ID: `{result.run_id}`",
        f"- Status: `{result.status.value}`",
        f"- Agent version: `{result.agent_version}`",
        f"- Started: `{result.started_at.isoformat()}`",
        f"- Completed: `{result.completed_at.isoformat()}`",
        f"- Data sources: `{len(result.data_sources)}`",
        f"- Findings: `{len(result.findings)}`",
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

    if not result.findings:

        lines.extend([
            "## Result",
            "",
            "Аудиторские наблюдения не сформированы.",
            "",
            "Это не означает отсутствие нарушений. "
            "Это означает, что действующие проверки "
            "не сформировали подтвержденных выводов.",
            "",
        ])

        return "\n".join(lines)

    lines.extend([
        "## Findings",
        "",
    ])

    for finding in result.findings:

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
