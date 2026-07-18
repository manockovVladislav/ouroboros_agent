# Audit Insight Agent

Локальный прототип универсального аудиторского агента. Таблицы загружаются в DuckDB, документы индексируются BGE-M3 в Qdrant, правила формируют воспроизводимые evidence, `candidate_findings.json` и `report.md`.

## Архитектура

```text
Gradio web :7860
    → Ouroboros server :8765 (/api/tasks)
        → Audit Insight public API
            ├─ DuckDB: таблицы, сверки, расчёты
            ├─ BGE-M3 + Qdrant :6333: поиск по документам
            ├─ Rule engine: quality, reconciliation, timeline, anomaly
            └─ outputs/runs/<run_id>/: evidence, findings, report
                → audit-evaluator (если настроен)
```

Qdrant и Ouroboros — отдельные процессы. `scripts/run_web.py` их не запускает.

## Структура проекта

```text
.
├─ cases/
│  ├─ README.md
│  └─ physical_currency_ovp/
│     ├─ data_sources.yaml
│     ├─ relationships.yaml
│     ├─ prompts/auditor_context.md
│     └─ rules/                 # 4 YAML-правила текущей проверки
├─ configs/
│  ├─ config.example.yaml
│  ├─ data_sources.example.yaml
│  └─ logging.yaml
├─ data/
│  └─ ovp/                     # CSV, DOCX и PDF текущей проверки
├─ knowledge/
│  ├─ documents/
│  └─ metadata/
├─ rules/
│  ├─ access_control/
│  ├─ cash/
│  ├─ data_quality/
│  ├─ market_operations/
│  └─ ovp/
├─ prompts/
│  ├─ system_prompt.md
│  ├─ audit_analysis.md
│  ├─ finding_prompt.md
│  └─ report_prompt.md
├─ scripts/
│  ├─ build_index.py
│  ├─ export_output_schema.py
│  ├─ profile_data.py
│  ├─ run_agent.py
│  ├─ run_checks.py
│  ├─ ouroboros_audit.py
│  └─ run_web.py
├─ skills/audit_insight/
│  ├─ SKILL.md
│  └─ tools.py
├─ src/audit_insight_agent/
│  ├─ __init__.py
│  ├─ agent.py                # оркестрация аудита
│  ├─ config.py, models.py     # конфиги и контракты
│  ├─ data_loader.py, data_profiler.py
│  ├─ document_loader.py, ingestion.py, retriever.py
│  ├─ audit_rag.py            # индексация и RAG-grounding выводов
│  ├─ rule_engine.py, analysis_tools.py, anomaly_detector.py
│  ├─ reconciliation.py, evidence_store.py, finding_builder.py
│  ├─ report_generator.py, case_package.py
│  ├─ ouroboros_tools.py       # публичный API аудита
│  ├─ ouroboros.py             # HTTP-клиент Ouroboros
│  ├─ web.py, cli.py           # Gradio и CLI
│  ├─ evaluator_adapter.py, developer_tools.py
│  └─ logging_config.py, run_logging.py
├─ templates/
│  ├─ finding.md.j2
│  └─ report.md.j2
├─ tests/
│  ├─ fixtures/
│  ├─ integration/
│  └─ unit/
├─ docs/
│  ├─ architecture.md
│  ├─ deployment.md
│  ├─ developer_mode.md
│  ├─ evaluator.md
│  └─ rules.md
├─ outputs/
│  ├─ evidence/
│  ├─ findings/
│  ├─ reports/
│  └─ runs/                    # артефакты по run_id
├─ logs/                            # ротируемый audit-insight.log
├─ .env.example
├─ .gitignore
├─ pyproject.toml
├─ requirements.txt
├─ requirements.lock.txt
└─ README.md
```

## Куда класть файлы

- Данные текущей проверки: `data/ovp/`. Таблицы и PDF, DOCX, Markdown, TXT, HTML могут лежать вместе.
- Дополнительные знания и нормативные документы: `knowledge/`.
- Описания текущих файлов и связей: `cases/physical_currency_ovp/data_sources.yaml` и `relationships.yaml`.
- Правила текущей проверки: `cases/physical_currency_ovp/rules/`; общие группы: `rules/`.
- Результаты создаются автоматически в `outputs/runs/<run_id>/`.

