"""Safe SQL composition and deterministic value normalization."""
from __future__ import annotations

import math
import re
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .data_loader import quote_identifier


FORBIDDEN_SQL = re.compile(
    r"\b(attach|copy|detach|export|import|install|load|pragma|call|create|drop|alter|update|delete|insert)\b|"
    r"\b(read_[a-z]+|csv_scan|parquet_scan|sqlite_scan|postgres_scan|glob|httpfs)\s*\(",
    re.IGNORECASE,
)
SOURCE_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z0-9_.-]+)\s*\}\}")


def render_safe_select(query: str, table_names: dict[str, str], allowed_sources: list[str]) -> str:
    """Render table placeholders and reject SQL capable of external mutation/access."""

    stripped = query.strip()
    if ";" in stripped or "--" in stripped or "/*" in stripped:
        raise ValueError("Rule SQL cannot contain separators or comments")
    if not re.match(r"^(select|with)\b", stripped, re.IGNORECASE):
        raise ValueError("Rule SQL must be a SELECT or WITH query")
    if FORBIDDEN_SQL.search(stripped):
        raise ValueError("Rule SQL contains a forbidden operation")
    allowed = set(allowed_sources)
    placeholders = set(SOURCE_PLACEHOLDER.findall(stripped))
    if not placeholders:
        raise ValueError("Rule SQL must reference a registered {{source_id}} placeholder")

    def replace(match: re.Match[str]) -> str:
        source_id = match.group(1)
        if source_id not in allowed or source_id not in table_names:
            raise ValueError(f"SQL references unavailable source: {source_id}")
        return quote_identifier(table_names[source_id])

    rendered = SOURCE_PLACEHOLDER.sub(replace, stripped)
    if "{{" in rendered or "}}" in rendered:
        raise ValueError("Malformed source placeholder in rule SQL")
    return rendered


def normalize_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    return {str(key): normalize_value(value) for key, value in row.items()}
