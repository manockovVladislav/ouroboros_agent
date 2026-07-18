# Ouroboros self-improvement

Самоулучшение включается в `configs/config.yaml`:

```yaml
self_improvement:
  enabled: true
  require_detected_gap: true
  allow_blocking_clarification: true
  max_changed_files: 20
```

Пользователь отправляет один обычный запрос. Ouroboros снача изучает источники,
запускает аудит и оценивает, не помешал ли системный пробел в коде, правилах, RAG
или промптах. Отсутствие нарушений или нехватка данных не считаются поводом менять код.

При `require_detected_gap: true` improvement-контур не запускается без обнаруженного пробела.
Если пробел есть, внутренний `run_id` автоматически используется для создания:

```text
/tmp/audit-insight-improvements/<run_id>   # isolated Git worktree
improvement/<run_id>                       # isolated branch
outputs/runs/<run_id>/development/improvement.patch
```

Вводить `run_id` вручную не нужно. Переключать текущую рабочую ветку также не нужно.
Commit, push, merge, rebase и checkout для Ouroboros запрещены. Система отклоняет результат,
если обнаружен коммит, защищённый путь или превышен `max_changed_files`.

Вопрос пользователю допускается только до расчётов и только если назначение данных нельзя
установить из схем, конфигурации и knowledge. Во всех остальных случаях агент делает явное допущение
и продолжает аудит.
