"""Text extraction and deterministic chunking for configured documents."""
from __future__ import annotations

import re
import uuid
from pathlib import Path

from .config import resolve_source_location
from .models import DocumentChunk, SourceConfig


DOCUMENT_FORMATS = {
    ".pdf": "pdf",
    ".docx": "docx",
    ".md": "markdown",
    ".markdown": "markdown",
    ".txt": "text",
    ".html": "html",
    ".htm": "html",
}


def infer_document_format(path: Path, configured_format: str = "auto") -> str:
    if configured_format != "auto":
        return configured_format.lower()
    try:
        return DOCUMENT_FORMATS[path.suffix.lower()]
    except KeyError as error:
        raise ValueError(f"Unsupported document extension: {path.suffix}") from error


def extract_document_text(source: SourceConfig, config_path: str | Path) -> str:
    """Extract normalized text while keeping parsing concerns out of retrieval."""

    if source.source_type != "document":
        raise ValueError(f"Source {source.source_id!r} is not a document")
    path = resolve_source_location(source, config_path)
    if not path.is_file():
        raise FileNotFoundError(f"Document source not found: {path}")
    document_format = infer_document_format(path, source.format)

    if document_format in {"text", "markdown"}:
        text = path.read_text(encoding=source.encoding)
    elif document_format == "html":
        try:
            from bs4 import BeautifulSoup
        except ImportError as error:
            raise RuntimeError("HTML support requires 'beautifulsoup4'") from error
        text = BeautifulSoup(
            path.read_text(encoding=source.encoding), "html.parser"
        ).get_text("\n")
    elif document_format == "pdf":
        try:
            from pypdf import PdfReader
        except ImportError as error:
            raise RuntimeError("PDF support requires 'pypdf'") from error
        text = "\n\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    elif document_format == "docx":
        try:
            from docx import Document
        except ImportError as error:
            raise RuntimeError("DOCX support requires 'python-docx'") from error
        document = Document(path)
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    else:
        raise ValueError(f"Unsupported configured document format: {document_format}")

    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunk_text(
    source_id: str,
    text: str,
    metadata: dict[str, object] | None = None,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
) -> list[DocumentChunk]:
    """Split text on nearby whitespace with stable offsets and identifiers."""

    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap < 0 or chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be between 0 and chunk_size - 1")

    chunks = []
    start = 0
    index = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind(" ", start, end))
            if boundary > start + chunk_size // 2:
                end = boundary
        fragment = text[start:end].strip()
        if fragment:
            stable_key = f"{source_id}:{index}:{start}:{end}:{fragment}"
            chunks.append(
                DocumentChunk(
                    chunk_id=str(uuid.uuid5(uuid.NAMESPACE_URL, stable_key)),
                    source_id=source_id,
                    text=fragment,
                    chunk_index=index,
                    start_char=start,
                    end_char=end,
                    metadata=dict(metadata or {}),
                )
            )
            index += 1
        if end == len(text):
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks


def load_document_chunks(
    source: SourceConfig,
    config_path: str | Path,
    chunk_size: int = 1200,
    chunk_overlap: int = 200,
) -> list[DocumentChunk]:
    path = resolve_source_location(source, config_path)
    text = extract_document_text(source, config_path)
    metadata = {
        **source.metadata,
        "location": str(path),
        "format": infer_document_format(path, source.format),
    }
    return chunk_text(
        source.source_id,
        text,
        metadata=metadata,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )
