# Архитектура

## Основной поток

```text
CLI или Ouroboros
        ↓
публичные прикладные интерфейсы
        ↓
загрузчики → профилирование / правила / сверки / аномалии
        ↓
Evidence → AuditFinding → отчёт
```

## Архитектурные ограничения

- Документы и табличные данные загружаются разными модулями.
- Все границы используют единые модели из `models.py`.
- Формальные нарушения отделены от статистических аномалий.
- Любой существенный вывод должен быть связан с воспроизводимым Evidence.
- Ouroboros работает только через allowlist функций `ouroboros_tools.py`.
- CLI и Ouroboros используют одно ядро без дублирования бизнес-логики.

## Универсальный ingestion/RAG

```text
data/ + knowledge/
   ├── automatic table discovery → DuckDB → DataProfile
   └── automatic document discovery → chunks → BGE-M3 → Qdrant
```

Файловые источники не требуют YAML-реестра. `configs/data_sources.yaml` остаётся
необязательным каталогом для SQL-реплик и явных метаданных.

## Поток аудита

```text
запрос аудитора
  → обнаружение и профилирование всех таблиц
  → отбор применимых YAML-правил из rules/
  → sql / reconciliation / timeline / anomaly
  → checksum-защищённые evidence JSON
  → объединение evidence по стабильному finding_id
  → candidate_findings.json + report.md
```

`группа rules/*/relationships.yaml` определяет ключи сверок. Python-ядро остаётся
предметно-независимым; поля и формулы задаются в `rules/`.

## Web и Ouroboros

```text
Gradio chat
  → OuroborosHTTPClient
  → отдельный Ouroboros server (/api/tasks)
  → allowlisted ouroboros_tools в workspace Audit Insight
  → AuditInsightAgent
  → candidate_findings.json + report.md
  → isolated improvement/<run_id> worktree
```

Web и developer mode используют разные модули. Developer tools не имеют операций merge
и не могут менять `data/`, `knowledge/`, secrets и production-конфиги.
Web не запускает Ouroboros и Qdrant как дочерние процессы: их URL задаются в
`configs/config.yaml`.

Все ключевые этапы записываются в `outputs/runs/<run_id>/events.jsonl`. Общий
ротируемый журнал находится в `logs/audit-insight.log`, а запрос и ответ
web-чата — в `outputs/runs/<run_id>/chat.json`.
