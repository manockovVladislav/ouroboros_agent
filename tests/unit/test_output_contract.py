from datetime import (
    datetime,
    timezone,
)

from audit_insight_agent.data_loader import (
    discover_data_sources,
)
from audit_insight_agent.models import (
    AgentRunResult,
    CandidateFinding,
    DataSource,
    EvidenceReference,
    RunStatus,
    Severity,
)
from audit_insight_agent.finding_builder import review_findings_and_build_plan
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


def test_finding_is_challenged_and_plan_keeps_exact_source_location():
    finding = CandidateFinding(
        finding_id="FND-1",
        check_id="CHECK-1",
        title="Contradiction",
        summary="Values disagree",
        issue_type="contradiction",
        severity=Severity.HIGH,
        criterion="Sources must agree",
        risk="Reporting can be distorted",
        root_cause="Unknown",
        confidence=0.9,
        evidence=[
            EvidenceReference(
                evidence_id="EVD-1",
                checksum="abc",
                source_name="ledger",
                object_id="row-1",
                description="Mismatch row",
                query="SELECT * FROM ledger WHERE id = 1",
            )
        ],
    )
    reviews, plan = review_findings_and_build_plan(
        [finding],
        [],
        [
            DataSource(
                source_id="ledger",
                relative_path="/data/ledger.csv",
                file_format="csv",
                size_bytes=10,
            )
        ],
    )
    assert reviews[0].verdict == "CONFIRMED"
    assert plan[0].status == "CONFIRMED_ISSUE"
    assert plan[0].source_locations == ["/data/ledger.csv"]

    uncertain = finding.model_copy(update={"finding_id": "FND-2", "confidence": 0.6})
    uncertain_reviews, uncertain_plan = review_findings_and_build_plan(
        [uncertain], []
    )
    assert uncertain_reviews[0].verdict == "REQUIRES_VALIDATION"
    assert uncertain_plan[0].status == "POTENTIAL_RISK"
