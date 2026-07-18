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
from .data_loader import (
    discover_data_sources,
)
from .finding_builder import (
    deduplicate_findings,
)
from .models import (
    AgentRunResult,
    AuditContext,
    CandidateFinding,
    RunStatus,
)
from .reconciliation import (
    run_reconciliations,
)
from .report_generator import (
    write_run_outputs,
)
from .rule_engine import (
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

