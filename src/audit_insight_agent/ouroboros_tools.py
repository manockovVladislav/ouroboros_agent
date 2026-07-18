from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .agent import AuditInsightAgent
from .audit_rag import ground_audit_with_documents
from .case_package import load_case_package
from .config import load_application_settings, resolve_source_location
from .data_loader import DuckDBTableStore
from .data_profiler import profile_table
from .evidence_store import EvidenceStore
from .finding_builder import deduplicate_findings
from .models import AgentRunResult, CandidateFinding
from .report_generator import render_markdown_report
from .retriever import BgeM3Embedder, QdrantRetriever, create_qdrant_client


"""Ограниченный публичный шлюз между Ouroboros и аудиторским ядром."""

PROJECT_ROOT = (
    Path(__file__)
    .resolve()
    .parents[2]
)

DEFAULT_DATA_ROOT = (
    PROJECT_ROOT
    / "data"
)

DEFAULT_OUTPUT_ROOT = (
    PROJECT_ROOT
    / "outputs"
    / "runs"
)

DEFAULT_CASES_ROOT = PROJECT_ROOT / "cases"


def _case_dir(case_name: str) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", case_name):
        raise ValueError("Некорректное имя case-пакета")
    case_dir = _resolve_inside(DEFAULT_CASES_ROOT / case_name, DEFAULT_CASES_ROOT)
    if not case_dir.is_dir():
        raise FileNotFoundError(f"Case-пакет не найден: {case_name}")
    return case_dir


def _safe_source_overrides(
    source_overrides: dict[str, str] | None,
) -> dict[str, Path]:
    return {
        source_id: _resolve_inside(path, _allowed_data_root())
        for source_id, path in (source_overrides or {}).items()
    }


def _allowed_data_root() -> Path:

    return Path(
        os.getenv(
            "AUDIT_AGENT_ALLOWED_DATA_ROOT",
            str(DEFAULT_DATA_ROOT),
        )
    ).expanduser().resolve()


def _output_root() -> Path:

    return Path(
        os.getenv(
            "AUDIT_AGENT_OUTPUT_ROOT",
            str(DEFAULT_OUTPUT_ROOT),
        )
    ).expanduser().resolve()


def _resolve_inside(
    candidate: str | Path,
    allowed_root: Path,
) -> Path:

    resolved = Path(
        candidate
    ).expanduser().resolve()

    if (
        resolved != allowed_root
        and allowed_root
        not in resolved.parents
    ):
        raise PermissionError(
            f"Путь находится вне разрешенного "
            f"каталога: {resolved}"
        )

    return resolved


def _validate_run_id(
    run_id: str | None,
) -> str | None:

    if run_id is None:
        return None

    if not re.fullmatch(
        r"[A-Za-z0-9_.-]{1,80}",
        run_id,
    ):
        raise ValueError(
            "Некорректный run_id"
        )

    return run_id


