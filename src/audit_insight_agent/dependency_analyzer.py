"""Case-neutral discovery of data links and rule-to-data vocabulary fit."""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any

from .data_loader import DuckDBTableStore, quote_identifier
from .models import AuditRule, DataProfile, RuleKind


_GENERIC_NAME_PARTS = {
    "from",
    "left",
    "right",
    "source",
    "target",
    "to",
}


def _name_parts(name: str) -> set[str]:
    parts = set(re.findall(r"[a-z0-9]+", name.casefold()))
    meaningful = parts - _GENERIC_NAME_PARTS
    return meaningful or parts


def _name_similarity(left: str, right: str) -> float:
    left_parts = _name_parts(left)
    right_parts = _name_parts(right)
    if not left_parts or not right_parts:
        return 0.0
    return len(left_parts & right_parts) / len(left_parts | right_parts)


def _sample_values(
    store: DuckDBTableStore,
    table_name: str,
    column_name: str,
    *,
    limit: int = 500,
) -> set[str]:
    table = quote_identifier(table_name)
    column = quote_identifier(column_name)
    rows = store.connection.execute(
        f"SELECT DISTINCT CAST({column} AS VARCHAR) AS value "
        f"FROM {table} WHERE {column} IS NOT NULL LIMIT {int(limit)}"
    ).fetchall()
    return {str(row[0]).strip() for row in rows if str(row[0]).strip()}


def _cardinality(left_unique: bool, right_unique: bool) -> str:
    if left_unique and right_unique:
        return "one_to_one"
    if left_unique:
        return "one_to_many"
    if right_unique:
        return "many_to_one"
    return "many_to_many"


def _infer_column_dependencies(
    store: DuckDBTableStore,
    profiles: list[DataProfile],
) -> list[dict[str, Any]]:
    candidates = []
    for left, right in combinations(profiles, 2):
        for left_column in left.columns:
            if left_column.distinct_count == 0:
                continue
            for right_column in right.columns:
                if right_column.distinct_count == 0:
                    continue
                name_similarity = _name_similarity(left_column.name, right_column.name)
                if name_similarity < 0.5:
                    continue
                candidates.append(
                    (left, left_column, right, right_column, name_similarity)
                )

    value_cache: dict[tuple[str, str], set[str]] = {}
    dependencies = []
    for left, left_column, right, right_column, name_similarity in candidates:
        left_key = (left.table_name, left_column.name)
        right_key = (right.table_name, right_column.name)
        if left_key not in value_cache:
            value_cache[left_key] = _sample_values(store, *left_key)
        if right_key not in value_cache:
            value_cache[right_key] = _sample_values(store, *right_key)
        left_values = value_cache[left_key]
        right_values = value_cache[right_key]
        if not left_values or not right_values:
            continue
        intersection_count = len(left_values & right_values)
        if intersection_count < 2:
            continue
        left_coverage = intersection_count / len(left_values)
        right_coverage = intersection_count / len(right_values)
        inclusion_coverage = max(left_coverage, right_coverage)
        if inclusion_coverage < 0.8:
            continue
        dependencies.append(
            {
                "left_source": left.source_id,
                "left_column": left_column.name,
                "right_source": right.source_id,
                "right_column": right_column.name,
                "dependency_type": (
                    "join_key_candidate"
                    if left_column.name.casefold().endswith("_id")
                    and right_column.name.casefold().endswith("_id")
                    else "shared_domain_candidate"
                ),
                "relationship": _cardinality(
                    left_column.distinct_count == left.row_count,
                    right_column.distinct_count == right.row_count,
                ),
                "name_similarity": round(name_similarity, 4),
                "sample_intersection_count": intersection_count,
                "left_sample_coverage": round(left_coverage, 4),
                "right_sample_coverage": round(right_coverage, 4),
                "confidence": round(
                    min(1.0, 0.65 * inclusion_coverage + 0.35 * name_similarity),
                    4,
                ),
                "basis": "inferred_from_column_names_and_exact_value_overlap",
            }
        )
    return sorted(
        dependencies,
        key=lambda item: (
            -item["confidence"],
            item["left_source"],
            item["left_column"],
            item["right_source"],
            item["right_column"],
        ),
    )


def _timeline_applicability(
    store: DuckDBTableStore,
    rule: AuditRule,
    table_names: dict[str, str],
) -> dict[str, Any]:
    assert rule.timeline is not None
    spec = rule.timeline
    table = quote_identifier(table_names[spec.source_id])
    event = quote_identifier(spec.event_field)
    literals = ", ".join(
        "'" + value.replace("'", "''") + "'" for value in spec.expected_order
    )
    total_rows, matched_rows = store.connection.execute(
        f"SELECT COUNT(*), COUNT(*) FILTER ("
        f"WHERE CAST({event} AS VARCHAR) IN ({literals})) FROM {table}"
    ).fetchone()
    observed_rows = store.connection.execute(
        f"SELECT CAST({event} AS VARCHAR), COUNT(*) AS rows_count FROM {table} "
        f"WHERE {event} IS NOT NULL GROUP BY 1 ORDER BY rows_count DESC, 1 LIMIT 50"
    ).fetchall()
    total = int(total_rows)
    matched = int(matched_rows)
    coverage = matched / total if total else 1.0
    if total and matched == 0:
        status = "INCOMPATIBLE"
    elif coverage < 1.0:
        status = "PARTIAL"
    else:
        status = "APPLICABLE"
    expected = set(spec.expected_order)
    return {
        "rule_id": rule.rule_id,
        "rule_kind": rule.kind.value,
        "source_id": spec.source_id,
        "column": spec.event_field,
        "status": status,
        "total_rows": total,
        "matched_rows": matched,
        "row_coverage": round(coverage, 6),
        "expected_values": list(spec.expected_order),
        "observed_values": [
            {"value": value, "rows_count": int(count), "mapped": value in expected}
            for value, count in observed_rows
        ],
        "reason": (
            "No observed values match the rule vocabulary."
            if status == "INCOMPATIBLE"
            else (
                "Some observed values are absent from the rule vocabulary."
                if status == "PARTIAL"
                else "Observed values are covered by the rule vocabulary."
            )
        ),
    }


def analyze_data_dependencies(
    store: DuckDBTableStore,
    table_names: dict[str, str],
    profiles: list[DataProfile],
    rules: tuple[AuditRule, ...],
) -> dict[str, Any]:
    """Infer cross-source links and test declarative vocabularies against data."""

    applicability = []
    for rule in rules:
        if rule.kind == RuleKind.TIMELINE and rule.timeline is not None:
            if rule.timeline.source_id in table_names:
                applicability.append(
                    _timeline_applicability(store, rule, table_names)
                )
    return {
        "schema_version": "1.0",
        "inferred_relationships": _infer_column_dependencies(store, profiles),
        "rule_applicability": applicability,
        "limitations": [
            "Relationships are hypotheses inferred from names and sampled exact values; they are not declared foreign keys.",
            "Semantic mappings between differently encoded values require documentary or code evidence.",
        ],
    }