При полном web-аудите агент автоматически находит документы в `data/` и `knowledge/`, индексирует изменённый набор в Qdrant и добавляет релевантные фрагменты к evidence каждой группы выводов.

## Установка

```bash
PROJECT_DIR="/path/to/ouroboros"
cd "$PROJECT_DIR"
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp configs/config.example.yaml configs/config.yaml
```

После копирования укажите в `configs/config.yaml` свои пути:

```yaml
embedding:
  model: /path/to/bge-m3
qdrant:
  url: http://127.0.0.1:6333
ouroboros:
  url: http://127.0.0.1:8765
  workspace: /path/to/ouroboros
  python_executable: .venv/bin/python
```

`/path/to/...` нужно заменить на пути в своём окружении.
Если используется Conda, в `python_executable` нужно указать пут, который выводит `which python` в активированном окружении Audit Agent.

Проверка зависимостей:

```bash
python -c "import duckdb, gradio, qdrant_client, sentence_transformers, docx, pypdf; print('dependencies: OK')"
```

Dashboard Qdrant: <http://127.0.0.1:6333/dashboard>. В конфиге указывается базовый API URL без `/dashboard`.

## Запуск Ouroboros

Ouroboros запускается отдельно:

```bash
CORE="/path/to/ouroboros-core"
TEST_WORKSPACE="/path/to/ouroboros-test-workspace"

mkdir -p "$TEST_WORKSPACE"
echo "Тестовая папка Ouroboros" > "$TEST_WORKSPACE/README.txt"

cd "$CORE"
source .venv/bin/activate

export OUROBOROS_SAFETY_MODE=full
export OUROBOROS_FILE_BROWSER_DEFAULT="$TEST_WORKSPACE"
export OUROBOROS_SERVER_HOST=127.0.0.1
export OUROBOROS_SERVER_PORT=8765

ouroboros server --host 127.0.0.1 --port 8765
```

`OUROBOROS_FILE_BROWSER_DEFAULT` задаёт папку интерфейса Ouroboros. Для каждой задачи Audit Insight web передаёт свой `workspace_root` из `configs/config.yaml`.

## Запуск web-чата

Когда Qdrant и Ouroboros уже работают, в отдельном терминале:

```bash
PROJECT_DIR="/path/to/ouroboros"
cd "$PROJECT_DIR"
source .venv/bin/activate
python scripts/run_web.py
```

Открыть <http://127.0.0.1:7860>. Интерфейс показывает статус задачи Ouroboros, ответ агента, findings, `run_id` и файл отчёта.

## Логи и история

Общий журнал web, CLI, Ouroboros-шлюза и аудиторского ядра:

```text
logs/audit-insight.log
```

Файл ротируется при размере 10 МБ; хранится до 10 архивов. Путь можно переопределить через `AUDIT_LOG_DIR`.

Каждый аудит сохраняет отдельную машиночитаемую историю:

```text
outputs/runs/<run_id>/
├─ events.jsonl              # этапы, статусы, ошибки и метрики
├─ chat.json                 # запрос аудитора, ответ и Ouroboros task_id
├─ candidate_findings.json
├─ rag_context.json
├─ report.md
├─ run_manifest.json
└─ evidence/
```

Наблюдение за общим логом:

```bash
tail -f logs/audit-insight.log
```

События run пишутся в append-only JSONL. Поля с именами `password`, `secret`, `token`, `api_key` и `authorization` маскируются.

## CLI и индексация

Загрузка источников из YAML и построение индекса:

```bash
audit-insight ingest --config cases/physical_currency_ovp/data_sources.yaml --settings configs/config.yaml
```

Прямой запуск проверки без web и Ouroboros:

```bash
audit-insight audit --case cases/physical_currency_ovp --query "Проверить данные и сформировать выводы"
```

Доступные Ouroboros функции: `list_data_sources`, `profile_data_source`, `search_documents`, `run_rule`, `run_rule_group`, `build_findings`, `generate_report`, `run_full_audit`. Developer-функции изменения кода и веток находятся в `developer_tools.py` и не вызываются обычным web-аудитом.

## Проверка

```bash
pytest -q
```
