"""Execution of validated declarative checks without arbitrary Python."""
from __future__ import annotations

import hashlib
from typing import Any

from .analysis_tools import normalize_row, render_safe_select
from .anomaly_detector import compile_anomaly
from .data_loader import quote_identifier
from .evidence_store import build_evidence_record
from .finding_builder import build_finding
from .models import (
    AuditRule,
    AuditRuntimeContext,
    CandidateFinding,
    EvidenceReference,
    RuleKind,
    RuleResult,
    RuleStatus,
)
from .reconciliation import compile_reconciliation
from .run_logging import RunEventLogger


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def compile_timeline(rule: AuditRule, table_names: dict[str, str]) -> str:
    spec = rule.timeline
    if spec is None:
        raise ValueError("Missing timeline specification")
    table = quote_identifier(table_names[spec.source_id])
    entities = [quote_identifier(field) for field in spec.entity_fields]
    timestamp = quote_identifier(spec.timestamp_field)
    timestamp_value = f"CAST({timestamp} AS TIMESTAMP)"
    event = quote_identifier(spec.event_field)
    partition = ", ".join(entities)
    rank_expression = "CASE " + " ".join(
        f"WHEN {event} = {_quote_literal(value)} THEN {index}"
        for index, value in enumerate(spec.expected_order, start=1)
    ) + " END"
    object_parts = ", ".join(f"CAST({field} AS VARCHAR)" for field in entities)
    gap_condition = ""
    if spec.max_gap_seconds is not None:
        gap_condition = (
            f" OR date_diff('second', previous_timestamp, {timestamp_value}) > {spec.max_gap_seconds}"
        )
    return (
        f"WITH ranked AS (SELECT *, {rank_expression} AS event_rank FROM {table}), "
        f"ordered AS (SELECT *, LAG(event_rank) OVER (PARTITION BY {partition} ORDER BY {timestamp_value}) "
        f"AS previous_rank, LAG({timestamp_value}) OVER (PARTITION BY {partition} ORDER BY {timestamp_value}) "
        "AS previous_timestamp FROM ranked) "
        f"SELECT concat_ws('|', {object_parts}) AS object_id, * FROM ordered "
        "WHERE event_rank IS NULL OR (previous_rank IS NULL AND event_rank <> 1) "
        "OR (previous_rank IS NOT NULL AND (event_rank < previous_rank "
        f"OR event_rank > previous_rank + 1)){gap_condition}"
    )


def compile_rule_query(rule: AuditRule, context: AuditRuntimeContext) -> tuple[str, str | None]:
    if rule.kind in {RuleKind.SQL, RuleKind.CONTRADICTION}:
        assert rule.sql is not None
        return (
            render_safe_select(rule.sql.query, context.table_names, rule.source_ids),
            rule.sql.object_id_column,
        )
    if rule.kind == RuleKind.RECONCILIATION:
        assert rule.reconciliation is not None
        relationship = context.relationships.get(rule.reconciliation.relationship_id)
        if relationship is None:
            raise ValueError(f"Unknown relationship: {rule.reconciliation.relationship_id}")
        return compile_reconciliation(rule, relationship, context.table_names), "object_id"
    if rule.kind == RuleKind.TIMELINE:
        return compile_timeline(rule, context.table_names), "object_id"
    if rule.kind == RuleKind.ANOMALY:
        return compile_anomaly(rule, context.table_names), None
    raise ValueError(f"Unsupported rule kind: {rule.kind}")


def _render(template: str, values: dict[str, Any]) -> str:
    return template.format_map(values)


def execute_rule(
    context: AuditRuntimeContext,
    rule: AuditRule,
) -> tuple[list[CandidateFinding], RuleResult]:
    try:
        applicability = context.rule_applicability.get(rule.rule_id, {})
        if applicability.get("status") == "INCOMPATIBLE":
            coverage = float(applicability.get("row_coverage") or 0.0)
            return [], RuleResult(
                rule_id=rule.rule_id,
                status=RuleStatus.ERROR,
                evaluated_rows=int(applicability.get("total_rows") or 0),
                findings_count=0,
                error=(
                    "Rule applicability rejected: observed values do not match "
                    f"the configured vocabulary (row coverage {coverage:.1%})."
                ),
            )
        query, object_id_column = compile_rule_query(rule, context)
        bounded_query = (
            f"SELECT * FROM ({query}) AS audit_rule_matches LIMIT {rule.max_findings + 1}"
        )
        frame = context.table_store.query(bounded_query)
        truncated = len(frame) > rule.max_findings
        if truncated:
            frame = frame.head(rule.max_findings)
        findings = []
        evidence_ids = []
        for raw_row in frame.to_dict(orient="records"):
            row = normalize_row(raw_row)
            object_id = (
                str(row[object_id_column])
                if object_id_column and row.get(object_id_column) is not None
                else hashlib.sha256(
                    repr(sorted(row.items())).encode("utf-8")
                ).hexdigest()[:20]
            )
            evidence = build_evidence_record(
                context.run_id,
                rule,
                object_id,
                query,
                row,
            )
            context.evidence_store.save(evidence)
            evidence_ids.append(evidence.evidence_id)
            template = rule.finding
            reference = EvidenceReference(
                evidence_id=evidence.evidence_id,
                checksum=evidence.checksum,
                source_name=", ".join(rule.source_ids),
                object_id=object_id,
                description=f"Rule {rule.rule_id} returned an exception row",
                fields=row,
                query=query,
            )
            findings.append(
                build_finding(
                    check_id=rule.rule_id,
                    issue_type=template.issue_type,
                    primary_object_id=object_id,
                    object_id=object_id,
                    title=_render(template.title, row),
                    summary=_render(template.summary, row),
                    root_cause=_render(template.root_cause, row),
                    criterion=_render(template.criterion, row),
                    risk=_render(template.risk, row),
                    recommendation=(
                        _render(template.recommendation, row)
                        if template.recommendation
                        else None
                    ),
                    severity=template.severity,
                    confidence=template.confidence,
                    evidence=[reference],
                    facts=row,
                    tags=[*rule.tags, *template.tags],
                )
            )
        return findings, RuleResult(
            rule_id=rule.rule_id,
            status=RuleStatus.FAIL if findings else RuleStatus.PASS,
            evaluated_rows=len(frame),
            findings_count=len(findings),
            evidence_ids=evidence_ids,
            truncated=truncated,
        )
    except Exception as error:
        return [], RuleResult(
            rule_id=rule.rule_id,
            status=RuleStatus.ERROR,
            evaluated_rows=0,
            findings_count=0,
            error=f"{type(error).__name__}: {error}",
        )


def execute_rules(
    context: AuditRuntimeContext,
    rules: tuple[AuditRule, ...],
) -> tuple[list[CandidateFinding], list[RuleResult]]:
    findings = []
    results = []
    event_log = RunEventLogger(context.evidence_store.root.parent, context.run_id)
    for rule in rules:
        event_log.event(
            "rule_started",
            rule_id=rule.rule_id,
            rule_kind=rule.kind.value,
            source_ids=rule.source_ids,
        )
        rule_findings, result = execute_rule(context, rule)
        findings.extend(rule_findings)
        results.append(result)
        event_log.event(
            "rule_completed",
            level="ERROR" if result.status == RuleStatus.ERROR else "INFO",
            rule_id=result.rule_id,
            status=result.status.value,
            evaluated_rows=result.evaluated_rows,
            findings_count=result.findings_count,
            truncated=result.truncated,
            error=result.error,
        )
    return findings, results
