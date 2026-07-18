from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


"""Валидируемые контракты данных между модулями аудиторского ядра."""


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RunStatus(str, Enum):
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_ERRORS = "COMPLETED_WITH_ERRORS"
    FAILED = "FAILED"


class RuleKind(str, Enum):
    SQL = "sql"
    CONTRADICTION = "contradiction"
    RECONCILIATION = "reconciliation"
    TIMELINE = "timeline"
    ANOMALY = "anomaly"


class RuleStatus(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    ERROR = "ERROR"


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


class SourceConfig(BaseModel):
    """Declarative description of a source available to the ingestion core."""

    model_config = ConfigDict(extra="forbid")

    source_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    source_type: Literal["table", "document"]
    location: str = ""
    format: str = "auto"
    enabled: bool = True
    table_name: str | None = None
    expected_fields: list[str] = Field(default_factory=list)
    primary_key: list[str] = Field(default_factory=list)
    encoding: str = "utf-8"
    options: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    connection: str | None = Field(
        default=None, pattern=r"^(?:\$selected|[A-Za-z0-9_.-]{1,80})$"
    )
    query: str | None = None

    @model_validator(mode="after")
    def database_source_is_complete(self) -> SourceConfig:
        if self.format.casefold() in {"postgresql", "postgres", "greenplum"}:
            if self.source_type != "table":
                raise ValueError("Database sources must use source_type=table")
            if not self.connection:
                raise ValueError("Database source requires connection alias")
            if not self.location and not self.query:
                raise ValueError("Database source requires location or query")
        elif self.connection or self.query:
            raise ValueError("connection/query are allowed only for database sources")
        elif not self.location:
            raise ValueError("File/document source requires location")
        return self


class SourceCatalog(BaseModel):
    """Validated contents of a source registry YAML file."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    sources: list[SourceConfig]

    @model_validator(mode="after")
    def source_ids_are_unique(self) -> SourceCatalog:
        source_ids = [source.source_id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source_id values must be unique")
        return self


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["sentence_transformers"] = "sentence_transformers"
    model: str = "BAAI/bge-m3"
    device: str | None = None
    batch_size: int = Field(default=16, gt=0)


class QdrantSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str | None = None
    path: str = ".audit_insight/qdrant"
    collection: str = "audit_knowledge"
    api_key_env: str = "QDRANT_API_KEY"


class OuroborosSettings(BaseModel):
    """Connection to the separately running Ouroboros gateway."""

    model_config = ConfigDict(extra="forbid")

    url: str = "http://127.0.0.1:8765"
    workspace: str = "."
    python_executable: str = ".venv/bin/python"
    password_env: str = "OUROBOROS_NETWORK_PASSWORD"
    poll_interval_seconds: float = Field(default=1.0, ge=0.1, le=30)
    timeout_seconds: int = Field(default=900, ge=10, le=86_400)


class SelfImprovementSettings(BaseModel):
    """Controlled post-audit improvement performed by Ouroboros."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    require_detected_gap: bool = True
    allow_blocking_clarification: bool = True
    max_changed_files: int = Field(default=20, ge=1, le=100)


class DatabaseConnectionSettings(BaseModel):
    """One allowlisted PostgreSQL-compatible read replica."""

    model_config = ConfigDict(extra="forbid")

    engine: Literal["postgresql", "greenplum"] = "postgresql"
    host: str = Field(min_length=1)
    port: int = Field(default=5432, ge=1, le=65535)
    database: str = Field(min_length=1)
    user: str = Field(min_length=1)
    password_env: str = Field(min_length=1)
    sslmode: str = "prefer"
    connect_timeout_seconds: int = Field(default=15, ge=1, le=120)


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    connections: dict[str, DatabaseConnectionSettings] = Field(default_factory=dict)
    fetch_size: int = Field(default=10_000, ge=100, le=1_000_000)

    @model_validator(mode="after")
    def aliases_are_safe(self) -> DatabaseSettings:
        invalid = [
            alias
            for alias in self.connections
            if not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", alias)
        ]
        if invalid:
            raise ValueError(f"Invalid database connection aliases: {invalid}")
        return self


class StorageSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    duckdb_path: str = ".audit_insight/audit.duckdb"


class ChunkingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    size: int = Field(default=1200, gt=0)
    overlap: int = Field(default=200, ge=0)

    @model_validator(mode="after")
    def overlap_is_smaller_than_size(self) -> ChunkingSettings:
        if self.overlap >= self.size:
            raise ValueError("chunking.overlap must be smaller than chunking.size")
        return self


class ApplicationSettings(BaseModel):
    """Shared runtime settings for models and analytical storage."""

    model_config = ConfigDict(extra="forbid")

    version: int = 1
    environment: str = "development"
    embedding: EmbeddingSettings = Field(default_factory=EmbeddingSettings)
    qdrant: QdrantSettings = Field(default_factory=QdrantSettings)
    ouroboros: OuroborosSettings = Field(default_factory=OuroborosSettings)
    self_improvement: SelfImprovementSettings = Field(
        default_factory=SelfImprovementSettings
    )
    databases: DatabaseSettings = Field(default_factory=DatabaseSettings)
    storage: StorageSettings = Field(default_factory=StorageSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    ingestion_output: str = "outputs/ingestion-result.json"


class ColumnProfile(BaseModel):
    """Reproducible structure and quality metrics for one column."""

    model_config = ConfigDict(extra="forbid")

    name: str
    data_type: str
    null_count: int
    null_ratio: float
    distinct_count: int
    min_value: Any | None = None
    max_value: Any | None = None


class DataProfile(BaseModel):
    """Dataset-level profile created from a table registered in DuckDB."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    table_name: str
    row_count: int
    column_count: int
    duplicate_row_count: int
    primary_key_duplicate_count: int | None = None
    missing_expected_fields: list[str] = Field(default_factory=list)
    columns: list[ColumnProfile]


class DocumentChunk(BaseModel):
    """A searchable document fragment with traceable provenance."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: str
    source_id: str
    text: str
    chunk_index: int
    start_char: int
    end_char: int
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchResult(BaseModel):
    """A ranked document fragment returned by the knowledge index."""

    model_config = ConfigDict(extra="forbid")

    chunk: DocumentChunk
    score: float


class IngestionResult(BaseModel):
    """Machine-readable summary of one configuration-driven ingestion run."""

    model_config = ConfigDict(extra="forbid")

    registered_tables: dict[str, str] = Field(default_factory=dict)
    profiles: list[DataProfile] = Field(default_factory=list)
    indexed_chunks: int = 0


class FeedbackForOuroboros(BaseModel):
    """Sanitized evaluator feedback; exact case answers are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: str = "1.0"
    run_id: str
    case_name: str
    summary: str
    weaknesses: list[str] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    aggregate_metrics: dict[str, float] = Field(default_factory=dict)
    previous_run_id: str | None = None
    quality_improved: bool | None = None


class RelationshipKey(BaseModel):
    model_config = ConfigDict(extra="forbid")

    left: str
    right: str


class RelationshipConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    left_source: str
    right_source: str
    keys: list[RelationshipKey] = Field(min_length=1)
    cardinality: Literal["one_to_one", "one_to_many", "many_to_one", "many_to_many"] = "many_to_many"


class RelationshipCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    relationships: list[RelationshipConfig] = Field(default_factory=list)


class FindingTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    summary: str
    issue_type: str
    root_cause: str
    criterion: str
    risk: str
    recommendation: str | None = None
    severity: Severity
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    tags: list[str] = Field(default_factory=list)


class SqlRuleSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str
    object_id_column: str | None = None


class ReconciliationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship_id: str
    left_value: str
    right_value: str
    aggregation: Literal["sum", "count", "avg", "min", "max"] = "sum"
    tolerance: float = Field(default=0.0, ge=0.0)


class TimelineSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    entity_fields: list[str] = Field(min_length=1)
    timestamp_field: str
    event_field: str
    expected_order: list[str] = Field(min_length=2)
    max_gap_seconds: int | None = Field(default=None, gt=0)


class AnomalySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    field: str
    group_by: list[str] = Field(default_factory=list)
    method: Literal["zscore", "iqr"] = "zscore"
    threshold: float = Field(default=3.0, gt=0.0)


class AuditRule(BaseModel):
    """Strict declarative check; exactly one kind-specific specification applies."""

    model_config = ConfigDict(extra="forbid")

    rule_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+$")
    version: str = "1"
    kind: RuleKind
    description: str = ""
    source_ids: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    enabled: bool = True
    max_findings: int = Field(default=100, gt=0, le=10_000)
    sql: SqlRuleSpec | None = None
    reconciliation: ReconciliationSpec | None = None
    timeline: TimelineSpec | None = None
    anomaly: AnomalySpec | None = None
    finding: FindingTemplate

    @model_validator(mode="after")
    def matching_spec_is_present(self) -> AuditRule:
        expected_specifications = {
            RuleKind.SQL: self.sql,
            RuleKind.CONTRADICTION: self.sql,
            RuleKind.RECONCILIATION: self.reconciliation,
            RuleKind.TIMELINE: self.timeline,
            RuleKind.ANOMALY: self.anomaly,
        }
        if expected_specifications[self.kind] is None:
            raise ValueError(f"Rule {self.rule_id} requires a {self.kind.value} specification")
        if sum(
            value is not None
            for value in (self.sql, self.reconciliation, self.timeline, self.anomaly)
        ) != 1:
            raise ValueError(f"Rule {self.rule_id} must contain exactly one specification")
        return self


class RuleCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = 1
    rules: list[AuditRule]


class EvidenceRecord(BaseModel):
    """Immutable and checksum-protected reproduction record for one failed check row."""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    checksum: str
    run_id: str
    rule_id: str
    rule_version: str
    rule_hash: str
    rule_kind: RuleKind
    source_ids: list[str]
    object_id: str
    query: str
    result: dict[str, Any]
    created_at: datetime


class RuleResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_id: str
    status: RuleStatus
    evaluated_rows: int
    findings_count: int
    evidence_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    error: str | None = None


class EvidenceReference(BaseModel):
    """
    Воспроизводимое доказательство,
    на котором основан вывод агента.
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    evidence_id: str | None = None
    checksum: str | None = None
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

    object_id: str | None = None
    event_start_ts: datetime | None = None
    event_end_ts: datetime | None = None
    criterion: str = ""
    risk: str = ""
    root_cause: str
    recommendation: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)

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


class FindingReview(BaseModel):
    """Adversarial evidence check performed before presenting a finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    verdict: Literal["CONFIRMED", "REQUIRES_VALIDATION", "REJECTED"]
    rationale: str
    evidence_checks: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    source_locations: list[str] = Field(default_factory=list)


class AuditPlanItem(BaseModel):
    """Ranked, evidence-bounded direction for audit work."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    priority: Severity
    status: Literal["CONFIRMED_ISSUE", "POTENTIAL_RISK", "BLOCKED_CHECK"]
    title: str
    rationale: str
    finding_id: str | None = None
    sources: list[str] = Field(default_factory=list)
    source_locations: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """
    Полный результат одного запуска.
    """

    model_config = ConfigDict(
        extra="forbid",
    )

    schema_version: str = "2.0"

    run_id: str
    status: RunStatus

    started_at: datetime
    completed_at: datetime

    agent_version: str

    data_root: str
    data_sources: list[DataSource]

    case_name: str | None = None
    auditor_query: str | None = None
    rule_results: list[RuleResult] = Field(default_factory=list)

    findings: list[CandidateFinding]
    finding_reviews: list[FindingReview] = Field(default_factory=list)
    audit_plan: list[AuditPlanItem] = Field(default_factory=list)

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


@dataclass(frozen=True, slots=True)
class AuditRuntimeContext:
    """Case-neutral runtime dependencies passed to declarative checks."""

    run_id: str
    table_store: Any
    table_names: dict[str, str]
    evidence_store: Any
    relationships: dict[str, RelationshipConfig]
