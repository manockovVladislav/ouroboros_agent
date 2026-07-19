# Ouroboros self-improvement

Самоулучшение включается в `configs/config.yaml`:

```yaml
self_improvement:
  enabled: true
  review_after_every_audit: true
  require_detected_gap: true
  allow_blocking_clarification: false
  max_changed_files: 20
```

Пользователь отправляет один обычный запрос. Ouroboros сначала изучает источники,
запускает аудит и оценивает, не помешал ли системный пробел в коде, правилах, RAG
или промптах. Отсутствие нарушений или нехватка данных не считаются поводом менять код.

При `review_after_every_audit: true` developer-контур запускается после каждого аудита.
Ouroboros изучает общую логику и либо меняет код, правила, RAG, промпты или тесты,
либо завершает review со статусом `NO_CHANGES`. `require_detected_gap` остаётся режимом
для конфигураций, где ежезапусковый review выключен.

Внутренний `run_id` автоматически используется для создания:

```text
/tmp/audit-insight-improvements/<run_id>   # isolated Git worktree
improvement/<run_id>                       # isolated branch
outputs/runs/<run_id>/development/improvement.patch
```

Вводить `run_id` вручную не нужно. Переключать текущую рабочую ветку также не нужно.
Commit, push, merge, rebase и checkout для Ouroboros запрещены. Система отклоняет результат,
если обнаружен коммит, защищённый путь или превышен `max_changed_files`.
После правок оркестратор сам запускает `pytest`. Только прошедший тесты patch получает
статус `PATCH_READY`; неуспешный — `TESTS_FAILED`.

Вопрос пользователю допускается только до расчётов и только если назначение данных нельзя
установить из схем, конфигурации и knowledge. Во всех остальных случаях агент делает явное допущение
и продолжает аудит.
