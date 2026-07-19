"""Command-line boundary for generic ingestion, retrieval and audit runs."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .agent import AuditInsightAgent
from .config import load_application_settings, load_source_catalog
from .data_loader import DuckDBTableStore
from .developer_orchestrator import OuroborosDeveloperOrchestrator
from .ingestion import ingest_catalog
from .logging_config import configure_logging
from .models import ApplicationSettings
from .ouroboros_tools import _output_root
from .retriever import BgeM3Embedder, QdrantRetriever, create_qdrant_client


def _rag_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--settings", default="configs/config.yaml")
    parser.add_argument("--collection")
    parser.add_argument("--qdrant-url")
    parser.add_argument("--qdrant-path")
    parser.add_argument("--embedding-model")
    parser.add_argument("--device")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="audit-insight")
    commands = parser.add_subparsers(dest="command", required=True)

    ingest = commands.add_parser("ingest", help="Load, profile and index YAML sources")
    ingest.add_argument("--config", required=True)
    ingest.add_argument("--database")
    ingest.add_argument("--output")
    ingest.add_argument("--chunk-size", type=int)
    ingest.add_argument("--chunk-overlap", type=int)
    ingest.add_argument("--replica")
    _rag_arguments(ingest)

    search = commands.add_parser("search", help="Search indexed requirements")
    search.add_argument("query")
    search.add_argument("--limit", type=int, default=5)
    _rag_arguments(search)

    audit = commands.add_parser("audit", help="Discover inputs and run an audit")
    audit.add_argument("--data-dir", default="data")
    audit.add_argument("--query", required=True)
    audit.add_argument("--output-root", default="outputs/runs")
    audit.add_argument("--database", default=":memory:")
    audit.add_argument("--run-id")
    audit.add_argument("--agent-version", default="0.3.0")
    audit.add_argument("--settings", default="configs/config.yaml")
    audit.add_argument(
        "--replica",
        help="Exact allowlisted replica alias from settings",
    )

    agent = commands.add_parser("agent", help="Run the existing audit check pipeline")
    agent.add_argument("--data-dir", required=True)
    agent.add_argument("--output-root", default="outputs/runs")
    agent.add_argument("--run-id")
    agent.add_argument("--agent-version", default="0.3.0")

    improve = commands.add_parser(
        "improve-from-feedback",
        help="Review sanitized audit-evaluator feedback in an isolated worktree",
    )
    improve.add_argument("--run-id", required=True)
    improve.add_argument("--settings", default="configs/config.yaml")
    return parser


def _build_retriever(
    arguments: argparse.Namespace,
    settings: ApplicationSettings,
) -> QdrantRetriever:
    qdrant_url = arguments.qdrant_url or settings.qdrant.url
    qdrant_path = arguments.qdrant_path or settings.qdrant.path
    client = create_qdrant_client(
        url=qdrant_url,
        api_key=os.getenv(settings.qdrant.api_key_env),
        path=None if qdrant_url else qdrant_path,
    )
    embedder = BgeM3Embedder(
        arguments.embedding_model or settings.embedding.model,
        device=arguments.device or settings.embedding.device,
        batch_size=settings.embedding.batch_size,
    )
    return QdrantRetriever(
        client,
        embedder,
        arguments.collection or settings.qdrant.collection,
    )


def _run_ingest(arguments: argparse.Namespace) -> None:
    settings = load_application_settings(arguments.settings)
    catalog = load_source_catalog(arguments.config)
    needs_rag = any(
        source.enabled and source.source_type == "document"
        for source in catalog.sources
    )
    retriever = _build_retriever(arguments, settings) if needs_rag else None
    database = Path(arguments.database or settings.storage.duckdb_path)
    database.parent.mkdir(parents=True, exist_ok=True)
    if arguments.replica and arguments.replica not in settings.databases.connections:
        raise ValueError(f"Unknown database replica: {arguments.replica}")
    with DuckDBTableStore(
        database,
        database_settings=settings.databases,
        selected_replica=arguments.replica,
    ) as store:
        result = ingest_catalog(
            arguments.config,
            store,
            retriever,
            chunk_size=arguments.chunk_size or settings.chunking.size,
            chunk_overlap=(
                arguments.chunk_overlap
                if arguments.chunk_overlap is not None
                else settings.chunking.overlap
            ),
        )
    output = Path(arguments.output or settings.ingestion_output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    print(result.model_dump_json(indent=2))


def _run_search(arguments: argparse.Namespace) -> None:
    settings = load_application_settings(arguments.settings)
    results = _build_retriever(arguments, settings).search(
        arguments.query,
        arguments.limit,
    )
    print(
        json.dumps(
            [item.model_dump(mode="json") for item in results],
            ensure_ascii=False,
            indent=2,
        )
    )


def _run_agent(arguments: argparse.Namespace) -> None:
    result, paths = AuditInsightAgent(arguments.agent_version).run(
        data_dir=arguments.data_dir,
        output_root=arguments.output_root,
        run_id=arguments.run_id,
    )
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status.value,
                "findings_count": len(result.findings),
                "candidate_findings": str(paths["candidate_findings"]),
                "report": str(paths["report"]),
                "run_manifest": str(paths["run_manifest"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _run_audit(arguments: argparse.Namespace) -> None:
    settings = load_application_settings(arguments.settings)
    if arguments.replica and arguments.replica not in settings.databases.connections:
        raise ValueError(f"Unknown database replica: {arguments.replica}")
    result, paths = AuditInsightAgent(arguments.agent_version).run(
        data_dir=arguments.data_dir,
        auditor_query=arguments.query,
        output_root=arguments.output_root,
        run_id=arguments.run_id,
        database=arguments.database,
        settings=settings,
        selected_replica=arguments.replica,
    )
    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status.value,
                "findings_count": len(result.findings),
                "candidate_findings": str(paths["candidate_findings"]),
                "report": str(paths["report"]),
                "run_manifest": str(paths["run_manifest"]),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


def _run_improve_from_feedback(arguments: argparse.Namespace) -> None:
    settings = load_application_settings(arguments.settings)
    orchestrator = OuroborosDeveloperOrchestrator(settings=settings)
    final_result: dict | None = None
    for event in orchestrator.run_from_evaluator_feedback(arguments.run_id):
        print(json.dumps(event, ensure_ascii=False))
        if event.get("kind") == "result":
            final_result = event.get("result")
    if final_result is None:
        raise RuntimeError("Ouroboros не вернул результат improvement")

    output = (
        _output_root()
        / arguments.run_id
        / "evaluation"
        / "improvement.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(final_result, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )


def main() -> None:
    project_root = Path(__file__).resolve().parents[2]
    logging_path = Path(
        os.getenv("AUDIT_LOG_CONFIG", str(project_root / "configs/logging.yaml"))
    )
    configure_logging(logging_path)
    arguments = build_parser().parse_args()
    if arguments.command == "ingest":
        _run_ingest(arguments)
    elif arguments.command == "search":
        _run_search(arguments)
    elif arguments.command == "audit":
        _run_audit(arguments)
    elif arguments.command == "improve-from-feedback":
        _run_improve_from_feedback(arguments)
    else:
        _run_agent(arguments)


if __name__ == "__main__":
    main()
