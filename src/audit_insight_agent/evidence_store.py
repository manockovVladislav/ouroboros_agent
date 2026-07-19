"""Immutable filesystem evidence records with reproducibility checksums."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import AuditRule, EvidenceRecord, RuleKind


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def build_evidence_record(
    run_id: str,
    rule: AuditRule,
    object_id: str,
    query: str,
    result: dict[str, Any],
) -> EvidenceRecord:
    rule_hash = hashlib.sha256(
        _canonical(rule.model_dump(mode="json")).encode("utf-8")
    ).hexdigest()
    content = {
        "rule_id": rule.rule_id,
        "rule_version": rule.version,
        "rule_hash": rule_hash,
        "rule_kind": rule.kind.value,
        "source_ids": rule.source_ids,
        "object_id": object_id,
        "query": query,
        "result": result,
    }
    checksum = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    evidence_id = "EVD-" + hashlib.sha256(f"{run_id}:{checksum}".encode()).hexdigest()[:20].upper()
    return EvidenceRecord(
        evidence_id=evidence_id,
        checksum=checksum,
        run_id=run_id,
        rule_id=rule.rule_id,
        rule_version=rule.version,
        rule_hash=rule_hash,
        rule_kind=rule.kind,
        source_ids=rule.source_ids,
        object_id=object_id,
        query=query,
        result=result,
        created_at=datetime.now(timezone.utc),
    )


def build_observation_evidence_record(
    *,
    run_id: str,
    check_id: str,
    source_ids: list[str],
    object_id: str,
    query: str,
    result: dict[str, Any],
) -> EvidenceRecord:
    """Build evidence for a reproduced read-only observation outside rule catalogues."""

    rule_contract = {
        "check_id": check_id,
        "kind": RuleKind.CONTRADICTION.value,
        "source_ids": sorted(source_ids),
        "version": "1",
    }
    rule_hash = hashlib.sha256(
        _canonical(rule_contract).encode("utf-8")
    ).hexdigest()
    content = {
        "rule_id": check_id,
        "rule_version": "1",
        "rule_hash": rule_hash,
        "rule_kind": RuleKind.CONTRADICTION.value,
        "source_ids": source_ids,
        "object_id": object_id,
        "query": query,
        "result": result,
    }
    checksum = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
    evidence_id = "EVD-" + hashlib.sha256(
        f"{run_id}:{checksum}".encode("utf-8")
    ).hexdigest()[:20].upper()
    return EvidenceRecord(
        evidence_id=evidence_id,
        checksum=checksum,
        run_id=run_id,
        rule_id=check_id,
        rule_version="1",
        rule_hash=rule_hash,
        rule_kind=RuleKind.CONTRADICTION,
        source_ids=source_ids,
        object_id=object_id,
        query=query,
        result=result,
        created_at=datetime.now(timezone.utc),
    )


class EvidenceStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, evidence: EvidenceRecord) -> Path:
        path = self.root / f"{evidence.evidence_id}.json"
        if path.exists():
            existing = self.get(evidence.evidence_id)
            if existing.checksum != evidence.checksum:
                raise ValueError(f"Evidence ID collision: {evidence.evidence_id}")
            return path
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(evidence.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(path)
        return path

    def get(self, evidence_id: str) -> EvidenceRecord:
        if not re_full_evidence_id(evidence_id):
            raise ValueError("Invalid evidence_id")
        path = self.root / f"{evidence_id}.json"
        record = EvidenceRecord.model_validate_json(path.read_text(encoding="utf-8"))
        content = {
            "rule_id": record.rule_id,
            "rule_version": record.rule_version,
            "rule_hash": record.rule_hash,
            "rule_kind": record.rule_kind.value,
            "source_ids": record.source_ids,
            "object_id": record.object_id,
            "query": record.query,
            "result": record.result,
        }
        checksum = hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()
        if checksum != record.checksum:
            raise ValueError(f"Evidence checksum mismatch: {evidence_id}")
        return record


def re_full_evidence_id(value: str) -> bool:
    import re

    return re.fullmatch(r"EVD-[A-F0-9]{20}", value) is not None
