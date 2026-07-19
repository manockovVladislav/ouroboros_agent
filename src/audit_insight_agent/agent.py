"""Universal audit runtime for automatically discovered inputs."""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from pathlib import Path

from .business_analyzer import analyze_business_logic, business_hypothesis_plan_items
from .config import resolve_source_location
from .data_loader import DuckDBTableStore, infer_table_format, is_database_source
from .data_profiler import profile_table
from .dependency_analyzer import analyze_data_dependencies
from .evidence_store import EvidenceStore
from .finding_builder import deduplicate_findings, review_findings_and_build_plan
from .models import (
    AgentRunResult,
    ApplicationSettings,
    AuditRuntimeContext,
    DataSource,
    RuleStatus,
    RunStatus,
    Severity,
)
from .report_generator import refresh_run_manifest_files, write_run_outputs
from .rule_engine import execute_rules
from .run_logging import RunEventLogger
from .workspace import AuditWorkspace, discover_workspace, select_relevant_rules


def create_run_id() -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"RUN-{timestamp}-{secrets.token_hex(3).upper()}"


def _write_json(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)
    return path


class AuditInsightAgent:
    def __init__(self, agent_version: str = "0.4.0") -> None:
        self.agent_version = agent_version

    def run(
        self,
        data_dir: str | Path,
        output_root: str | Path,
        run_id: str | None = None,
        *,
        auditor_query: str = "Выполнить полный аудит всех доступных данных",
        project_root: str | Path | None = None,
        **kwargs,
    ) -> tuple[AgentRunResult, dict[str, Path]]:
        data_root = Path(data_dir).expanduser().resolve()
        project = Path(project_root or data_root.parent).expanduser().resolve()
        workspace = discover_workspace(project, data_root=data_root)
        return self.run_workspace(
            workspace,
            auditor_query=auditor_query,
            output_root=output_root,
            run_id=run_id,
            **kwargs,
        )

    def run_workspace(
        self,
        workspace: AuditWorkspace,
        auditor_query: str,
        output_root: str | Path,
        run_id: str | None = None,
        database: str | Path = ":memory:",
        selected_rule_ids: set[str] | None = None,
        settings: ApplicationSettings | None = None,
        selected_replica: str | None = None,
    ) -> tuple[AgentRunResult, dict[str, Path]]:
        """Profile all discovered tables and execute every applicable selected rule."""

        started_at = datetime.now(timezone.utc)
        actual_run_id = run_id or create_run_id()
        run_output_dir = Path(output_root).expanduser().resolve() / actual_run_id
        event_log = RunEventLogger(run_output_dir, actual_run_id)
        event_log.event(
            "audit_started",
            data_root=str(workspace.data_root),
            knowledge_root=str(workspace.knowledge_root),
            auditor_query_length=len(auditor_query),
        )
        event_log.event(
            "workspace_discovered",
            sources=len(workspace.sources.sources),
            applicable_rules=len(workspace.rules),
            skipped_rules=workspace.skipped_rules,
            relationships=len(workspace.relationships.relationships),
        )

        if selected_rule_ids is None:
            selected_rules = select_relevant_rules(auditor_query, workspace.rules)
        else:
            available = {rule.rule_id: rule for rule in workspace.rules if rule.enabled}
            unknown_rules = selected_rule_ids - set(available)
            if unknown_rules:
                raise ValueError(f"Unknown or inapplicable rule IDs: {sorted(unknown_rules)}")
            selected_rules = tuple(available[item] for item in sorted(selected_rule_ids))
        event_log.event(
            "rules_selected",
            rule_ids=[rule.rule_id for rule in selected_rules],
            count=len(selected_rules),
        )

        table_sources = []
        for source in workspace.sources.sources:
            if not source.enabled or source.source_type != "table":
                continue
            if is_database_source(source):
                alias = selected_replica if source.connection == "$selected" else source.connection
                if selected_replica is None or alias != selected_replica:
                    continue
            table_sources.append(source)

        evidence_store = EvidenceStore(run_output_dir / "evidence")
        source_descriptors: list[DataSource] = []
        profiles = []
        source_errors: list[str] = []
        table_names: dict[str, str] = {}
        with DuckDBTableStore(
            database,
            database_settings=settings.databases if settings else None,
            selected_replica=selected_replica,
        ) as table_store:
            for source in table_sources:
                event_log.event("source_loading", source_id=source.source_id)
                try:
                    table_names[source.source_id] = table_store.ingest(
                        source, workspace.source_config_path
                    )
                    profile = profile_table(table_store, source)
                    profiles.append(profile)
                except Exception as error:
                    message = f"{source.source_id}: {type(error).__name__}: {error}"
                    source_errors.append(message)
                    event_log.exception(
                        "source_loading_failed", error, source_id=source.source_id
                    )
                    continue
                if is_database_source(source):
                    alias = selected_replica if source.connection == "$selected" else source.connection
                    descriptor = DataSource(
                        source_id=source.source_id,
                        relative_path=f"database://{alias}/{source.location}",
                        file_format=source.format.casefold(),
                        size_bytes=0,
                    )
                    source_size = None
                else:
                    path = resolve_source_location(source, workspace.source_config_path)
                    descriptor = DataSource(
                        source_id=source.source_id,
                        relative_path=str(path),
                        file_format=infer_table_format(path, source.format),
                        size_bytes=path.stat().st_size,
                    )
                    source_size = descriptor.size_bytes
                source_descriptors.append(descriptor)
                event_log.event(
                    "source_loaded",
                    source_id=source.source_id,
                    file_format=descriptor.file_format,
                    size_bytes=source_size,
                    rows=profile.row_count,
                )

            runnable_rules = tuple(
                rule
                for rule in selected_rules
                if set(rule.source_ids) <= set(table_names)
            )
            unavailable_rules = [
                rule.rule_id for rule in selected_rules if rule not in runnable_rules
            ]
            source_errors.extend(
                f"{rule_id}: required source could not be loaded"
                for rule_id in unavailable_rules
            )
            dependency_analysis = analyze_data_dependencies(
                table_store,
                table_names,
                profiles,
                runnable_rules,
            )
            business_analysis = analyze_business_logic(
                table_store,
                profiles,
                dependency_analysis,
                [
                    item.model_dump(mode="json")
                    for item in workspace.relationships.relationships
                ],
            )
            rule_applicability = {
                item["rule_id"]: item
                for item in dependency_analysis["rule_applicability"]
            }
            runtime = AuditRuntimeContext(
                run_id=actual_run_id,
                table_store=table_store,
                table_names=table_names,
                evidence_store=evidence_store,
                relationships={
                    item.relationship_id: item
                    for item in workspace.relationships.relationships
                },
                rule_applicability=rule_applicability,
            )
            findings, rule_results = execute_rules(runtime, runnable_rules)

        event_log.event(
            "rules_completed",
            results=[item.model_dump(mode="json") for item in rule_results],
        )
        findings = deduplicate_findings(findings)
        errors = [
            *source_errors,
            *[
                f"{item.rule_id}: {item.error}"
                for item in rule_results
                if item.status == RuleStatus.ERROR
            ],
        ]
        finding_reviews, audit_plan = review_findings_and_build_plan(
            findings,
            errors,
            source_descriptors,
            rule_applicability,
        )
        source_locations = {
            item.source_id: item.relative_path for item in source_descriptors
        }
        audit_plan.extend(
            business_hypothesis_plan_items(business_analysis, source_locations)
        )
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        audit_plan.sort(
            key=lambda item: (severity_order[item.priority], item.plan_id)
        )
        completed_at = datetime.now(timezone.utc)
        result = AgentRunResult(
            run_id=actual_run_id,
            status=RunStatus.COMPLETED_WITH_ERRORS if errors else RunStatus.COMPLETED,
            started_at=started_at,
            completed_at=completed_at,
            agent_version=self.agent_version,
            data_root=str(workspace.data_root),
            data_sources=source_descriptors,
            auditor_query=auditor_query,
            rule_results=rule_results,
            findings=findings,
            finding_reviews=finding_reviews,
            audit_plan=audit_plan,
            execution_errors=errors,
            metrics={
                "discovered_sources_count": len(workspace.sources.sources),
                "loaded_tables_count": len(table_names),
                "profiled_tables_count": len(profiles),
                "applicable_rules_count": len(workspace.rules),
                "selected_rules_count": len(selected_rules),
                "executed_rules_count": len(rule_results),
                "skipped_rules": workspace.skipped_rules,
                "findings_count": len(findings),
                "evidence_count": sum(len(item.evidence_ids) for item in rule_results),
                "rule_applicability": dependency_analysis["rule_applicability"],
                "business_hypotheses_count": len(
                    business_analysis["semantic_hypotheses"]
                ),
                "duration_seconds": (completed_at - started_at).total_seconds(),
            },
        )
        paths = write_run_outputs(result, run_output_dir)
        paths["discovered_sources"] = _write_json(
            run_output_dir / "discovered_sources.json",
            workspace.sources.model_dump(mode="json"),
        )
        paths["relationships"] = _write_json(
            run_output_dir / "relationships.json",
            workspace.relationships.model_dump(mode="json"),
        )
        paths["selected_rules"] = _write_json(
            run_output_dir / "selected_rules.json",
            {
                "selected": [rule.model_dump(mode="json") for rule in selected_rules],
                "skipped": workspace.skipped_rules,
            },
        )
        paths["profiles"] = _write_json(
            run_output_dir / "profiles.json",
            [profile.model_dump(mode="json") for profile in profiles],
        )
        paths["data_dependencies"] = _write_json(
            run_output_dir / "data_dependencies.json",
            dependency_analysis,
        )
        paths["business_analysis"] = _write_json(
            run_output_dir / "business_analysis.json",
            business_analysis,
        )
        refresh_run_manifest_files(run_output_dir)
        event_log.event(
            "audit_outputs_written",
            status=result.status.value,
            findings_count=len(result.findings),
            execution_errors_count=len(result.execution_errors),
            files={name: str(path) for name, path in paths.items()},
        )
        return result, paths
