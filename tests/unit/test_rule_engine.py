import pytest

from audit_insight_agent.analysis_tools import render_safe_select


def test_rule_sql_only_allows_registered_source_placeholders():
    query = render_safe_select(
        "SELECT * FROM {{arbitrary_source}} WHERE amount > 0",
        {"arbitrary_source": "table_1"},
        ["arbitrary_source"],
    )
    assert 'FROM "table_1"' in query

    with pytest.raises(ValueError):
        render_safe_select(
            "SELECT * FROM read_csv('/tmp/secret.csv')",
            {},
            [],
        )
