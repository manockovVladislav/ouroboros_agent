"""Compatibility wrapper for configuration-driven ingestion."""
import sys

from audit_insight_agent.cli import main


if __name__ == "__main__":
    sys.argv.insert(1, "ingest")
    main()
