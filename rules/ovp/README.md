# Правила OVP

Здесь будут храниться правила проверки открытой валютной позиции и утверждённых лимитов.

Планируемый пример структуры, не готовое исполняемое правило:

```yaml
rule_id: OVP_LIMIT_001
condition: abs(total_position) > approved_limit
severity: high
```
