"""Unified agentic audit and controlled self-improvement workflow."""
from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Any

from .developer_orchestrator import OuroborosDeveloperOrchestrator
from .models import ApplicationSettings
from .ouroboros import OuroborosOrchestrator


class AuditAgentSystem:
    """Run the audit and a controlled Ouroboros capability review."""

    logger = logging.getLogger("audit_insight.agent_system")

    def __init__(
        self,
        audit: OuroborosOrchestrator | None = None,
        developer: OuroborosDeveloperOrchestrator | None = None,
        settings: ApplicationSettings | None = None,
    ) -> None:
        self.audit = audit or OuroborosOrchestrator(settings=settings)
        self.settings = settings or self.audit.settings
        self.developer = developer or OuroborosDeveloperOrchestrator(
            settings=self.settings
        )

    def run_with_updates(self, user_request: str) -> Iterator[dict[str, Any]]:
        audit_result: dict[str, Any] | None = None
        for event in self.audit.run_with_updates(user_request):
            if event["kind"] == "result":
                audit_result = event["result"]
                continue
            yield event
        if audit_result is None:
            return

        improvement = self.settings.self_improvement
        detected_gap = bool(
            audit_result.get("improvement_needed")
            or audit_result.get("execution_errors")
        )
        should_review = improvement.enabled and (
            improvement.review_after_every_audit
            or detected_gap
            or not improvement.require_detected_gap
        )
        if not should_review:
            audit_result["self_improvement"] = {
                "status": "NOT_REQUIRED" if improvement.enabled else "DISABLED",
                "merged": False,
            }
            yield {"kind": "result", "result": audit_result}
            return

        yield {
            "kind": "status",
            "message": (
                "Ouroboros проводит post-audit review: ищет "
                "пробелы в логике, правилах, RAG и промптах…"
            ),
        }
        try:
            for event in self.developer.run_after_audit(audit_result, user_request):
                if event["kind"] == "status":
                    yield {
                        "kind": "status",
                        "message": f"Самоулучшение: {event['message']}",
                    }
                    continue
                has_changes = bool(event["result"].get("has_changes", True))
                tests_passed = event["result"].get("tests_passed")
                if has_changes and tests_passed is not True:
                    status = "TESTS_FAILED"
                else:
                    status = "PATCH_READY" if has_changes else "NO_CHANGES"
                audit_result["self_improvement"] = {
                    "status": status,
                    **event["result"],
                }
        except Exception as error:
            self.logger.exception(
                "Automatic improvement failed run_id=%s", audit_result.get("run_id")
            )
            audit_result["self_improvement"] = {
                "status": "FAILED",
                "error": f"{type(error).__name__}: {error}",
                "merged": False,
            }
            yield {
                "kind": "status",
                "message": "Аудит завершён, но improvement-patch отклонён.",
            }
        yield {"kind": "result", "result": audit_result}
