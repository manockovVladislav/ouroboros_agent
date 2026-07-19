from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import (
    AuditPlanItem,
    CandidateFinding,
    DataSource,
    EvidenceReference,
    FindingReview,
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


def review_findings_and_build_plan(
    findings: list[CandidateFinding],
    execution_errors: list[str],
    data_sources: list[DataSource] | None = None,
    rule_applicability: dict[str, dict[str, Any]] | None = None,
) -> tuple[list[FindingReview], list[AuditPlanItem]]:
    """Challenge candidate findings and produce a ranked, source-specific plan."""

    reviews: list[FindingReview] = []
    plan: list[AuditPlanItem] = []
    locations_by_source = {
        item.source_id: item.relative_path for item in (data_sources or [])
    }
    applicability_by_rule = rule_applicability or {}
    for finding in findings:
        applicability = applicability_by_rule.get(finding.check_id, {})
        table_evidence = [
            item
            for item in finding.evidence
            if item.evidence_id and item.checksum and item.query
        ]
        sources = sorted(
            {
                source.strip()
                for item in finding.evidence
                for source in item.source_name.split(",")
                if source.strip()
            }
        )
        source_locations = sorted(
            {
                str(location)
                for item in finding.evidence
                for location in (
                    (item.fields.get("metadata") or {}).get("location"),
                    (item.fields.get("metadata") or {}).get("relative_path"),
                )
                if location
            }
            | {
                locations_by_source[source]
                for source in sources
                if source in locations_by_source
            }
        )
        limitations = []
        limitations.append(
            "Правило подтверждает отклонение, но не доказывает заявленную первопричину; "
            "её нужно проверять отдельно."
        )
        if not source_locations:
            limitations.append(
                "Точный путь к нормативному документу не привязан."
            )
        if finding.confidence < 0.75:
            limitations.append(
                "Уровень уверенности ниже порога 0.75; сигнал требует проверки."
            )
        if applicability.get("status") == "PARTIAL":
            limitations.append(
                "Словарь правила покрывает не все фактические значения; семантическая "
                "применимость вывода требует проверки."
            )
        if not table_evidence:
            verdict = "REJECTED"
            rationale = (
                "Вывод не прошёл критику: нет воспроизводимой строки результата "
                "с checksum и SQL-запросом."
            )
        elif finding.confidence < 0.75 or applicability.get("status") == "PARTIAL":
            verdict = "REQUIRES_VALIDATION"
            rationale = (
                "Воспроизводимый сигнал есть, но уверенность или покрытие словаря недостаточны, "
                "чтобы выдать его за подтверждённое нарушение."
            )
        else:
            verdict = "CONFIRMED"
            rationale = (
                "Вывод подтверждён воспроизводимым результатом правила, checksum и ссылкой "
                "на объект источника."
            )
        reviews.append(
            FindingReview(
                finding_id=finding.finding_id,
                verdict=verdict,
                rationale=rationale,
                evidence_checks=[
                    f"Воспроизводимых табличных evidence: {len(table_evidence)}",
                    f"Всего ссылок на evidence: {len(finding.evidence)}",
                    f"Confidence: {finding.confidence:.2f}",
                    (
                        "Rule vocabulary row coverage: "
                        f"{float(applicability['row_coverage']):.1%}"
                        if "row_coverage" in applicability
                        else "Rule vocabulary: not applicable to this rule kind"
                    ),
                ],
                limitations=limitations,
                source_locations=source_locations,
            )
        )
        if verdict == "REJECTED":
            continue
        plan.append(
            AuditPlanItem(
                plan_id="PLAN-" + hashlib.sha256(
                    finding.finding_id.encode("utf-8")
                ).hexdigest()[:12].upper(),
                priority=finding.severity,
                status=(
                    "CONFIRMED_ISSUE"
                    if verdict == "CONFIRMED"
                    else "POTENTIAL_RISK"
                ),
                title=finding.title,
                rationale=rationale,
                finding_id=finding.finding_id,
                sources=sources,
                source_locations=source_locations,
                next_steps=[
                    "Повторить расчёт по SQL из evidence и сверить строку источника.",
                    "Сопоставить факт с указанным критерием и документом.",
                    "Отдельно подтвердить первопричину; не выводить её только из факта отклонения.",
                ],
            )
        )

    for index, error in enumerate(execution_errors, start=1):
        plan.append(
            AuditPlanItem(
                plan_id=f"PLAN-BLOCKED-{index:03d}",
                priority=Severity.HIGH,
                status="BLOCKED_CHECK",
                title="Невыполненная аудиторская проверка",
                rationale=error,
                next_steps=[
                    "Устранить ошибку источника или правила и повторить запуск."
                ],
            )
        )
    severity_order = {
        Severity.CRITICAL: 0,
        Severity.HIGH: 1,
        Severity.MEDIUM: 2,
        Severity.LOW: 3,
    }
    plan.sort(key=lambda item: (severity_order[item.priority], item.plan_id))
    return reviews, plan
