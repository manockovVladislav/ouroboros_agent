import json
import logging

from audit_insight_agent.logging_config import configure_logging
from audit_insight_agent.run_logging import RunEventLogger, write_chat_history


def test_run_history_is_append_only_and_redacts_secrets(tmp_path):
    run_dir = tmp_path / "RUN-LOG"
    event_log = RunEventLogger(run_dir, "RUN-LOG")
    event_log.event(
        "test_event",
        api_key="must-not-leak",
        nested={"password": "must-not-leak", "value": 42},
    )
    event_log.event("second_event", status="ok")

    lines = [json.loads(line) for line in event_log.path.read_text("utf-8").splitlines()]
    assert [line["event"] for line in lines] == ["test_event", "second_event"]
    assert lines[0]["details"]["api_key"] == "***REDACTED***"
    assert lines[0]["details"]["nested"]["password"] == "***REDACTED***"

    chat_path = write_chat_history(
        run_dir,
        run_id="RUN-LOG",
        task_id="task-1",
        user_request="Check data",
        ouroboros_answer="Completed",
    )
    chat = json.loads(chat_path.read_text("utf-8"))
    assert [message["role"] for message in chat["messages"]] == ["user", "assistant"]


def test_logging_configuration_writes_rotating_file(tmp_path):
    config = tmp_path / "configs" / "logging.yaml"
    config.parent.mkdir()
    config.write_text(
        """version: 1
disable_existing_loggers: false
formatters:
  standard:
    format: "%(levelname)s %(message)s"
handlers:
  file:
    class: logging.handlers.RotatingFileHandler
    formatter: standard
    filename: audit.log
    maxBytes: 1024
    backupCount: 2
    encoding: utf-8
root:
  level: INFO
  handlers: [file]
""",
        encoding="utf-8",
    )
    configure_logging(config, force=True)
    logging.getLogger("test").info("persistent history")
    for handler in logging.getLogger().handlers:
        handler.flush()

    log_path = tmp_path / "logs" / "audit.log"
    assert "persistent history" in log_path.read_text("utf-8")
