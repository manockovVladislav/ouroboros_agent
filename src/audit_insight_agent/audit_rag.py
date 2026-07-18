"""RAG grounding for completed deterministic audit runs."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from .config import resolve_source_location
from .document_loader import DOCUMENT_FORMATS, load_document_chunks
from .models import (
    AgentRunResult,
    ApplicationSettings,
    DataSource,
    EvidenceReference,
    RunStatus,
    SourceConfig,
)
from .report_generator import write_run_outputs
from .retriever import BgeM3Embedder, QdrantRetriever, create_qdrant_client
from .run_logging import RunEventLogger


def _fingerprint(paths: list[Path], model: str, collection: str) -> str:
    state = [
        {
            "path": str(path),
            "size": path.stat().st_size,
            "mtime_ns": path.stat().st_mtime_ns,
        }
        for path in paths
    ]
    payload = json.dumps(
        {"files": state, "model": model, "collection": collection},
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _manifest_path(project_root: Path, collection: str) -> Path:
    safe_collection = "".join(
        character if character.isalnum() or character in "_.-" else "_"
        for character in collection
    )
    return project_root / ".audit_insight" / f"rag-{safe_collection}.json"


def _read_manifest(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json_atomic(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _collect_document_sources(
    package: Any, source_config_path: Path, project_root: Path
) -> list[SourceConfig]:
    configured = [
        source
        for source in package.sources.sources
        if source.enabled and source.source_type == "document"
    ]
    known_paths = {
        resolve_source_location(source, source_config_path) for source in configured
    }
    discovered = []
    for root_name in ("data", "knowledge"):
        root = project_root / root_name
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*")):
            resolved = path.resolve()
            if (
                not path.is_file()
                or path.suffix.lower() not in DOCUMENT_FORMATS
                or resolved in known_paths
            ):
                continue
            relative = resolved.relative_to(project_root).as_posix()
            source_hash = hashlib.sha256(relative.encode("utf-8")).hexdigest()[:16]
            discovered.append(
                SourceConfig(
                    source_id=f"auto_document_{source_hash}",
                    source_type="document",
                    location=str(resolved),
                    metadata={"origin": root_name, "relative_path": relative},
                )
            )
            known_paths.add(resolved)
    return [*configured, *discovered]


def ground_audit_with_documents(
    *,
    result: AgentRunResult,
    paths: dict[str, Path],
    package: Any,
    settings: ApplicationSettings,
    project_root: Path,
    auditor_query: str,
) -> tuple[AgentRunResult, dict[str, Path]]:
    """Index configured documents, retrieve criteria and attach traceable references."""

    run_dir = Path(paths["candidate_findings"]).parent
    event_log = RunEventLogger(run_dir, result.run_id)
    source_config_path = package.root / "data_sources.yaml"
    document_sources = _collect_document_sources(
        package, source_config_path, project_root
    )
    if not document_sources:
        event_log.event("rag_skipped", reason="no_documents")
        return result, paths
    event_log.event(
        "rag_started",
        documents=len(document_sources),
        collection=settings.qdrant.collection,
        model=settings.embedding.model,
    )

    document_paths = [
        resolve_source_location(source, source_config_path)
        for source in document_sources
    ]
    missing = [str(path) for path in document_paths if not path.is_file()]
    if missing:
        raise FileNotFoundError("Не найдены документы аудита: " + ", ".join(missing))

    client = create_qdrant_client(
        url=settings.qdrant.url,
        api_key=os.getenv(settings.qdrant.api_key_env),
        path=None if settings.qdrant.url else settings.qdrant.path,
    )
    embedder = BgeM3Embedder(
        settings.embedding.model,
        device=settings.embedding.device,
        batch_size=settings.embedding.batch_size,
    )
    retriever = QdrantRetriever(client, embedder, settings.qdrant.collection)
    fingerprint = _fingerprint(
        document_paths, settings.embedding.model, settings.qdrant.collection
    )
    manifest_path = _manifest_path(project_root, settings.qdrant.collection)
    manifest = _read_manifest(manifest_path)
    collection_exists = client.collection_exists(settings.qdrant.collection)
    index_required = not collection_exists or manifest.get("fingerprint") != fingerprint
    event_log.event(
        "rag_index_decision",
        collection_exists=collection_exists,
        index_required=index_required,
        fingerprint=fingerprint,
    )

    indexed_chunks = 0
    document_errors: list[str] = []
    if index_required:
        if collection_exists:
            client.delete_collection(settings.qdrant.collection)
        chunks = []
        for source in document_sources:
            try:
                chunks.extend(
                    load_document_chunks(
                        source,
                        source_config_path,
                        chunk_size=settings.chunking.size,
                        chunk_overlap=settings.chunking.overlap,
                    )
                )
            except Exception as error:
                document_errors.append(
                    f"{source.source_id}: {type(error).__name__}: {error}"
                )
                event_log.exception(
                    "document_extraction_failed", error, source_id=source.source_id
                )
        if not chunks:
            raise RuntimeError(
                "Не удалось извлечь ни одного фрагмента документов: "
                + "; ".join(document_errors)
            )
        indexed_chunks = retriever.index(chunks)
        event_log.event(
            "rag_index_updated",
            documents=len(document_sources),
            chunks=indexed_chunks,
            collection=settings.qdrant.collection,
        )
        _write_json_atomic(
            manifest_path,
            {
                "fingerprint": fingerprint,
                "collection": settings.qdrant.collection,
                "model": settings.embedding.model,
                "documents": len(document_sources),
                "chunks": indexed_chunks,
            },
        )

    representatives: dict[str, Any] = {}
    for finding in result.findings:
        representatives.setdefault(finding.check_id, finding)

    searches: dict[str, dict[str, Any]] = {}
    references_by_check: dict[str, list[EvidenceReference]] = {}
    for check_id, finding in representatives.items():
        search_query = " ".join(
            part
            for part in (
                auditor_query,
                finding.title,
                finding.criterion,
                " ".join(finding.tags),
            )
            if part
        )[:4000]
        matches = retriever.search(search_query, limit=3)
        event_log.event(
            "rag_search_completed",
            check_id=check_id,
            query_length=len(search_query),
            matches=len(matches),
            top_scores=[match.score for match in matches],
        )
        searches[check_id] = {
            "query": search_query,
            "matches": [match.model_dump(mode="json") for match in matches],
        }
        references_by_check[check_id] = [
            EvidenceReference(
                checksum=hashlib.sha256(
                    f"{match.chunk.chunk_id}:{match.chunk.text}".encode("utf-8")
                ).hexdigest(),
                source_name=match.chunk.source_id,
                object_id=match.chunk.chunk_id,
                description=match.chunk.text[:1000],
                fields={
                    "score": match.score,
                    "chunk_index": match.chunk.chunk_index,
                    "start_char": match.chunk.start_char,
                    "end_char": match.chunk.end_char,
                    "metadata": match.chunk.metadata,
                },
                query=search_query,
            )
            for match in matches
        ]

    grounded_findings = [
        finding.model_copy(
            update={
                "evidence": [
                    *finding.evidence,
                    *references_by_check.get(finding.check_id, []),
                ],
                "tags": list(dict.fromkeys([*finding.tags, "rag_grounded"])),
            }
        )
        for finding in result.findings
    ]
    document_descriptors = [
        DataSource(
            source_id=source.source_id,
            relative_path=str(path),
            file_format=path.suffix.lower().lstrip("."),
            size_bytes=path.stat().st_size,
        )
        for source, path in zip(document_sources, document_paths, strict=True)
    ]
    metrics = {
        **result.metrics,
        "rag": {
            "documents": len(document_sources),
            "indexed_chunks": indexed_chunks,
            "index_reused": not index_required,
            "grounded_rule_groups": len(searches),
            "document_errors": document_errors,
        },
    }
    errors = [*result.execution_errors, *document_errors]
    grounded = result.model_copy(
        update={
            "findings": grounded_findings,
            "data_sources": [*result.data_sources, *document_descriptors],
            "metrics": metrics,
            "execution_errors": errors,
            "status": RunStatus.COMPLETED_WITH_ERRORS if errors else result.status,
        }
    )
    rag_context_path = run_dir / "rag_context.json"
    _write_json_atomic(
        rag_context_path,
        {
            "run_id": result.run_id,
            "collection": settings.qdrant.collection,
            "documents": [source.model_dump(mode="json") for source in document_sources],
            "searches": searches,
            "errors": document_errors,
        },
    )
    updated_paths = write_run_outputs(grounded, run_dir)
    updated_paths["rag_context"] = rag_context_path
    event_log.event(
        "rag_completed",
        status=grounded.status.value,
        documents=len(document_sources),
        indexed_chunks=indexed_chunks,
        grounded_rule_groups=len(searches),
        document_errors=len(document_errors),
        rag_context_path=str(rag_context_path),
    )
    return grounded, updated_paths
