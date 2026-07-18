from __future__ import annotations

import secrets
from datetime import (
    datetime,
    timezone,
)
from pathlib import Path
from typing import Callable

from .anomaly_detector import (
    detect_anomalies,
)
from .case_package import load_case_package, select_relevant_rules
from .config import resolve_source_location
from .data_loader import (
    DuckDBTableStore,
    discover_data_sources,
    infer_table_format,
)
from .evidence_store import EvidenceStore
from .finding_builder import (
    deduplicate_findings,
)
from .models import (
    AgentRunResult,
    AuditContext,
    AuditRuntimeContext,
    CandidateFinding,
    DataSource,
    RuleStatus,
    RunStatus,
)
from .reconciliation import (
    run_reconciliations,
)
from .report_generator import (
    write_run_outputs,
)
from .rule_engine import (
    execute_rules,
    run_rules,
)


CheckFunction = Callable[
    [AuditContext],
    list[CandidateFinding],
]

"""Оркестрация полного аудиторского сценария.

TODO:
- принимать AgentRequest и возвращать AgentResponse;
- координировать загрузку, профилирование, правила, поиск и отчётность;
- не дублировать бизнес-логику специализированных модулей;
- обеспечивать трассируемость каждого шага через evidence_store.
"""


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
        agent_version: str = "0.1.0",
    ) -> None:

        self.agent_version = (
            agent_version
        )

        self.checks: list[
            tuple[str, CheckFunction]
        ] = [
            (
                "rule_engine",
                run_rules,
            ),
            (
                "reconciliation",
                run_reconciliations,
            ),
            (
                "anomaly_detector",
                detect_anomalies,
            ),
        ]

    def run(
        self,
        data_dir: str | Path,
        output_root: str | Path,
        run_id: str | None = None,
    ) -> tuple[
        AgentRunResult,
        dict[str, Path],
    ]:

        started_at = datetime.now(
            timezone.utc
        )

        actual_run_id = (
            run_id
            or create_run_id()
        )

        data_root = Path(
            data_dir
        ).expanduser().resolve()

        sources = discover_data_sources(
            data_root
        )

        context = AuditContext(
            run_id=actual_run_id,
            data_root=data_root,
            data_sources=tuple(
                sources
            ),
        )

        findings: list[
            CandidateFinding
        ] = []

        errors: list[str] = []

        check_metrics: dict[
            str,
            dict[str, int | str]
        ] = {}

        for (
            check_name,
            check_function,
        ) in self.checks:

            try:
                check_findings = (
                    check_function(
                        context
                    )
                )

                findings.extend(
                    check_findings
                )

                check_metrics[
                    check_name
                ] = {
                    "status": "SUCCESS",
                    "findings_count": len(
                        check_findings
                    ),
                }

            except Exception as error:

                error_message = (
                    f"{check_name}: "
                    f"{type(error).__name__}: "
                    f"{error}"
                )

                errors.append(
                    error_message
                )

                check_metrics[
                    check_name
                ] = {
                    "status": "ERROR",
                    "findings_count": 0,
                }

        findings = deduplicate_findings(
            findings
        )

        completed_at = datetime.now(
            timezone.utc
        )

        if errors:
            status = (
                RunStatus
                .COMPLETED_WITH_ERRORS
            )
        else:
            status = (
                RunStatus.COMPLETED
            )

        result = AgentRunResult(
            run_id=actual_run_id,
            status=status,
            started_at=started_at,
            completed_at=completed_at,
            agent_version=(
                self.agent_version
            ),
            data_root=str(
                data_root
            ),
            data_sources=sources,
            findings=findings,
            execution_errors=errors,
            metrics={
                "data_sources_count": len(
                    sources
                ),
                "findings_count": len(
                    findings
                ),
                "checks": check_metrics,
                "duration_seconds": (
                    completed_at
                    - started_at
                ).total_seconds(),
            },
        )

        run_output_dir = (
            Path(output_root)
            / actual_run_id
        )

        paths = write_run_outputs(
            result=result,
            output_dir=run_output_dir,
        )

        return result, paths

    def run_case(
        self,
        case_dir: str | Path,
        auditor_query: str,
        output_root: str | Path,
        run_id: str | None = None,
        database: str | Path = ":memory:",
        shared_rules_dir: str | Path | None = None,
        source_overrides: dict[str, str | Path] | None = None,
    ) -> tuple[AgentRunResult, dict[str, Path]]:
        """Execute a self-contained case package through the generic audit core."""

        started_at = datetime.now(timezone.utc)
        actual_run_id = run_id or create_run_id()
        package = load_case_package(case_dir, shared_rules_dir=shared_rules_dir)
        selected_rules = select_relevant_rules(auditor_query, package.rules)
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
        run_output_dir = Path(output_root).expanduser().resolve() / actual_run_id
        evidence_store = EvidenceStore(run_output_dir / "evidence")
        source_descriptors = []

        with DuckDBTableStore(database) as table_store:
            table_names = {}
            for source in selected_sources:
                if source.source_type != "table":
                    raise ValueError(
                        f"Executable rule source must be a table: {source.source_id}"
                    )
                table_names[source.source_id] = table_store.ingest(
                    source,
                    source_config_path,
                )
                path = resolve_source_location(source, source_config_path)
                source_descriptors.append(
                    DataSource(
                        source_id=source.source_id,
                        relative_path=str(path),
                        file_format=infer_table_format(path, source.format),
                        size_bytes=path.stat().st_size,
                    )
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

        findings = deduplicate_findings(findings)
        errors = [
            f"{result.rule_id}: {result.error}"
            for result in rule_results
            if result.status == RuleStatus.ERROR
        ]
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
        return result, paths
