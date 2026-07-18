from __future__ import annotations

from .models import (
    AuditContext,
    CandidateFinding,
)

"""Поиск нестандартных случаев вне покрытия формальных правил.

Аномалия — объект для дополнительной проверки, а не подтверждённое нарушение.
TODO: определить методы, признаки, пороги, объяснимость результатов и защиту
от превращения статистического сигнала в необоснованное аудиторское мнение.
"""

def detect_anomalies(
    context: AuditContext,
) -> list[CandidateFinding]:
    """
    Поиск необычных операций и цепочек.
    """

    return []

