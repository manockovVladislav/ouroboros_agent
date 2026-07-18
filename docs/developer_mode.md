# Ouroboros developer mode

Developer mode никогда не изменяет стабильную рабочую копию. Для запуска
`RUN-...` создаётся отдельный Git worktree и ветка:

```text
/tmp/audit-insight-improvements/<run_id>
└── improvement/<run_id>
```

Игнорируемые runtime-входы из `data/`, `knowledge/documents/` и локальные
конфиги подключаются в worktree символическими ссылками. Они доступны для
повторного анализа, но не попадают в diff или patch.

Разрешённый API:

- `create_improvement_branch(run_id)`;
- `read_feedback(run_id)`;
- `apply_code_changes(run_id, patch)`;
- `run_tests(run_id, test_path="tests")`;
- `create_patch(run_id, evaluation_run_id=None)`.

Перед применением patch проверяется активная ветка, размер patch и каждый
затронутый путь. Запрещены `.env`, локальная/production-конфигурация,
`audit-evaluator`, любые пути с evaluator или ground truth и всё вне allowlist
исходного кода, правил, cases, тестов и документации.

API не содержит merge/rebase/push. Изменения заканчиваются patch-файлом для
повторной оценки и ручной проверки. Автоматический merge в `main`, `develop`
или другую стабильную ветку невозможен.

Экспорт patch дополнительно закрыт quality gate: feedback указанного
evaluation-run обязан содержать `quality_improved: true`. При отсутствии
оценки или ухудшении patch не выдаётся.
