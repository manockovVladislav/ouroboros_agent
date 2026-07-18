from __future__ import annotations

import secrets
import os
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path
from .case_package import load_case_package, select_relevant_rules
from .config import resolve_source_location
from .data_loader import (
    DuckDBTableStore,
    infer_table_format,
    is_database_source,
)
from .evidence_store import EvidenceStore
from .finding_builder import (
    deduplicate_findings,
    review_findings_and_build_plan,
)
from .models import (
    AgentRunResult,
    ApplicationSettings,
    AuditRuntimeContext,
    DataSource,
    RuleStatus,
    RunStatus,
)
from .report_generator import (
    write_run_outputs,
)
from .run_logging import RunEventLogger
from .rule_engine import (
    execute_rules,
)


def create_run_id() -> str:

    timestamp = datetime.now(
        timezone.utc
    ).strftime(
        "%Y%m%dT%H%M%SZ"
    )

    suffix = secrets.token_hex(
        3
    ).upper()

    return f"RUN-{timestamp}-{suffix}"


class AuditInsightAgent:

    def __init__(
        self,
        agent_version: str = "0.3.0",
    ) -> None:

        self.agent_version = (
            agent_version
        )

    def run(
        self,
        data_dir: str | Path,
        output_root: str | Path,
        run_id: str | None = None,
    ) -> tuple[
        AgentRunResult,
        dict[str, Path],
    ]:

        data_root = Path(data_dir).expanduser().resolve()
        if not data_root.is_dir():
            raise NotADirectoryError(f"Data directory not found: {data_root}")
        files_by_name = {
            path.name: path.resolve()
            for path in data_root.rglob("*")
            if path.is_file()
        }
        cases_root = Path(
            os.getenv(
                "AUDIT_AGENT_CASES_ROOT",
                str(Path(__file__).resolve().parents[2] / "cases"),
            )
        ).expanduser().resolve()
        candidates = []
        for case_dir in sorted(path for path in cases_root.iterdir() if path.is_dir()):
            if not (case_dir / "data_sources.yaml").is_file():
                continue
            package = load_case_package(case_dir)
            table_sources = [
                source for source in package.sources.sources
                if source.enabled and source.source_type == "table"
            ]
            matches = {
                source.source_id: files_by_name[Path(source.location).name]
                for source in table_sources
                if Path(source.location).name in files_by_name
            }
            if matches:
                candidates.append((len(matches), case_dir.name, case_dir, matches))
        if not candidates:
            raise ValueError(
                f"Не найден case-пакет для файлов из {data_root}"
            )
        candidates.sort(key=lambda item: (-item[0], item[1]))
        best_score, _, case_dir, overrides = candidates[0]
        if len(candidates) > 1 and candidates[1][0] == best_score:
            raise ValueError("Неоднозначный выбор case-пакета")
        return self.run_case(
            case_dir=case_dir,
            auditor_query="Выполнить полный аудит всех доступных данных",
            output_root=output_root,
            run_id=run_id,
            source_overrides=overrides,
        )

    def run_case(
        self,
        case_dir: str | Path,
        auditor_query: str,
        output_root: str | Path,
        run_id: str | None = None,
        database: str | Path = ":memory:",
        shared_rules_dir: str | Path | None = None,
        source_overrides: dict[str, str | Path] | None = None,
        selected_rule_ids: set[str] | None = None,
        settings: ApplicationSettings | None = None,
        selected_replica: str | None = None,
    ) -> tuple[AgentRunResult, dict[str, Path]]:
        """Execute a self-contained case package through the generic audit core."""

        started_at = datetime.now(timezone.utc)
        actual_run_id = run_id or create_run_id()
        run_output_dir = Path(output_root).expanduser().resolve() / actual_run_id
        event_log = RunEventLogger(run_output_dir, actual_run_id)
        event_log.event(
            "audit_started",
            case_dir=str(Path(case_dir).expanduser().resolve()),
            auditor_query_length=len(auditor_query),
        )
        package = load_case_package(case_dir, shared_rules_dir=shared_rules_dir)
        event_log.event(
            "case_loaded",
            case_name=package.name,
            configured_sources=len(package.sources.sources),
            configured_rules=len(package.rules),
        )
        if selected_rule_ids is None:
            selected_rules = select_relevant_rules(auditor_query, package.rules)
        else:
            available = {rule.rule_id: rule for rule in package.rules if rule.enabled}
            unknown_rules = selected_rule_ids - set(available)
            if unknown_rules:
                raise ValueError(f"Unknown rule IDs: {sorted(unknown_rules)}")
            selected_rules = tuple(
                available[rule_id] for rule_id in sorted(selected_rule_ids)
            )
        event_log.event(
            "rules_selected",
            rule_ids=[rule.rule_id for rule in selected_rules],
            count=len(selected_rules),
        )
        selected_source_ids = {
            source_id for rule in selected_rules for source_id in rule.source_ids
        }
        source_config_path = package.root / "data_sources.yaml"
        overrides = source_overrides or {}
        unknown_overrides = set(overrides) - {
            source.source_id for source in package.sources.sources
        }
        if unknown_overrides:
            raise ValueError(f"Unknown source overrides: {sorted(unknown_overrides)}")
        selected_sources = [
            source.model_copy(
                update={"location": str(Path(overrides[source.source_id]).expanduser().resolve())}
            )
            if source.source_id in overrides
            else source
            for source in package.sources.sources
            if source.enabled and source.source_id in selected_source_ids
        ]
        evidence_store = EvidenceStore(run_output_dir / "evidence")
        source_descriptors = []

        with DuckDBTableStore(
            database,
            database_settings=settings.databases if settings else None,
            selected_replica=selected_replica,
        ) as table_store:
            table_names = {}
            for source in selected_sources:
                if source.source_type != "table":
                    raise ValueError(
                        f"Executable rule source must be a table: {source.source_id}"
                    )
                event_log.event("source_loading", source_id=source.source_id)
                try:
                    table_names[source.source_id] = table_store.ingest(
                        source,
                        source_config_path,
                    )
                except Exception as error:
                    event_log.exception(
                        "source_loading_failed", error, source_id=source.source_id
                    )
                    raise
                if is_database_source(source):
                    alias = (
                        selected_replica
                        if source.connection == "$selected"
                        else source.connection
                    )
                    source_descriptors.append(
                        DataSource(
                            source_id=source.source_id,
                            relative_path=f"database://{alias}/{source.location}",
                            file_format=source.format.casefold(),
                            size_bytes=0,
                        )
                    )
                    source_format = source.format.casefold()
                    source_size = None
                else:
                    path = resolve_source_location(source, source_config_path)
                    source_descriptors.append(
                        DataSource(
                            source_id=source.source_id,
                            relative_path=str(path),
                            file_format=infer_table_format(path, source.format),
                            size_bytes=path.stat().st_size,
                        )
                    )
                    source_format = infer_table_format(path, source.format)
                    source_size = path.stat().st_size
                event_log.event(
                    "source_loaded",
                    source_id=source.source_id,
                    file_format=source_format,
                    size_bytes=source_size,
                )

            runtime = AuditRuntimeContext(
                run_id=actual_run_id,
                table_store=table_store,
                table_names=table_names,
                evidence_store=evidence_store,
                relationships={
                    item.relationship_id: item
                    for item in package.relationships.relationships
                },
            )
            findings, rule_results = execute_rules(runtime, selected_rules)
        event_log.event(
            "rules_completed",
            results=[
                {
                    "rule_id": item.rule_id,
                    "status": item.status.value,
                    "findings_count": item.findings_count,
                    "error": item.error,
                }
                for item in rule_results
            ],
        )

        findings = deduplicate_findings(findings)
        errors = [
            f"{result.rule_id}: {result.error}"
            for result in rule_results
            if result.status == RuleStatus.ERROR
        ]
        finding_reviews, audit_plan = review_findings_and_build_plan(
            findings, errors, source_descriptors
        )
        completed_at = datetime.now(timezone.utc)
        result = AgentRunResult(
            run_id=actual_run_id,
            status=RunStatus.COMPLETED_WITH_ERRORS if errors else RunStatus.COMPLETED,
            started_at=started_at,
            completed_at=completed_at,
            agent_version=self.agent_version,
            data_root=str(package.root),
            data_sources=source_descriptors,
            case_name=package.name,
            auditor_query=auditor_query,
            rule_results=rule_results,
            findings=findings,
            finding_reviews=finding_reviews,
            audit_plan=audit_plan,
            execution_errors=errors,
            metrics={
                "selected_sources_count": len(selected_sources),
                "selected_rules_count": len(selected_rules),
                "findings_count": len(findings),
                "evidence_count": sum(len(item.evidence_ids) for item in rule_results),
                "duration_seconds": (completed_at - started_at).total_seconds(),
            },
        )
        paths = write_run_outputs(result, run_output_dir)
        event_log.event(
            "audit_outputs_written",
            status=result.status.value,
            findings_count=len(result.findings),
            execution_errors_count=len(result.execution_errors),
            files={name: str(path) for name, path in paths.items()},
        )
        return result, paths
