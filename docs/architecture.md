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
YAML registry
   ├── table source → pandas reader → DuckDB → DataProfile
   └── document source → text extraction → chunks → BGE-M3 → Qdrant
```

Форматы, пути, ожидаемые поля, ключи и метаданные задаются конфигурацией.
Специализированные пакеты проверок используют зарегистрированные таблицы и
результаты поиска, но не меняют ingestion-ядро.

## Поток аудиторского case-пакета

```text
запрос аудитора
  → выбор YAML-правил по описаниям и тегам
  → загрузка только используемых источников в DuckDB
  → sql / reconciliation / timeline / anomaly
  → checksum-защищённые evidence JSON
  → объединение evidence по стабильному finding_id
  → candidate_findings.json + report.md
```

`relationships.yaml` определяет источники и ключи сверок. Python-компиляторы
работают только с общими понятиями source, key, value, timestamp и event; все
предметные поля и формулы остаются в case-пакете.

## Web, Ouroboros и evaluator

```text
Gradio chat
  → OuroborosHTTPClient
  → отдельный Ouroboros server (/api/tasks)
  → allowlisted ouroboros_tools в workspace Audit Insight
  → AuditInsightAgent
  → candidate_findings.json + report.md
  → external audit-evaluator
  → sanitized feedback_for_ouroboros.json
  → isolated improvement/<run_id> worktree
```

Web и developer mode используют разные модули. Developer tools не входят в
обычный web callback и не имеют операций merge или доступа к evaluator.
Web не запускает Ouroboros и Qdrant как дочерние процессы: их URL задаются в
`configs/config.yaml`.
