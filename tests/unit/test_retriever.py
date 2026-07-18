from __future__ import annotations

from audit_insight_agent.document_loader import chunk_text
from audit_insight_agent.retriever import QdrantRetriever, create_qdrant_client


class TinyEmbedder:
    @property
    def dimension(self):
        return 3

    def embed_documents(self, texts):
        return [self.embed_query(text) for text in texts]

    def embed_query(self, text):
        lowered = text.lower()
        return [
            float("audit" in lowered),
            float("policy" in lowered),
            float("risk" in lowered),
        ]


def test_qdrant_indexes_and_returns_traceable_chunks():
    chunks = chunk_text(
        "requirements",
        "Audit policy and controls",
        metadata={"kind": "policy"},
    )
    retriever = QdrantRetriever(
        create_qdrant_client(), TinyEmbedder(), "test_requirements"
    )

    assert retriever.index(chunks) == 1
    results = retriever.search("audit policy", metadata_filter={"kind": "policy"})

    assert results[0].chunk.source_id == "requirements"
    assert results[0].chunk.chunk_id == chunks[0].chunk_id
