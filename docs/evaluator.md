# External Audit Evaluator

Evaluator является отдельным процессом и отдельным репозиторием. Только он
имеет доступ к закрытому ground truth. Audit Insight Agent передаёт ему путь к
`candidate_findings.json`, но не открывает evaluator-каталоги и не читает
`evaluation.json`.

Подключение:

```bash
export AUDIT_EVALUATOR_ROOT=/path/to/audit-evaluator
export AUDIT_EVALUATOR_COMMAND="python -m audit_evaluator"
```

Команда получает аргументы `--case`, `--run-id`, `--candidate-findings` и
`--output-dir`. Ожидаемые результаты:

```text
outputs/runs/<run_id>/evaluation/
├── evaluation.json                 # закрыт для Ouroboros
└── feedback_for_ouroboros.json     # доступен через read_feedback
```

`feedback_for_ouroboros.json` проходит строгую схему: summary, общие
weaknesses, recommendations, агрегированные метрики и признак улучшения.
Любые дополнительные поля, включая точные ответы или ground truth,
отклоняются.