def run_audit(
    data_dir: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """
    Запускает полный анализ публичных данных.

    Доступен Ouroboros.
    """

    allowed_root = (
        _allowed_data_root()
    )

    resolved_data_dir = (
        _resolve_inside(
            candidate=data_dir,
            allowed_root=allowed_root,
        )
    )

    validated_run_id = (
        _validate_run_id(
            run_id
        )
    )

    agent = AuditInsightAgent()

    result, paths = agent.run(
        data_dir=resolved_data_dir,
        output_root=_output_root(),
        run_id=validated_run_id,
    )
    if result.case_name:
        package = load_case_package(_case_dir(result.case_name))
        settings_file = Path(
            os.getenv("AUDIT_INSIGHT_CONFIG", str(PROJECT_ROOT / "configs/config.yaml"))
        ).expanduser().resolve()
        result, paths = ground_audit_with_documents(
            result=result,
            paths=paths,
            package=package,
            settings=load_application_settings(settings_file),
            project_root=PROJECT_ROOT,
            auditor_query="Выполнить полный аудит всех доступных данных",
        )

    return {
        "run_id": result.run_id,
        "status": result.status.value,
        "findings_count": len(
            result.findings
        ),
        "execution_errors_count": len(
            result.execution_errors
        ),
        "candidate_findings_path": str(
            paths[
                "candidate_findings"
            ]
        ),
        "report_path": str(
            paths["report"]
        ),
        "run_manifest_path": str(
            paths["run_manifest"]
        ),
        "rag_context_path": str(paths["rag_context"]) if "rag_context" in paths else None,
    }


def run_case_audit(
    case_name: str,
    auditor_query: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Run an allowlisted declarative case package for an auditor request."""

    return run_full_audit(case_name, auditor_query, run_id=run_id)


def list_data_sources(case_name: str) -> list[dict[str, Any]]:
    """List configured inputs without exposing file contents."""

    package = load_case_package(_case_dir(case_name))
    config_path = package.root / "data_sources.yaml"
    result = []
    for source in package.sources.sources:
        path = resolve_source_location(source, config_path)
        result.append(
            {
                "source_id": source.source_id,
                "source_type": source.source_type,
                "format": source.format,
                "enabled": source.enabled,
                "exists": path.is_file(),
                "size_bytes": path.stat().st_size if path.is_file() else None,
                "expected_fields": source.expected_fields,
                "metadata": source.metadata,
            }
        )
    return result


def profile_data_source(case_name: str, source_id: str) -> dict[str, Any]:
    """Profile one configured table through DuckDB."""

    package = load_case_package(_case_dir(case_name))
    source = next(
        (item for item in package.sources.sources if item.source_id == source_id),
        None,
    )
    if source is None:
        raise ValueError(f"Неизвестный source_id: {source_id}")
    if source.source_type != "table":
        raise ValueError(f"Источник не является таблицей: {source_id}")
    config_path = package.root / "data_sources.yaml"
    with DuckDBTableStore() as store:
        store.ingest(source, config_path)
        return profile_table(store, source).model_dump(mode="json")


def search_documents(
    query: str,
    limit: int = 5,
    settings_path: str | None = None,
) -> list[dict[str, Any]]:
    """Search the existing Qdrant knowledge index using configured BGE-M3."""

    if not query.strip() or len(query) > 4000:
        raise ValueError("Поисковый запрос должен содержать от 1 до 4000 символов")
    settings_file = Path(
        settings_path
        or os.getenv("AUDIT_INSIGHT_CONFIG", str(PROJECT_ROOT / "configs/config.yaml"))
    ).expanduser().resolve()
    settings = load_application_settings(settings_file)
    client = create_qdrant_client(
        url=settings.qdrant.url,
        api_key=os.getenv(settings.qdrant.api_key_env),
        path=None if settings.qdrant.url else settings.qdrant.path,
    )
    embedder = BgeM3Embedder(
        settings.embedding.model,
        device=settings.embedding.device,
        batch_size=settings.embedding.batch_size,
    )
    retriever = QdrantRetriever(client, embedder, settings.qdrant.collection)
    return [item.model_dump(mode="json") for item in retriever.search(query, limit)]


def run_rule(
    case_name: str,
    rule_id: str,
    auditor_query: str = "Запуск выбранного правила",
    run_id: str | None = None,
    source_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    return _run_selected_rules(
        case_name,
        {rule_id},
        auditor_query,
        run_id,
        source_overrides,
    )


def run_rule_group(
    case_name: str,
    tag: str,
    auditor_query: str,
    run_id: str | None = None,
    source_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    package = load_case_package(_case_dir(case_name))
    rule_ids = {rule.rule_id for rule in package.rules if tag in rule.tags and rule.enabled}
    if not rule_ids:
        raise ValueError(f"Группа правил не найдена: {tag}")
    return _run_selected_rules(
        case_name,
        rule_ids,
        auditor_query,
        run_id,
        source_overrides,
    )


def _run_selected_rules(
    case_name: str,
    rule_ids: set[str],
    auditor_query: str,
    run_id: str | None,
    source_overrides: dict[str, str] | None,
) -> dict[str, Any]:
    result, paths = AuditInsightAgent(agent_version="0.3.0").run_case(
        case_dir=_case_dir(case_name),
        auditor_query=auditor_query,
        output_root=_output_root(),
        run_id=_validate_run_id(run_id),
        source_overrides=_safe_source_overrides(source_overrides),
        selected_rule_ids=rule_ids,
    )
    return _run_payload(result, paths)


def build_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and deduplicate candidate findings without file access."""

    validated = [CandidateFinding.model_validate(item) for item in findings]
    return [item.model_dump(mode="json") for item in deduplicate_findings(validated)]


def generate_report(run_id: str) -> dict[str, Any]:
    """Regenerate report.md strictly from a validated run result."""

    validated_run_id = _validate_run_id(run_id)
    if validated_run_id is None:
        raise ValueError("run_id обязателен")
    run_dir = _resolve_inside(_output_root() / validated_run_id, _output_root())
    candidate_path = run_dir / "candidate_findings.json"
    result = AgentRunResult.model_validate_json(candidate_path.read_text(encoding="utf-8"))
    report_path = run_dir / "report.md"
    temporary = report_path.with_suffix(".md.tmp")
    temporary.write_text(render_markdown_report(result), encoding="utf-8")
    temporary.replace(report_path)
    return {"run_id": validated_run_id, "report_path": str(report_path)}


def run_full_audit(
    case_name: str,
    auditor_query: str,
    run_id: str | None = None,
    source_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Primary API: auditor request to findings, Evidence and report."""

    if not auditor_query.strip() or len(auditor_query) > 4000:
        raise ValueError("Запрос аудитора должен содержать от 1 до 4000 символов")
    case_dir = _case_dir(case_name)
    package = load_case_package(case_dir)
    result, paths = AuditInsightAgent(agent_version="0.3.0").run_case(
        case_dir=case_dir,
        auditor_query=auditor_query,
        output_root=_output_root(),
        run_id=_validate_run_id(run_id),
        source_overrides=_safe_source_overrides(source_overrides),
    )
    settings_file = Path(
        os.getenv("AUDIT_INSIGHT_CONFIG", str(PROJECT_ROOT / "configs/config.yaml"))
    ).expanduser().resolve()
    settings = load_application_settings(settings_file)
    result, paths = ground_audit_with_documents(
        result=result,
        paths=paths,
        package=package,
        settings=settings,
        project_root=PROJECT_ROOT,
        auditor_query=auditor_query,
    )
    return _run_payload(result, paths)


def _run_payload(result: AgentRunResult, paths: dict[str, Path]) -> dict[str, Any]:
    payload = {
        "run_id": result.run_id,
        "status": result.status.value,
        "case_name": result.case_name,
        "findings_count": len(result.findings),
        "findings": [item.model_dump(mode="json") for item in result.findings],
        "rule_results": [item.model_dump(mode="json") for item in result.rule_results],
        "execution_errors": result.execution_errors,
        "candidate_findings_path": str(paths["candidate_findings"]),
        "report_path": str(paths["report"]),
        "run_manifest_path": str(paths["run_manifest"]),
    }
    if "rag_context" in paths:
        payload["rag_context_path"] = str(paths["rag_context"])
    return payload


def get_evidence(run_id: str, evidence_id: str) -> dict[str, Any]:
    """Return a checksum-verified evidence record through the public gateway."""

    validated_run_id = _validate_run_id(run_id)
    if validated_run_id is None:
        raise ValueError("run_id обязателен")
    evidence_root = _resolve_inside(
        _output_root() / validated_run_id / "evidence",
        _output_root(),
    )
    return EvidenceStore(evidence_root).get(evidence_id).model_dump(mode="json")


def get_run_summary(
    run_id: str,
) -> dict[str, Any]:
    """
    Возвращает только безопасную сводку запуска.
    """

    validated_run_id = (
        _validate_run_id(
            run_id
        )
    )

    if validated_run_id is None:
        raise ValueError(
            "run_id обязателен"
        )

    manifest_path = (
        _output_root()
        / validated_run_id
        / "run_manifest.json"
    )

    resolved_manifest = (
        _resolve_inside(
            candidate=manifest_path,
            allowed_root=_output_root(),
        )
    )

    if not resolved_manifest.exists():
        raise FileNotFoundError(
            f"Запуск не найден: {run_id}"
        )

    return json.loads(
        resolved_manifest.read_text(
            encoding="utf-8",
        )
    )
