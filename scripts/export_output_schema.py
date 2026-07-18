import argparse
import json
from pathlib import Path

from audit_insight_agent.models import (
    AgentRunResult,
)

"""Экспорт JSON Schema выходного контракта агента.

TODO: после реализации AgentResponse экспортировать его JSON Schema в
стабильный машиночитаемый файл. Скрипт должен импортировать публичную
модель из audit_insight_agent, а не дублировать её поля.
"""


def main() -> None:

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--output",
        default=(
            "contracts/"
            "agent_run_result.schema.json"
        ),
    )

    arguments = parser.parse_args()

    output_path = Path(
        arguments.output
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    schema = (
        AgentRunResult
        .model_json_schema()
    )

    output_path.write_text(
        json.dumps(
            schema,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"JSON Schema создана: "
        f"{output_path}"
    )


if __name__ == "__main__":
    main()
