from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


"""Единые структуры данных и контракты между модулями.

TODO определить модели:
- AuditFinding: факт, критерий, риск, основание, доказательства, рекомендация;
- Evidence: источник, строки/фрагменты, правило, расчёт и результат;
- RuleResult: идентификатор правила, статус, серьёзность и детали проверки;
- DocumentChunk: фрагмент документа с метаданными и ссылкой на источник;
- DataProfile: схема, качество, статистики и обнаруженные проблемы набора;
- AgentRequest: входные данные, область и параметры запуска агента;
- AgentResponse: результаты, наблюдения, доказательства и служебные метаданные.

Все модули должны обмениваться этими объектами, а не несогласованными dict.
"""


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class DataSource(BaseModel):
    """
    Публичный источник, доступный агенту.
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    source_id: str
    relative_path: str
    file_format: str
    size_bytes: int


class EvidenceReference(BaseModel):
    """
    Воспроизводимое доказательство,
    на котором основан вывод агента.
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    source_name: str
    object_id: str | None = None

    description: str

    row_filters: dict[str, Any] = Field(
        default_factory=dict,
    )

    fields: dict[str, Any] = Field(
        default_factory=dict,
    )

    query: str | None = None


class CandidateFinding(BaseModel):
    """
    Структурированное аудиторское наблюдение.
    Именно эти объекты проверяет evaluator.
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    finding_id: str
    check_id: str

    title: str
    summary: str

    issue_type: str
    severity: Severity

    currency: str | None = None

    event_start_ts: datetime | None = None
    event_end_ts: datetime | None = None

    amount: float | None = None

    expected_portfolio_id: str | None = None
    actual_portfolio_id: str | None = None

    mapping_rule_id: str | None = None
    shipment_id: str | None = None
    deal_id: str | None = None
    cash_operation_id: str | None = None

    related_movement_ids: list[str] = Field(
        default_factory=list,
    )

    related_accounting_entry_ids: list[str] = Field(
        default_factory=list,
    )

    related_snapshot_ids: list[str] = Field(
        default_factory=list,
    )

    related_hedge_ids: list[str] = Field(
        default_factory=list,
    )

    root_cause: str
    recommendation: str | None = None

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    evidence: list[EvidenceReference] = Field(
        min_length=1,
    )

    tags: list[str] = Field(
        default_factory=list,
    )

    @model_validator(
        mode="after",
    )
    def validate_time_range(
        self,
    ) -> CandidateFinding:

        if (
            self.event_start_ts is not None
            and self.event_end_ts is not None
            and self.event_end_ts
            < self.event_start_ts
        ):
            raise ValueError(
                "event_end_ts не может быть "
                "раньше event_start_ts"
            )

        return self


class AgentRunResult(BaseModel):
    """
    Полный результат одного запуска.
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    schema_version: str = "1.0"

    run_id: str
    status: RunStatus

    started_at: datetime
    completed_at: datetime

    agent_version: str

    data_root: str
    data_sources: list[DataSource]

    findings: list[CandidateFinding]

    execution_errors: list[str] = Field(
        default_factory=list,
    )

    metrics: dict[str, Any] = Field(
        default_factory=dict,
    )


@dataclass(
    frozen=True,
    slots=True,
)
class AuditContext:
    """
    Контекст, передаваемый проверкам.
    """

    run_id: str
    data_root: Path
    data_sources: tuple[DataSource, ...]




