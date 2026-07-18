from datetime import datetime, timezone
from types import SimpleNamespace

from audit_insight_agent import audit_rag
from audit_insight_agent.models import (
    AgentRunResult,
    ApplicationSettings,
    CandidateFinding,
    DocumentChunk,
    EvidenceReference,
    RunStatus,
    SearchResult,
    Severity,
    SourceCatalog,
    SourceConfig,
)
from audit_insight_agent.report_generator import write_run_outputs


def test_completed_run_is_grounded_with_document_chunks(tmp_path, monkeypatch):
    document = tmp_path / "policy.txt"
    document.write_text("The balance must be reconciled daily.", encoding="utf-8")
    package = SimpleNamespace(
        source_config_path=tmp_path / "sources.yaml",
        sources=SourceCatalog(
            sources=[
                SourceConfig(
                    source_id="policy",
                    source_type="document",
                    location=str(document),
                )
            ]
        ),
    )
    finding = CandidateFinding(
        finding_id="FND-TEST",
        check_id="CHECK-1",
        title="Balance mismatch",
        summary="Values differ",
        issue_type="reconciliation",
        severity=Severity.HIGH,
        root_cause="Unknown",
        criterion="Balances must agree",
        risk="Incorrect reporting",
        confidence=1.0,
        evidence=[
            EvidenceReference(
                source_name="table",
                description="Calculated mismatch",
            )
        ],
    )
    now = datetime.now(timezone.utc)
    result = AgentRunResult(
        run_id="RUN-RAG-TEST",
        status=RunStatus.COMPLETED,
        started_at=now,
        completed_at=now,
        agent_version="test",
        data_root=str(tmp_path),
        data_sources=[],
        auditor_query="Check balances",
        findings=[finding],
    )
    paths = write_run_outputs(result, tmp_path / result.run_id)

    class FakeClient:
        def collection_exists(self, _collection):
            return False

    class FakeRetriever:
        def __init__(self, _client, _embedder, _collection):
            pass

        def index(self, chunks):
            return len(chunks)

        def search(self, _query, limit=3):
            assert limit == 3
            return [
                SearchResult(
                    chunk=DocumentChunk(
                        chunk_id="chunk-1",
                        source_id="policy",
                        text="The balance must be reconciled daily.",
                        chunk_index=0,
                        start_char=0,
                        end_char=38,
                    ),
                    score=0.91,
                )
            ]

    monkeypatch.setattr(audit_rag, "create_qdrant_client", lambda **_kwargs: FakeClient())
    monkeypatch.setattr(audit_rag, "BgeM3Embedder", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(audit_rag, "QdrantRetriever", FakeRetriever)

    grounded, updated_paths = audit_rag.ground_audit_with_documents(
        result=result,
        paths=paths,
        package=package,
        settings=ApplicationSettings(),
        project_root=tmp_path,
        auditor_query="Check balances",
    )

    assert "rag_grounded" in grounded.findings[0].tags
    assert len(grounded.findings[0].evidence) == 2
    assert grounded.metrics["rag"]["grounded_rule_groups"] == 1
    assert updated_paths["rag_context"].is_file()
