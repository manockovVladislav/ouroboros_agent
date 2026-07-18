from audit_insight_agent.config import load_application_settings


def test_application_settings_load_local_embedding_path(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        """version: 1
embedding:
  model: /models/bge-m3
qdrant: {}
storage: {}
chunking: {}
""",
        encoding="utf-8",
    )

    settings = load_application_settings(config)
    assert settings.embedding.model == "/models/bge-m3"
    assert settings.qdrant.collection == "audit_knowledge"
