# Audit Insight Agent

Локальный прототип универсального аудиторского агента. Таблицы загружаются в DuckDB, документы индексируются BGE-M3 в Qdrant, правила формируют воспроизводимые evidence, `candidate_findings.json` и `report.md`.

Полная пошаговая установка, подготовка входных архивов и порядок запуска описаны в [INSTALLATION.md](INSTALLATION.md).

## Данные и воспроизводимость

Входные данные, эталонное решение и архивы результатов лежат в [общей папке Google Drive](https://drive.google.com/drive/folders/1MonROlCn1B_VijhhTCpeXW7RGL8e-vgn?usp=drive_link):

- `data.zip` — входные данные;
- `knowledge.zip` — справочные документы;
- `ovp_ground_truth/` — закрытое эталонное решение;
- `RUN-20260719T151515Z-7F19AE.zip` — полные артефакты запуска;
- `RUN-20260719T151515Z-7F19AE_CONCLUSION.zip` — пакет итогового заключения для воспроизводимости результата.

`ovp_ground_truth/` не передаётся агенту и используется только для независимой оценки.

## Архитектура

```text
Gradio web :7860
    → Ouroboros server :8765 (/api/tasks)
        → Audit Insight public API
            ├─ DuckDB: таблицы, сверки, расчёты
            ├─ BGE-M3 + Qdrant :6333: поиск по документам
            ├─ Rule engine: quality, reconciliation, timeline, anomaly
            └─ outputs/runs/<run_id>/: evidence, findings, report
```

Qdrant и Ouroboros — отдельные процессы. `scripts/run_web.py` их не запускает.

## Структура проекта

```text
.
├─ configs/
│  ├─ config.example.yaml
│  ├─ data_sources.example.yaml
│  └─ logging.yaml
├─ data/
│  ├─ ovp/                     # CSV, DOCX и PDF проверки ОВП
│  └─ robots/                  # C++, Java, Confluence и журналы роботов
├─ knowledge/
│  ├─ documents/
│  └─ metadata/
├─ rules/
│  ├─ access_control/
│  ├─ cash/                    # правила кассы и timeline
│  ├─ data_quality/
│  ├─ market_operations/       # правила торговых операций
│  └─ ovp/                     # правила и связи ОВП
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
│  ├─ report_generator.py, workspace.py
│  ├─ ouroboros_tools.py       # публичный API аудита
│  ├─ ouroboros.py             # HTTP-клиент Ouroboros
│  ├─ web.py, cli.py           # Gradio и CLI
│  ├─ developer_orchestrator.py, developer_tools.py
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
│  └─ rules.md
├─ outputs/
│  ├─ evidence/
│  ├─ findings/
│  ├─ reports/
│  └─ runs/                    # артефакты по run_id
├─ logs/                            # ротируемый audit-insight.log
├─ .env.example
├─ INSTALLATION.md
├─ .gitignore
├─ pyproject.toml
├─ requirements.txt
└─ README.md
```

## Куда класть файлы

- Любые входные данные: `data/` и любые её подпапки. Агент читает CSV, Excel, Parquet, JSON, PDF, DOCX, Markdown, TXT, HTML, C/C++ и Java.
- Дополнительные знания и нормативные документы: `knowledge/`.
- Декларативные правила и связи: `rules/`. Правило запускается только когда все его `source_ids` автоматически найдены.
- Общие и предметные промпты: `prompts/`.
- Результаты создаются автоматически в `outputs/runs/<run_id>/`.

При web-аудите агент автоматически находит таблицы и документы, профилирует все таблицы, выбирает применимые правила, индексирует документы в Qdrant и сохраняет обнаруженную структуру в артефактах run.

## PostgreSQL и Greenplum

Реплики регистрируются в `configs/config.yaml`; пароль хранится только в environment:

```yaml
databases:
  connections:
    replica_a:
      engine: postgresql       # или greenplum
      host: db-read.example.local
      port: 5432
      database: audit_db
      user: audit_readonly
      password_env: AUDIT_REPLICA_A_PASSWORD
      sslmode: require
```

Источник в `data_sources.yaml` может ссылаться на точный alias или на alias из запроса аудитора:

```yaml
- source_id: accounting_entries
  source_type: table
  format: postgresql
  connection: $selected
  location: audit.accounting_entries
  # Вместо location можно задать один read-only SELECT/WITH:
  # query: SELECT entry_id, amount, operation_date FROM audit.accounting_entries
```

В чате alias нужно назвать точно, например: `проверь replica_a`. Произвольные DSN из чата не
принимаются. Коннектор принудительно включает read-only transaction, читает данные пакетами и переносит их в DuckDB.

```bash
export AUDIT_REPLICA_A_PASSWORD="..."
```

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
Если используется Conda, в `python_executable` нужно указать путь, который выводит `which python` в активированном окружении Audit Agent.

Проверка зависимостей:

```bash
python -c "import duckdb, gradio, psycopg, qdrant_client, sentence_transformers, docx, pypdf; print('dependencies: OK')"
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

Открыть <http://127.0.0.1:7860> и выбрать один из двух независимых режимов. Вкладка
«Аудитор — быстрый» анализирует данные и возвращает заключение сразу после аудита, не
ожидая самоулучшения. Вкладка «Аудитор + разработчик» после аудита дополнительно проводит
review качества и при необходимости запускает контролируемое самоулучшение. У каждой
вкладки собственный диалог и состояние, а формат сохранённых результатов одинаков.

При временной потере соединения с Ouroboros web-интерфейс сохраняет уже показанный
отчёт и продолжает опрос задачи. Если аудит успел завершиться до остановки сервера,
результат восстанавливается из сохранённого маркера без повторного расчёта.
Активный `request_id` также хранится в браузере: если страница или сам Gradio
перезагрузились во время анализа, новая web-сессия подключается к той же задаче
и ждёт сохранённый результат, не запуская аудит повторно.

Интерфейс показывает текущий этап, ответ, findings, `run_id` и отчёт. Если в
`configs/config.yaml` включён `self_improvement.enabled`, полный режим при обоснованном системном пробеле может
изменить код, правила, RAG, промпты и тесты в isolated worktree. Система сама запускает
`pytest` и сохраняет patch, но не делает commit или merge. Вводить `run_id` вручную или
переключать ветку не нужно.
Подробнее: [docs/developer_mode.md](docs/developer_mode.md).

### Feedback закрытого evaluator

`audit-evaluator` публикует только санитизированную обратную связь в:

```text
outputs/runs/<run_id>/evaluation/feedback.json
```

Ground truth, закрытые ID и бизнес-логика evaluator в Ouroboros не передаются. После оценки можно запустить контролируемый review:

```bash
audit-insight improve-from-feedback --run-id <run_id>
```

Ouroboros проверяет контракт feedback, сопоставляет его с артефактами run и создаёт только reviewable patch в isolated worktree. Результат review сохраняется в `evaluation/improvement.json`; commit и merge не выполняются.

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
├─ discovered_sources.json   # автоматически найденные источники
├─ profiles.json             # структура и качество таблиц
├─ relationships.json        # применимые связи
├─ selected_rules.json       # выбранные и пропущенные правила
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

Прямой запуск проверки без web и Ouroboros:

```bash
audit-insight audit --data-dir data --query "Проверить данные и сформировать выводы"
```

Доступные Ouroboros функции: `list_data_sources`, `profile_data_source`, `search_documents`, `run_rule`, `run_rule_group`, `build_findings`, `generate_report`, `run_full_audit`. Developer-функции изменения кода и веток находятся в `developer_tools.py` и не вызываются обычным web-аудитом.

## Проверка

```bash
pytest -q
```
