from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import (
    CandidateFinding,
    EvidenceReference,
    Severity,
)

"""Сборка валидированных аудиторских выводов и их дедупликация."""



def make_finding_id(
    check_id: str,
    issue_type: str,
    primary_object_id: str,
) -> str:
    """
    Создает стабильный finding_id.
    """

    value = (
        f"{check_id}|"
        f"{issue_type}|"
        f"{primary_object_id}"
    )

    digest = hashlib.sha256(
        value.encode("utf-8")
    ).hexdigest()[:16]

    return f"FND-{digest.upper()}"


def build_finding(
    *,
    check_id: str,
    issue_type: str,
    primary_object_id: str,
    title: str,
    summary: str,
    root_cause: str,
    criterion: str = "",
    risk: str = "",
    severity: Severity,
    confidence: float,
    evidence: list[EvidenceReference],
    **additional_fields: Any,
) -> CandidateFinding:

    return CandidateFinding(
        finding_id=make_finding_id(
            check_id=check_id,
            issue_type=issue_type,
            primary_object_id=(
                primary_object_id
            ),
        ),
        check_id=check_id,
        issue_type=issue_type,
        title=title,
        summary=summary,
        root_cause=root_cause,
        criterion=criterion,
        risk=risk,
        severity=severity,
        confidence=confidence,
        evidence=evidence,
        **additional_fields,
    )


def deduplicate_findings(
    findings: list[CandidateFinding],
) -> list[CandidateFinding]:
    """
    Объединяет одинаковые finding_id.
    """

    result: dict[
        str,
        CandidateFinding,
    ] = {}

    for finding in findings:

        existing = result.get(
            finding.finding_id
        )

        if existing is None:
            result[
                finding.finding_id
            ] = finding
            continue

        evidence_map = {
            json.dumps(
                evidence.model_dump(
                    mode="json",
                ),
                ensure_ascii=False,
                sort_keys=True,
            ): evidence
            for evidence
            in (
                existing.evidence
                + finding.evidence
            )
        }

        result[
            finding.finding_id
        ] = existing.model_copy(
            update={
                "confidence": max(
                    existing.confidence,
                    finding.confidence,
                ),
                "evidence": list(
                    evidence_map.values()
                ),
            }
        )

    return sorted(
        result.values(),
        key=lambda finding: (
            -finding.confidence,
            finding.finding_id,
        ),
    )
