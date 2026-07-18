"""BGE-M3 embeddings and Qdrant-backed document retrieval."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from .models import DocumentChunk, SearchResult


class Embedder(Protocol):
    @property
    def dimension(self) -> int: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class BgeM3Embedder:
    """Lazy SentenceTransformers adapter for the multilingual BGE-M3 model."""

    def __init__(
        self,
        model_name: str = "BAAI/bge-m3",
        device: str | None = None,
        batch_size: int = 16,
    ) -> None:
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "BGE-M3 embeddings require the 'sentence-transformers' package"
            ) from error
        self.model = SentenceTransformer(model_name, device=device)
        self.batch_size = batch_size

    @property
    def dimension(self) -> int:
        dimension = self.model.get_sentence_embedding_dimension()
        if dimension is None:
            raise RuntimeError("Embedding model did not report its vector dimension")
        return int(dimension)

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        vectors = self.model.encode(
            list(texts),
            batch_size=self.batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return vectors.tolist()

    def embed_query(self, text: str) -> list[float]:
        return self.embed_documents([text])[0]


def create_qdrant_client(
    url: str | None = None,
    api_key: str | None = None,
    path: str | None = None,
) -> Any:
    """Create either a server client or a local persistent/in-memory client."""

    try:
        from qdrant_client import QdrantClient
    except ImportError as error:
        raise RuntimeError("Vector search requires the 'qdrant-client' package") from error
    if url:
        return QdrantClient(url=url, api_key=api_key)
    return QdrantClient(path=path or ":memory:")


class QdrantRetriever:
    """Index and retrieve traceable chunks using an injected embedding backend."""

    def __init__(self, client: Any, embedder: Embedder, collection: str) -> None:
        self.client = client
        self.embedder = embedder
        self.collection = collection

    def ensure_collection(self) -> None:
        from qdrant_client.models import Distance, VectorParams

        if not self.client.collection_exists(self.collection):
            self.client.create_collection(
                collection_name=self.collection,
                vectors_config=VectorParams(
                    size=self.embedder.dimension,
                    distance=Distance.COSINE,
                ),
            )

    def index(self, chunks: Sequence[DocumentChunk], batch_size: int = 64) -> int:
        from qdrant_client.models import PointStruct

        if not chunks:
            return 0
        self.ensure_collection()
        indexed = 0
        for offset in range(0, len(chunks), batch_size):
            batch = chunks[offset : offset + batch_size]
            vectors = self.embedder.embed_documents([chunk.text for chunk in batch])
            points = [
                PointStruct(
                    id=chunk.chunk_id,
                    vector=vector,
                    payload={"chunk": chunk.model_dump(mode="json")},
                )
                for chunk, vector in zip(batch, vectors, strict=True)
            ]
            self.client.upsert(
                collection_name=self.collection,
                points=points,
                wait=True,
            )
            indexed += len(points)
        return indexed

    def search(
        self,
        query: str,
        limit: int = 5,
        metadata_filter: dict[str, Any] | None = None,
    ) -> list[SearchResult]:
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        if limit <= 0:
            raise ValueError("limit must be positive")
        self.ensure_collection()
        query_filter = None
        if metadata_filter:
            query_filter = Filter(
                must=[
                    FieldCondition(
                        key=f"chunk.metadata.{key}",
                        match=MatchValue(value=value),
                    )
                    for key, value in metadata_filter.items()
                ]
            )
        response = self.client.query_points(
            collection_name=self.collection,
            query=self.embedder.embed_query(query),
            query_filter=query_filter,
            limit=limit,
            with_payload=True,
        )
        points = response.points
        return [
            SearchResult(
                chunk=DocumentChunk.model_validate(point.payload["chunk"]),
                score=float(point.score),
            )
            for point in points
        ]
