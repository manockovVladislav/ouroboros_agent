"""Generic relationship-driven reconciliation SQL compiler."""
from __future__ import annotations

from .data_loader import quote_identifier
from .models import AuditRule, RelationshipConfig


def compile_reconciliation(
    rule: AuditRule,
    relationship: RelationshipConfig,
    table_names: dict[str, str],
) -> str:
    spec = rule.reconciliation
    if spec is None:
        raise ValueError("Missing reconciliation specification")
    left_table = quote_identifier(table_names[relationship.left_source])
    right_table = quote_identifier(table_names[relationship.right_source])
    left_keys = [quote_identifier(key.left) for key in relationship.keys]
    right_keys = [quote_identifier(key.right) for key in relationship.keys]
    key_aliases = [f"key_{index}" for index in range(len(relationship.keys))]
    left_select = ", ".join(
        f"{column} AS {quote_identifier(alias)}"
        for column, alias in zip(left_keys, key_aliases, strict=True)
    )
    right_select = ", ".join(
        f"{column} AS {quote_identifier(alias)}"
        for column, alias in zip(right_keys, key_aliases, strict=True)
    )
    left_groups = ", ".join(left_keys)
    right_groups = ", ".join(right_keys)
    join = " AND ".join(
        f"l.{quote_identifier(alias)} IS NOT DISTINCT FROM r.{quote_identifier(alias)}"
        for alias in key_aliases
    )
    output_keys = ", ".join(
        f"COALESCE(l.{quote_identifier(alias)}, r.{quote_identifier(alias)}) AS {quote_identifier(alias)}"
        for alias in key_aliases
    )
    object_parts = ", ".join(
        f"CAST(COALESCE(l.{quote_identifier(alias)}, r.{quote_identifier(alias)}) AS VARCHAR)"
        for alias in key_aliases
    )
    aggregate = spec.aggregation.upper()
    left_value = quote_identifier(spec.left_value)
    right_value = quote_identifier(spec.right_value)
    return (
        f"WITH left_data AS (SELECT {left_select}, {aggregate}({left_value}) AS left_value "
        f"FROM {left_table} GROUP BY {left_groups}), "
        f"right_data AS (SELECT {right_select}, {aggregate}({right_value}) AS right_value "
        f"FROM {right_table} GROUP BY {right_groups}) "
        f"SELECT concat_ws('|', {object_parts}) AS object_id, {output_keys}, "
        "l.left_value, r.right_value, "
        "COALESCE(l.left_value, 0) - COALESCE(r.right_value, 0) AS difference "
        f"FROM left_data l FULL OUTER JOIN right_data r ON {join} "
        f"WHERE ABS(COALESCE(l.left_value, 0) - COALESCE(r.right_value, 0)) > {float(spec.tolerance)}"
    )


def run_reconciliations(*_args, **_kwargs):
    """Legacy hook retained for API compatibility; case execution uses the compiler."""
    return []
