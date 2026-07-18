"""Restricted public gateway between Ouroboros and the universal audit core."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from .agent import AuditInsightAgent
from .audit_rag import ground_audit_with_documents
from .config import load_application_settings, resolve_source_location
from .data_loader import DuckDBTableStore, is_database_source
from .data_profiler import profile_table
from .evidence_store import EvidenceStore
from .finding_builder import deduplicate_findings
from .models import AgentRunResult, CandidateFinding
from .report_generator import render_markdown_report
from .retriever import BgeM3Embedder, QdrantRetriever, create_qdrant_client
from .run_logging import RunEventLogger
from .workspace import AuditWorkspace, discover_workspace


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "outputs" / "runs"


def _allowed_data_root() -> Path:
    return Path(
        os.getenv("AUDIT_AGENT_ALLOWED_DATA_ROOT", str(DEFAULT_DATA_ROOT))
    ).expanduser().resolve()


def _output_root() -> Path:
    return Path(
        os.getenv("AUDIT_AGENT_OUTPUT_ROOT", str(DEFAULT_OUTPUT_ROOT))
    ).expanduser().resolve()


def _resolve_inside(candidate: str | Path, allowed_root: Path) -> Path:
    resolved = Path(candidate).expanduser().resolve()
    if resolved != allowed_root and allowed_root not in resolved.parents:
        raise PermissionError(f"Путь находится вне разрешенного каталога: {resolved}")
    return resolved


def _validate_run_id(run_id: str | None) -> str | None:
    if run_id is None:
        return None
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id):
        raise ValueError("Некорректный run_id")
    return run_id


def _application_settings():
    settings_file = Path(
        os.getenv("AUDIT_INSIGHT_CONFIG", str(PROJECT_ROOT / "configs/config.yaml"))
    ).expanduser().resolve()
    return load_application_settings(settings_file)


def _workspace(data_root: str | Path | None = None) -> AuditWorkspace:
    data = _resolve_inside(data_root or _allowed_data_root(), _allowed_data_root())
    return discover_workspace(PROJECT_ROOT, data_root=data)


def _validate_replica_name(settings, replica_name: str | None) -> str | None:
    if replica_name is None:
        return None
    if replica_name not in settings.databases.connections:
        raise ValueError(f"Unknown database replica: {replica_name}")
    return replica_name


def list_data_sources() -> list[dict[str, Any]]:
    """Discover current data/, knowledge/ and configured SQL sources."""

    workspace = _workspace()
    result = []
    for source in workspace.sources.sources:
        if is_database_source(source):
            result.append(
                {
                    "source_id": source.source_id,
                    "source_type": source.source_type,
                    "format": source.format,
                    "enabled": source.enabled,
                    "exists": None,
                    "size_bytes": None,
                    "connection": source.connection,
                    "relation": source.location,
                    "expected_fields": source.expected_fields,
                    "metadata": source.metadata,
                }
            )
            continue
        path = resolve_source_location(source, workspace.source_config_path)
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


def profile_data_source(
    source_id: str, replica_name: str | None = None
) -> dict[str, Any]:
    """Profile one automatically discovered table through DuckDB."""

    workspace = _workspace()
    source = next(
        (item for item in workspace.sources.sources if item.source_id == source_id), None
    )
    if source is None:
        raise ValueError(f"Неизвестный source_id: {source_id}")
    if source.source_type != "table":
        raise ValueError(f"Источник не является таблицей: {source_id}")
    settings = _application_settings()
    selected_replica = _validate_replica_name(settings, replica_name)
    with DuckDBTableStore(
        database_settings=settings.databases,
        selected_replica=selected_replica,
    ) as store:
        store.ingest(source, workspace.source_config_path)
        return profile_table(store, source).model_dump(mode="json")


def search_documents(
    query: str,
    limit: int = 5,
    settings_path: str | None = None,
) -> list[dict[str, Any]]:
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


def _execute(
    auditor_query: str,
    *,
    run_id: str | None = None,
    rule_ids: set[str] | None = None,
    replica_name: str | None = None,
    data_root: str | Path | None = None,
    include_rag: bool = True,
) -> tuple[AgentRunResult, dict[str, Path]]:
    if not auditor_query.strip() or len(auditor_query) > 4000:
        raise ValueError("Запрос аудитора должен содержать от 1 до 4000 символов")
    workspace = _workspace(data_root)
    settings = _application_settings()
    selected_replica = _validate_replica_name(settings, replica_name)
    result, paths = AuditInsightAgent(agent_version="0.4.0").run_workspace(
        workspace=workspace,
        auditor_query=auditor_query,
        output_root=_output_root(),
        run_id=_validate_run_id(run_id),
        selected_rule_ids=rule_ids,
        settings=settings,
        selected_replica=selected_replica,
    )
    if include_rag:
        try:
            result, paths = ground_audit_with_documents(
                result=result,
                paths=paths,
                package=workspace,
                settings=settings,
                project_root=PROJECT_ROOT,
                auditor_query=auditor_query,
            )
        except Exception as error:
            RunEventLogger(Path(paths["candidate_findings"]).parent, result.run_id).exception(
                "audit_failed", error, phase="rag"
            )
            raise
    return result, paths


def run_audit(data_dir: str, run_id: str | None = None) -> dict[str, Any]:
    result, paths = _execute(
        "Выполнить полный аудит всех доступных данных",
        run_id=run_id,
        data_root=data_dir,
    )
    return _run_payload(result, paths)


def run_rule(
    rule_id: str,
    auditor_query: str = "Запуск выбранного правила",
    run_id: str | None = None,
) -> dict[str, Any]:
    result, paths = _execute(
        auditor_query, run_id=run_id, rule_ids={rule_id}, include_rag=False
    )
    return _run_payload(result, paths)


def run_rule_group(
    tag: str,
    auditor_query: str,
    run_id: str | None = None,
) -> dict[str, Any]:
    workspace = _workspace()
    rule_ids = {
        rule.rule_id for rule in workspace.rules if tag in rule.tags and rule.enabled
    }
    if not rule_ids:
        raise ValueError(f"Группа правил не найдена: {tag}")
    result, paths = _execute(
        auditor_query, run_id=run_id, rule_ids=rule_ids, include_rag=False
    )
    return _run_payload(result, paths)


def run_full_audit(
    auditor_query: str,
    run_id: str | None = None,
    replica_name: str | None = None,
) -> dict[str, Any]:
    """Primary API: discover inputs, profile, execute rules, RAG and report."""

    result, paths = _execute(
        auditor_query, run_id=run_id, replica_name=replica_name
    )
    return _run_payload(result, paths)


def build_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    validated = [CandidateFinding.model_validate(item) for item in findings]
    return [item.model_dump(mode="json") for item in deduplicate_findings(validated)]


def generate_report(run_id: str) -> dict[str, Any]:
    validated_run_id = _validate_run_id(run_id)
    if validated_run_id is None:
        raise ValueError("run_id обязателен")
    run_dir = _resolve_inside(_output_root() / validated_run_id, _output_root())
    result = AgentRunResult.model_validate_json(
        (run_dir / "candidate_findings.json").read_text(encoding="utf-8")
    )
    report_path = run_dir / "report.md"
    temporary = report_path.with_suffix(".md.tmp")
    temporary.write_text(render_markdown_report(result), encoding="utf-8")
    temporary.replace(report_path)
    return {"run_id": validated_run_id, "report_path": str(report_path)}


def _run_payload(result: AgentRunResult, paths: dict[str, Path]) -> dict[str, Any]:
    payload = {
        "run_id": result.run_id,
        "status": result.status.value,
        "findings_count": len(result.findings),
        "findings": [item.model_dump(mode="json") for item in result.findings],
        "finding_reviews": [
            item.model_dump(mode="json") for item in result.finding_reviews
        ],
        "audit_plan": [item.model_dump(mode="json") for item in result.audit_plan],
        "rule_results": [item.model_dump(mode="json") for item in result.rule_results],
        "execution_errors": result.execution_errors,
        "candidate_findings_path": str(paths["candidate_findings"]),
        "report_path": str(paths["report"]),
        "run_manifest_path": str(paths["run_manifest"]),
    }
    for name in ("discovered_sources", "profiles", "relationships", "selected_rules"):
        if name in paths:
            payload[f"{name}_path"] = str(paths[name])
    if "rag_context" in paths:
        payload["rag_context_path"] = str(paths["rag_context"])
    return payload


def get_evidence(run_id: str, evidence_id: str) -> dict[str, Any]:
    validated_run_id = _validate_run_id(run_id)
    if validated_run_id is None:
        raise ValueError("run_id обязателен")
    evidence_root = _resolve_inside(
        _output_root() / validated_run_id / "evidence", _output_root()
    )
    return EvidenceStore(evidence_root).get(evidence_id).model_dump(mode="json")


def get_run_summary(run_id: str) -> dict[str, Any]:
    validated_run_id = _validate_run_id(run_id)
    if validated_run_id is None:
        raise ValueError("run_id обязателен")
    manifest = _resolve_inside(
        _output_root() / validated_run_id / "run_manifest.json", _output_root()
    )
    if not manifest.exists():
        raise FileNotFoundError(f"Запуск не найден: {run_id}")
    return json.loads(manifest.read_text(encoding="utf-8"))
