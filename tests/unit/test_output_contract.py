from datetime import (
    datetime,
    timezone,
)

from audit_insight_agent.data_loader import (
    discover_data_sources,
)
from audit_insight_agent.models import (
    AgentRunResult,
    RunStatus,
)
from audit_insight_agent.report_generator import (
    write_run_outputs,
)

"""Контрактные тесты выходной модели агента.

TODO: после реализации AgentResponse зафиксировать обязательные поля,
версию схемы, сериализацию и обратную совместимость выходного JSON.
"""



def test_private_data_is_not_discovered(
    tmp_path,
):

    public_dir = (
        tmp_path
        / "error_injected"
    )

    private_dir = (
        public_dir
        / "private"
    )

    public_dir.mkdir()
    private_dir.mkdir()

    (
        public_dir
        / "public.csv"
    ).write_text(
        "id,value\n1,100\n",
        encoding="utf-8",
    )

    (
        private_dir
        / "ground_truth_private.csv"
    ).write_text(
        "answer\nsecret\n",
        encoding="utf-8",
    )

    sources = discover_data_sources(
        public_dir
    )

    paths = {
        source.relative_path
        for source in sources
    }

    assert "public.csv" in paths

    assert (
        "private/"
        "ground_truth_private.csv"
        not in paths
    )


def test_agent_result_can_be_saved(
    tmp_path,
):

    now = datetime.now(
        timezone.utc
    )

    result = AgentRunResult(
        run_id="RUN-TEST-001",
        status=RunStatus.COMPLETED,
        started_at=now,
        completed_at=now,
        agent_version="0.1.0",
        data_root="/tmp/public",
        data_sources=[],
        findings=[],
        execution_errors=[],
        metrics={
            "findings_count": 0,
        },
    )

    paths = write_run_outputs(
        result=result,
        output_dir=tmp_path,
    )

    saved_result = (
        AgentRunResult
        .model_validate_json(
            paths[
                "candidate_findings"
            ].read_text(
                encoding="utf-8",
            )
        )
    )

    assert (
        saved_result.run_id
        == "RUN-TEST-001"
    )

    assert (
        paths["report"].exists()
    )

    assert (
        paths["run_manifest"].exists()
    )
