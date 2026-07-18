"""Explainable z-score and IQR anomaly query compiler."""
from __future__ import annotations

from .data_loader import quote_identifier
from .models import AuditRule


def compile_anomaly(rule: AuditRule, table_names: dict[str, str]) -> str:
    spec = rule.anomaly
    if spec is None:
        raise ValueError("Missing anomaly specification")
    table = quote_identifier(table_names[spec.source_id])
    field = quote_identifier(spec.field)
    groups = [quote_identifier(item) for item in spec.group_by]
    partition = f"PARTITION BY {', '.join(groups)} " if groups else ""
    if spec.method == "zscore":
        return (
            "WITH scored AS (SELECT *, "
            f"AVG({field}) OVER ({partition}) AS expected_mean, "
            f"STDDEV_POP({field}) OVER ({partition}) AS expected_stddev FROM {table}) "
            "SELECT *, CASE WHEN expected_stddev = 0 THEN 0 "
            f"ELSE ({field} - expected_mean) / expected_stddev END AS anomaly_score "
            "FROM scored WHERE expected_stddev > 0 AND "
            f"ABS(({field} - expected_mean) / expected_stddev) > {float(spec.threshold)}"
        )
    group_clause = f"GROUP BY {', '.join(groups)}" if groups else ""
    join_clause = " AND ".join(f"d.{g} IS NOT DISTINCT FROM s.{g}" for g in groups) or "TRUE"
    selected_groups = (", " + ", ".join(groups)) if groups else ""
    return (
        f"WITH stats AS (SELECT quantile_cont({field}, 0.25) AS q1, "
        f"quantile_cont({field}, 0.75) AS q3{selected_groups} FROM {table} {group_clause}) "
        f"SELECT d.*, s.q1, s.q3, CASE WHEN s.q3 = s.q1 THEN 0 ELSE "
        f"ABS(d.{field} - (s.q1 + s.q3) / 2) / (s.q3 - s.q1) END AS anomaly_score "
        f"FROM {table} d JOIN stats s ON {join_clause} WHERE d.{field} < s.q1 - "
        f"{float(spec.threshold)} * (s.q3 - s.q1) OR d.{field} > s.q3 + "
        f"{float(spec.threshold)} * (s.q3 - s.q1)"
    )
