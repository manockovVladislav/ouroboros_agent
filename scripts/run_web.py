"""Launch the local Audit Insight Gradio chat."""
import os
import sys
from pathlib import Path

# Support the documented direct launch without requiring an editable install.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = PROJECT_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from audit_insight_agent.logging_config import configure_logging
from audit_insight_agent.web import build_interface


if __name__ == "__main__":
    configure_logging(PROJECT_ROOT / "configs" / "logging.yaml")
    build_interface().launch(
        server_name=os.getenv("AUDIT_WEB_HOST", "127.0.0.1"),
        server_port=int(os.getenv("AUDIT_WEB_PORT", "7860")),
        share=False,
        show_error=True,
    )
