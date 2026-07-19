"""Generic lineage and cross-entity semantic consistency analysis."""
from __future__ import annotations

import hashlib
import re
from collections import deque
from itertools import combinations
from typing import Any

import pandas as pd

from .data_loader import DuckDBTableStore, quote_identifier
from .evidence_store import EvidenceStore, build_observation_evidence_record
from .finding_builder import build_finding
from .models import (
    AuditPlanItem,
    CandidateFinding,
    DataProfile,
    EvidenceReference,
    FindingReview,
    Severity,
)


_TARGET_MARKERS = {"destination", "target", "to"}
_NON_SEMANTIC_PARTS = {
    "batch",
    "created",
    "date",
    "description",
    "id",
    "name",
    "status",
    "time",
    "timestamp",
    "ts",
    "valid",
    "version",
}
_SEMANTIC_PARTS = {
    "asset",
    "business",
    "category",
    "class",
    "classification",
    "country",
    "currency",
    "entity",
    "operation",
    "portfolio",
    "product",
    "region",
    "type",
    "unit",
}


def _parts(name: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", name.casefold()))


def _is_target_key(column: str) -> bool:
    parts = _parts(column)
    return column.casefold().endswith("_id") and bool(parts & _TARGET_MARKERS)


def _is_semantic_attribute(name: str, distinct_count: int) -> bool:
    parts = _parts(name)
    return (
        bool(parts)
        and not name.casefold().endswith("_id")
        and not parts & _NON_SEMANTIC_PARTS
        and bool(parts & _SEMANTIC_PARTS)
        and 1 <= distinct_count <= 500
    )


def _is_mapping_definition(profile: DataProfile) -> bool:
    names = {column.name.casefold() for column in profile.columns}
    has_rule_key = any(
        name == "rule_id" or name.endswith("_rule_id") for name in names
    )
    has_lifecycle_metadata = bool(
        names
        & {
            "approved_by",
            "description",
            "valid_from",
            "valid_to",
        }
    )
    return has_rule_key and has_lifecycle_metadata


def _profile_index(
    profiles: list[DataProfile],
) -> dict[str, DataProfile]:
    return {profile.source_id: profile for profile in profiles}


def _join_roles(
    dependency: dict[str, Any],
) -> tuple[str, str, str, str] | None:
    left_column = str(dependency["left_column"])
    right_column = str(dependency["right_column"])
    left_target = _is_target_key(left_column)
    right_target = _is_target_key(right_column)
    if left_target == right_target:
        return None
    relationship = str(dependency.get("relationship") or "")
    if left_target:
        if relationship not in {"many_to_one", "one_to_one"}:
            return None
        return (
            str(dependency["left_source"]),
            left_column,
            str(dependency["right_source"]),
            right_column,
        )
    if relationship not in {"one_to_many", "one_to_one"}:
        return None
    return (
        str(dependency["right_source"]),
        right_column,
        str(dependency["left_source"]),
        left_column,
    )


def _identifier_columns(profile: DataProfile) -> list[str]:
    return [
        column.name
        for column in profile.columns
        if column.name.casefold().endswith("_id")
    ][:4]


def _contextual_target_outliers(
    store: DuckDBTableStore,
    mapping: DataProfile,
    mapping_key: str,
    reference: DataProfile,
    reference_key: str,
    dependency_confidence: float,
) -> list[dict[str, Any]]:
    """Find rare target classifications within otherwise stable mapping contexts."""

    if not _is_mapping_definition(mapping):
        return []
    context_columns = [
        column.name
        for column in mapping.columns
        if _is_semantic_attribute(column.name, column.distinct_count)
    ][:8]
    target_attributes = [
        column.name
        for column in reference.columns
        if _is_semantic_attribute(column.name, column.distinct_count)
    ][:8]
    if not context_columns or not target_attributes:
        return []
    mapping_table = quote_identifier(mapping.table_name)
    reference_table = quote_identifier(reference.table_name)
    mapping_key_sql = quote_identifier(mapping_key)
    reference_key_sql = quote_identifier(reference_key)
    identifiers = _identifier_columns(mapping)
    select_parts = [
        f"m.{quote_identifier(name)} AS {quote_identifier(name)}"
        for name in dict.fromkeys([*identifiers, mapping_key])
    ]
    select_parts.append(
        f"m.{mapping_key_sql} AS {quote_identifier('mapping_target_key')}"
    )
    select_parts.extend(
        f"m.{quote_identifier(name)} AS {quote_identifier('context_' + name)}"
        for name in context_columns
    )
    select_parts.extend(
        f"r.{quote_identifier(name)} AS {quote_identifier('target_' + name)}"
        for name in target_attributes
    )
    join = (
        f"CAST(m.{mapping_key_sql} AS VARCHAR) "
        f"= CAST(r.{reference_key_sql} AS VARCHAR)"
    )
    base_query = (
        f"SELECT {', '.join(select_parts)} FROM {mapping_table} m "
        f"JOIN {reference_table} r ON {join}"
    )
    frame = store.connection.execute(base_query).fetchdf()
    if frame.empty:
        return []

    context_sets = [(name,) for name in context_columns]
    context_sets.extend(combinations(context_columns, 2))
    candidates: dict[tuple[str, ...], dict[str, Any]] = {}
    for target_attribute in target_attributes:
        target_column = "target_" + target_attribute
        if target_column not in frame.columns:
            continue
        for context_fields in context_sets:
            context_aliases = ["context_" + name for name in context_fields]
            grouping_key: str | list[str] = (
                context_aliases[0]
                if len(context_aliases) == 1
                else context_aliases
            )
            grouped = frame.groupby(grouping_key, dropna=False, sort=False)
            for context_values, group in grouped:
                if len(group) < 5:
                    continue
                counts = group[target_column].value_counts(dropna=False)
                if len(counts) < 2:
                    continue
                expected_value = counts.index[0]
                expected_count = int(counts.iloc[0])
                dominant_ratio = expected_count / len(group)
                if dominant_ratio < 0.9:
                    continue
                if _is_null(expected_value):
                    minority = group[group[target_column].notna()]
                else:
                    minority = group[
                        group[target_column].isna()
                        | (group[target_column] != expected_value)
                    ]
                if minority.empty:
                    continue
                values_tuple = (
                    context_values
                    if isinstance(context_values, tuple)
                    else (context_values,)
                )
                context = {
                    field: _json_value(value)
                    for field, value in zip(
                        context_fields, values_tuple, strict=True
                    )
                }
                context_predicates = [
                    f"m.{quote_identifier(field)} IS NOT DISTINCT FROM "
                    f"{_sql_literal(value)}"
                    for field, value in zip(
                        context_fields, values_tuple, strict=True
                    )
                ]
                outlier_predicate = (
                    f"r.{quote_identifier(target_attribute)} IS DISTINCT FROM "
                    f"{_sql_literal(expected_value)}"
                )
                reproduction_query = (
                    f"{base_query} WHERE "
                    + " AND ".join([*context_predicates, outlier_predicate])
                )
                identity_columns = [
                    name for name in identifiers if name in minority.columns
                ]
                identity_values = tuple(
                    sorted(
                        "|".join(
                            str(row[name]) for name in identity_columns
                        )
                        for _, row in minority.iterrows()
                    )
                )
                deduplication_key = identity_values
                confidence = min(
                    0.99,
                    float(dependency_confidence) * dominant_ratio,
                )
                hypothesis = {
                    "hypothesis_id": "BIZ-" + hashlib.sha256(
                        (
                            f"{mapping.source_id}|{mapping_key}|"
                            f"{reference.source_id}|{reference_key}|"
                            f"{target_attribute}|{context}|{identity_values}"
                        ).encode("utf-8")
                    ).hexdigest()[:16].upper(),
                    "kind": "contextual_target_outlier",
                    "status": "POTENTIAL_RISK",
                    "mapping_source": mapping.source_id,
                    "mapping_target_key": mapping_key,
                    "reference_source": reference.source_id,
                    "reference_key": reference_key,
                    "attribute": target_attribute,
                    "outlier_attributes": {
                        target_attribute: _json_value(expected_value)
                    },
                    "context": context,
                    "expected_reference_value": _json_value(expected_value),
                    "joined_rows": int(len(group)),
                    "mismatch_rows": int(len(minority)),
                    "mismatch_ratio": round(len(minority) / len(group), 6),
                    "pattern": "OUTLIER",
                    "confidence": round(confidence, 4),
                    "reproduction_query": reproduction_query,
                    "samples": [
                        {
                            str(key): _json_value(value)
                            for key, value in row.items()
                        }
                        for row in minority.head(20).to_dict(orient="records")
                    ],
                    "limitations": [
                        "The dominant route is inferred from peer mappings, not from a normative document.",
                        "A rare route may be a valid exception and requires documentary or code evidence.",
                    ],
                }
                existing = candidates.get(deduplication_key)
                if existing is None:
                    candidates[deduplication_key] = hypothesis
                    continue
                existing["outlier_attributes"][target_attribute] = _json_value(
                    expected_value
                )
                existing["attribute"] = ", ".join(
                    sorted(existing["outlier_attributes"])
                )
                expected_values = existing["outlier_attributes"]
                existing["expected_reference_value"] = (
                    next(iter(expected_values.values()))
                    if len(expected_values) == 1
                    else expected_values
                )
                if (len(context_fields), confidence) > (
                    len(existing["context"]),
                    existing["confidence"],
                ):
                    existing["context"] = context
                    existing["joined_rows"] = int(len(group))
                    existing["mismatch_rows"] = int(len(minority))
                    existing["mismatch_ratio"] = round(
                        len(minority) / len(group), 6
                    )
                    existing["confidence"] = round(confidence, 4)
                    existing["reproduction_query"] = reproduction_query
    return list(candidates.values())


def _is_null(value: Any) -> bool:
    """Avoid leaking pandas scalar types into serialized analysis artifacts."""

    result = pd.isna(value)
    return bool(result) if not hasattr(result, "__len__") else False


def _json_value(value: Any) -> Any:
    if _is_null(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _sql_literal(value: Any) -> str:
    normalized = _json_value(value)
    if normalized is None:
        return "NULL"
    if isinstance(normalized, bool):
        return "TRUE" if normalized else "FALSE"
    if isinstance(normalized, (int, float)):
        return str(normalized)
    return "'" + str(normalized).replace("'", "''") + "'"


def _lineage_edges(
    inferred_relationships: list[dict[str, Any]],
    declared_relationships: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    edges = []
    for relationship in declared_relationships:
        for key in relationship.get("keys", []):
            edges.append(
                {
                    "left_source": relationship["left_source"],
                    "left_column": key["left"],
                    "right_source": relationship["right_source"],
                    "right_column": key["right"],
                    "confidence": 1.0,
                    "basis": "declared_relationship",
                    "relationship_id": relationship.get("relationship_id"),
                }
            )
    for relationship in inferred_relationships:
        if (
            relationship.get("dependency_type") != "join_key_candidate"
            or float(relationship.get("confidence") or 0.0) < 0.8
        ):
            continue
        edges.append(
            {
                "left_source": relationship["left_source"],
                "left_column": relationship["left_column"],
                "right_source": relationship["right_source"],
                "right_column": relationship["right_column"],
                "confidence": relationship["confidence"],
                "basis": "inferred_join_key",
                "relationship": relationship.get("relationship"),
            }
        )
    unique = {}
    for edge in edges:
        identity = (
            edge["left_source"],
            edge["left_column"],
            edge["right_source"],
            edge["right_column"],
        )
        existing = unique.get(identity)
        if existing is None or edge["confidence"] > existing["confidence"]:
            unique[identity] = edge
    return sorted(
        unique.values(),
        key=lambda item: (
            -item["confidence"],
            item["left_source"],
            item["left_column"],
            item["right_source"],
            item["right_column"],
        ),
    )


def _impact_paths(
    seed_source: str,
    edges: list[dict[str, Any]],
    *,
    max_depth: int = 4,
    max_paths: int = 30,
) -> list[dict[str, Any]]:
    adjacency: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for edge in edges:
        left = str(edge["left_source"])
        right = str(edge["right_source"])
        adjacency.setdefault(left, []).append((right, edge))
        adjacency.setdefault(right, []).append((left, edge))
    queue = deque([(seed_source, [seed_source], [])])
    shortest_depth = {seed_source: 0}
    paths = []
    emitted_sequences: set[tuple[str, ...]] = set()
    while queue and len(paths) < max_paths:
        current, sources, path_edges = queue.popleft()
        depth = len(path_edges)
        if depth >= max_depth:
            continue
        for neighbor, edge in adjacency.get(current, []):
            if neighbor in sources:
                continue
            next_sources = [*sources, neighbor]
            next_edges = [*path_edges, edge]
            next_depth = depth + 1
            previous_depth = shortest_depth.get(neighbor)
            if previous_depth is not None and previous_depth < next_depth:
                continue
            shortest_depth[neighbor] = next_depth
            sequence = tuple(next_sources)
            if sequence in emitted_sequences:
                continue
            emitted_sequences.add(sequence)
            paths.append(
                {
                    "sources": next_sources,
                    "edges": next_edges,
                    "status": "CANDIDATE_PATH",
                    "limitation": (
                        "The source-level path does not prove that the same business "
                        "object propagated through every edge."
                    ),
                }
            )
            queue.append((neighbor, next_sources, next_edges))
    return paths


def analyze_business_logic(
    store: DuckDBTableStore,
    profiles: list[DataProfile],
    dependency_analysis: dict[str, Any],
    declared_relationships: list[dict[str, Any]],
) -> dict[str, Any]:
    """Find semantic mapping inconsistencies and candidate downstream paths."""

    profile_by_source = _profile_index(profiles)
    inferred = list(dependency_analysis.get("inferred_relationships") or [])
    lineage_edges = _lineage_edges(inferred, declared_relationships)
    hypotheses = []
    seen = set()
    for dependency in inferred:
        if dependency.get("dependency_type") != "join_key_candidate":
            continue
        roles = _join_roles(dependency)
        if roles is None:
            continue
        mapping_source, mapping_key, reference_source, reference_key = roles
        mapping = profile_by_source.get(mapping_source)
        reference = profile_by_source.get(reference_source)
        if mapping is None or reference is None:
            continue
        identity = (
            mapping_source,
            mapping_key,
            reference_source,
            reference_key,
        )
        if identity in seen:
            continue
        seen.add(identity)
        contextual_hypotheses = _contextual_target_outliers(
            store,
            mapping,
            mapping_key,
            reference,
            reference_key,
            float(dependency.get("confidence") or 0.0),
        )
        for hypothesis in contextual_hypotheses:
            hypothesis["candidate_impact_paths"] = _impact_paths(
                mapping_source,
                lineage_edges,
            )
            hypotheses.append(hypothesis)
    hypotheses.sort(
        key=lambda item: (
            -item["confidence"],
            item["mismatch_ratio"],
            item["hypothesis_id"],
        )
    )
    return {
        "schema_version": "1.0",
        "lineage_edges": lineage_edges,
        "semantic_hypotheses": hypotheses,
        "limitations": [
            "Automatically inferred lineage is a navigation aid, not proof of causality.",
            "Business conclusions require a declared criterion or independent documentary evidence.",
        ],
    }


def _hypothesis_object_id(hypothesis: dict[str, Any], sample: dict[str, Any]) -> str:
    preferred = [
        key
        for key in sample
        if key == "rule_id" or key.endswith("_rule_id")
    ]
    preferred.extend(
        key
        for key in ("mapping_target_key", "target_id", "object_id")
        if key in sample
    )
    for key in preferred:
        value = sample.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return str(hypothesis["hypothesis_id"])


def _actual_target_attributes(
    hypothesis: dict[str, Any], sample: dict[str, Any]
) -> dict[str, Any]:
    attributes = hypothesis.get("outlier_attributes") or {}
    return {
        str(attribute): sample.get("target_" + str(attribute))
        for attribute in attributes
    }


def investigate_business_hypotheses(
    *,
    store: DuckDBTableStore,
    analysis: dict[str, Any],
    evidence_store: EvidenceStore,
    run_id: str,
    minimum_confidence: float = 0.8,
) -> list[CandidateFinding]:
    """Reproduce material hypotheses and promote them into reviewable findings."""

    promoted: list[CandidateFinding] = []
    for hypothesis in analysis.get("semantic_hypotheses", []):
        confidence = float(hypothesis.get("confidence") or 0.0)
        query = str(hypothesis.get("reproduction_query") or "").strip()
        if confidence < minimum_confidence or not query:
            hypothesis["investigation"] = {
                "status": "BELOW_MATERIALITY_THRESHOLD",
                "minimum_confidence": minimum_confidence,
            }
            continue
        try:
            reproduced = store.query(
                f"SELECT * FROM ({query}) AS business_hypothesis_matches LIMIT 101"
            )
        except Exception as error:
            hypothesis["investigation"] = {
                "status": "ERROR",
                "error": f"{type(error).__name__}: {error}",
            }
            continue
        if reproduced.empty:
            hypothesis["investigation"] = {
                "status": "NOT_REPRODUCED",
                "rows": 0,
            }
            continue

        normalized_rows = [
            {str(key): _json_value(value) for key, value in row.items()}
            for row in reproduced.head(100).to_dict(orient="records")
        ]
        sample = normalized_rows[0]
        object_id = _hypothesis_object_id(hypothesis, sample)
        sources = [
            str(hypothesis["mapping_source"]),
            str(hypothesis["reference_source"]),
        ]
        check_id = "BUSINESS_SEMANTIC_CONSTRAINT"
        evidence = build_observation_evidence_record(
            run_id=run_id,
            check_id=check_id,
            source_ids=sources,
            object_id=object_id,
            query=query,
            result={
                "hypothesis_id": hypothesis["hypothesis_id"],
                "reproduced_rows": len(reproduced),
                "sample": sample,
            },
        )
        evidence_store.save(evidence)
        expected = hypothesis.get("expected_reference_value")
        actual = _actual_target_attributes(hypothesis, sample)
        context = dict(hypothesis.get("context") or {})
        finding = build_finding(
            check_id=check_id,
            issue_type="semantic_target_mismatch",
            primary_object_id=object_id,
            title=f"Целевая сущность маршрута не соответствует его контексту: {object_id}",
            summary=(
                f"Воспроизведён нетипичный маршрут: для контекста {context!r} "
                f"ожидаемые свойства цели {expected!r}, фактические — {actual!r}."
            ),
            root_cause=(
                "Определение маршрута направляет операцию на сущность, свойства "
                "которой отличаются от устойчивого контекста сопоставимых маршрутов."
            ),
            criterion=(
                f"Для контекста {context!r} целевая сущность должна соответствовать "
                f"свойствам {expected!r}; нормативное основание проверяется отдельно."
            ),
            risk=(
                "Операция может быть отражена в неверном бизнес-классе и повлиять "
                "на связанные остатки, отчётность или автоматические решения."
            ),
            severity=Severity.HIGH if confidence >= 0.9 else Severity.MEDIUM,
            confidence=confidence,
            evidence=[
                EvidenceReference(
                    evidence_id=evidence.evidence_id,
                    checksum=evidence.checksum,
                    source_name=", ".join(sources),
                    object_id=object_id,
                    description=(
                        "Повторный read-only запрос воспроизвёл строку с "
                        "семантически нетипичной целевой сущностью."
                    ),
                    fields={
                        "hypothesis_id": hypothesis["hypothesis_id"],
                        "context": context,
                        "expected_target_attributes": expected,
                        "actual_target_attributes": actual,
                        "reproduced_rows": len(reproduced),
                        "sample": sample,
                    },
                    query=query,
                )
            ],
            recommendation=(
                "Проверить маршрут по нормативному критерию, затем проследить тот "
                "же бизнес-объект по связанным операциям и измерить последствия."
            ),
            object_id=object_id,
            facts={
                "hypothesis_id": hypothesis["hypothesis_id"],
                "semantic_constraint": {
                    "context": context,
                    "expected": expected,
                    "actual": actual,
                },
                "reproduced_rows": normalized_rows,
                "candidate_impact_paths": hypothesis.get("candidate_impact_paths", []),
            },
            tags=[
                "business_hypothesis",
                "material_business_hypothesis",
                "semantic_constraint",
                "requires_policy_grounding",
            ],
        )
        promoted.append(finding)
        hypothesis["investigation"] = {
            "status": "PROMOTED_TO_FINDING",
            "rows": len(reproduced),
            "finding_id": finding.finding_id,
            "evidence_id": evidence.evidence_id,
        }
    return promoted


def business_hypothesis_coverage(
    findings: list[CandidateFinding],
    reviews: list[FindingReview],
    expected_hypothesis_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Describe whether every material hypothesis received an explicit verdict."""

    review_by_id = {review.finding_id: review for review in reviews}
    items = []
    for finding in findings:
        if "material_business_hypothesis" not in finding.tags:
            continue
        review = review_by_id.get(finding.finding_id)
        items.append(
            {
                "hypothesis_id": finding.facts.get("hypothesis_id"),
                "finding_id": finding.finding_id,
                "verdict": review.verdict if review else "MISSING_REVIEW",
            }
        )
    represented = {str(item["hypothesis_id"]) for item in items}
    expected = set(expected_hypothesis_ids or represented)
    for hypothesis_id in sorted(expected - represented):
        items.append(
            {
                "hypothesis_id": hypothesis_id,
                "finding_id": None,
                "verdict": "MISSING_FINDING",
            }
        )
    unresolved = [
        item
        for item in items
        if item["verdict"]
        in {"MISSING_FINDING", "MISSING_REVIEW", "REQUIRES_VALIDATION"}
    ]
    return {
        "expected_hypothesis_ids": sorted(expected),
        "material_hypotheses": items,
        "material_hypotheses_count": len(items),
        "unresolved_count": len(unresolved),
        "complete": all(
            item["verdict"] not in {"MISSING_FINDING", "MISSING_REVIEW"}
            for item in items
        ),
    }


def business_hypothesis_plan_items(
    analysis: dict[str, Any],
    source_locations: dict[str, str] | None = None,
) -> list[AuditPlanItem]:
    """Expose semantic hypotheses as review work, never as confirmed findings."""

    locations = source_locations or {}
    result = []
    for hypothesis in analysis.get("semantic_hypotheses", []):
        if (hypothesis.get("investigation") or {}).get("finding_id"):
            continue
        mapping_source = str(hypothesis["mapping_source"])
        reference_source = str(hypothesis["reference_source"])
        sources = [mapping_source, reference_source]
        result.append(
            AuditPlanItem(
                plan_id="PLAN-" + hypothesis["hypothesis_id"],
                priority=(
                    Severity.HIGH
                    if hypothesis["pattern"] == "OUTLIER"
                    and float(hypothesis["confidence"]) >= 0.8
                    else Severity.MEDIUM
                ),
                status="POTENTIAL_RISK",
                title=(
                    "Нетипичное значение атрибута "
                    f"{hypothesis['attribute']} целевой сущности"
                ),
                rationale=(
                    f"В {hypothesis['mismatch_rows']} из {hypothesis['joined_rows']} "
                    "сопоставимых маршрутов классификация целевой сущности отличается "
                    f"от доминирующего значения {hypothesis['expected_reference_value']!r}. "
                    "Это гипотеза, а не "
                    "подтверждённое нарушение."
                ),
                sources=sources,
                source_locations=sorted(
                    {locations[source] for source in sources if source in locations}
                ),
                next_steps=[
                    "Проверить совместимость атрибутов по нормативному документу или коду.",
                    "Выполнить reproduction_query и проверить строки-исключения.",
                    "Проследить один и тот же бизнес-объект по candidate_impact_paths.",
                ],
            )
        )
    return result
