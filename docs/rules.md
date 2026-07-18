# Декларативные правила

Правила хранятся в YAML вне Python-кода. Файл может содержать одно правило
либо каталог `version: 1` + `rules: [...]`.

Поддерживаемые виды:

- `sql` — SELECT-запрос к разрешённым источникам;
- `contradiction` — SQL-проверка взаимоисключающих фактов или состояний;
- `reconciliation` — агрегированная сверка источников по relationship;
- `timeline` — контроль порядка и интервалов событий;
- `anomaly` — объяснимые z-score или IQR-сигналы.

Пример SQL-правила:

```yaml
rule_id: VALUE_LIMIT
version: "1.0"
kind: sql
description: Проверка значения относительно лимита
source_ids: [operations]
tags: [лимит]
max_findings: 100
sql:
  object_id_column: operation_id
  query: >-
    SELECT operation_id, amount, approved_limit
    FROM {{operations}}
    WHERE abs(amount) > approved_limit
finding:
  title: Превышен лимит операции {operation_id}
  summary: Значение {amount} превышает установленный лимит.
  issue_type: limit_breach
  root_cause: Требуется исследование причины.
  criterion: Значение не должно превышать утверждённый лимит.
  risk: Выполнение операции вне установленного риск-аппетита.
  recommendation: Проверить операцию и настройку контроля.
  severity: HIGH
  confidence: 1.0
```

Имена таблиц подставляются только через `{{source_id}}`. Движок разрешает
только `SELECT`/`WITH` и запрещает SQL-команды изменения данных, подключение
файлов, расширений и внешних баз. Произвольный Python и shell не исполняются.

Для каждого отклонения сохраняются версия и hash правила, фактический SQL,
идентификаторы источников, строка результата и checksum. Статистическая
аномалия должна иметь пониженную уверенность и явно маркироваться как сигнал
для дополнительной проверки.

`max_findings` ограничивает количество materialized evidence для одного
правила. При превышении RuleResult получает `truncated: true`; это защищает
боевой запуск от неограниченного роста файлов и памяти.
