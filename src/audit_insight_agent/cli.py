from __future__ import annotations

import argparse
import json
from pathlib import Path

from .agent import AuditInsightAgent

"""Командный интерфейс для автономного запуска аудиторского ядра.

Первая версия должна предоставить команды `audit-insight profile`,
`audit-insight check` и `audit-insight report`. CLI использует те же прикладные
интерфейсы, что и Ouroboros, и не содержит отдельной бизнес-логики.
"""


def build_parser() -> argparse.ArgumentParser:

    parser = argparse.ArgumentParser(
        prog="audit-insight",
    )

    parser.add_argument(
        "--data-dir",
        required=True,
        help="Публичный каталог данных",
    )

    parser.add_argument(
        "--output-root",
        default="outputs/runs",
        help="Каталог результатов",
    )

    parser.add_argument(
        "--run-id",
        default=None,
        help="Необязательный идентификатор запуска",
    )

    parser.add_argument(
        "--agent-version",
        default="0.1.0",
    )

    return parser


def main() -> None:

    arguments = (
        build_parser().parse_args()
    )

    agent = AuditInsightAgent(
        agent_version=(
            arguments.agent_version
        ),
    )

    result, paths = agent.run(
        data_dir=arguments.data_dir,
        output_root=(
            arguments.output_root
        ),
        run_id=arguments.run_id,
    )

    print(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": (
                    result.status.value
                ),
                "findings_count": len(
                    result.findings
                ),
                "candidate_findings": str(
                    paths[
                        "candidate_findings"
                    ]
                ),
                "report": str(
                    paths["report"]
                ),
                "run_manifest": str(
                    paths["run_manifest"]
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
