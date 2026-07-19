# Установка и запуск Audit Insight Agent

Документ описывает полную последовательность развёртывания локального аудиторского агента. Для работы нужны три отдельных сервиса:

```text
Qdrant :6333
    → Ouroboros server :8765
        → Audit Insight web :7860
```

## 1. Исходные файлы

Архивы и закрытое эталонное решение хранятся в [папке Google Drive](https://drive.google.com/drive/folders/1MonROlCn1B_VijhhTCpeXW7RGL8e-vgn?usp=drive_link):

- `data.zip` — входные таблицы, сделки, заявки, исполнения, котировки, комиссии, данные Murex и другие рабочие источники, ВНД;
- `knowledge.zip` — нормативные и справочные документы для RAG;
- `ovp_ground_truth/` — закрытые файлы с эталонным решением, номерами сделок и обоснованием отклонений;
- `RUN-20260719T151515Z-7F19AE.zip` — полные артефакты тестового запуска;
- `RUN-20260719T151515Z-7F19AE_CONCLUSION.zip` — пакет итогового заключения для воспроизводимости результата.

`ovp_ground_truth/` не является входом агента. Её нельзя копировать в `data/`, `knowledge/`, индексировать в Qdrant или передавать Ouroboros. Папка нужна только для независимой оценки и должна лежать вне рабочей папки проекта.

Рекомендуемая структура:

```text
/path/to/audit-insight/       # этот репозиторий
/path/to/ouroboros-core/      # сервер Ouroboros
/path/to/ovp_ground_truth/    # закрытое решение, вне audit-insight
```

## 2. Подготовка `data/` и `knowledge/`

Скачайте `data.zip` и `knowledge.zip` из Google Drive. Если архивы уже содержат верхние папки `data/` и `knowledge/`, распакуйте их в корень проекта:

```bash
PROJECT_DIR="/path/to/audit-insight"
cd "$PROJECT_DIR"
unzip /path/to/downloads/data.zip
unzip /path/to/downloads/knowledge.zip
```

Если внутри архивов лежат сами файлы без верхней папки, используйте:

```bash
mkdir -p data knowledge
unzip /path/to/downloads/data.zip -d data
unzip /path/to/downloads/knowledge.zip -d knowledge
```

## 3. Создание окружения `audit-agent`

Единый `requirements.txt` зафиксирован по фактическому Conda-окружению `audit-agent`: Python 3.12.13, PyTorch 2.6.0 с CUDA 11.8, Sentence Transformers 5.6.0 и Gradio 6.20.0.

```bash
conda create -n audit-agent python=3.12 -y
conda activate audit-agent

cd "/path/to/audit-insight"
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m pip install -e . --no-deps
```

Файл соответствует Linux x86_64 с NVIDIA GPU и CUDA 11.8. Для CPU-only или другой версии CUDA строки PyTorch/CUDA нужно адаптировать под целевую машину.

Проверка окружения:

```bash
python --version
python -c "import duckdb, gradio, psycopg, qdrant_client, sentence_transformers, torch; print('dependencies: OK'); print('CUDA:', torch.cuda.is_available())"
python -m pytest -q
```

## 4. Модель BGE-M3

Модель embedding должна быть заранее скачана в локальную папку. Пример:

```text
/path/to/models/bge-m3
```

Укажите этот путь в `configs/config.yaml` в поле `embedding.model`. Не оставляйте шаблонное `/path/to/bge-m3`.

## 5. Конфигурация Audit Insight

```bash
cd "/path/to/audit-insight"
cp configs/config.example.yaml configs/config.yaml
cp .env.example .env
```

Обязательно проверьте в `configs/config.yaml`:

```yaml
embedding:
  model: /path/to/models/bge-m3
qdrant:
  url: http://127.0.0.1:6333
ouroboros:
  url: http://127.0.0.1:8765
  workspace: /path/to/audit-insight
  python_executable: /path/to/miniconda3/envs/audit-agent/bin/python
```

`workspace` должен указывать на корень этого проекта, а `python_executable` — на `python` из активированного `audit-agent`:

```bash
conda activate audit-agent
which python
```

## 6. Запуск Qdrant — терминал 1

При наличии Docker:

```bash
PROJECT_DIR="/path/to/audit-insight"
docker run --rm --name audit-qdrant \
  -p 6333:6333 \
  -v "$PROJECT_DIR/.audit_insight/qdrant-server:/qdrant/storage" \
  qdrant/qdrant
```

Проверка:

```bash
curl http://127.0.0.1:6333/collections
```

Dashboard: <http://127.0.0.1:6333/dashboard>.

## 7. Запуск Ouroboros — терминал 2

Ouroboros — отдельный проект со своим окружением. Сначала установите `ouroboros-core` по его собственной инструкции. Затем:

### Модели Ouroboros

В рабочей конфигурации использовались следующие модели:

| Роль | Модель |
| --- | --- |
| Main | `anthropic/claude-sonnet-5` |
| Heavy | `anthropic/claude-opus-4.8` |
| Light | `deepseek/deepseek-v4-flash` |
| Vision | `google/gemini-3.5-flash` |
| Consciousness | `deepseek/deepseek-v4-flash` |
| Fallback | `deepseek/deepseek-v4-pro` |
| Claude Code Model | `opus[1m]` |

Задайте их в `Settings → Model Routing` или через `data/settings.json`. Соответствующие поля конфигурации:

```json
{
  "OUROBOROS_MODEL": "anthropic/claude-sonnet-5",
  "OUROBOROS_MODEL_HEAVY": "anthropic/claude-opus-4.8",
  "OUROBOROS_MODEL_LIGHT": "deepseek/deepseek-v4-flash",
  "OUROBOROS_MODEL_VISION": "google/gemini-3.5-flash",
  "OUROBOROS_MODEL_CONSCIOUSNESS": "deepseek/deepseek-v4-flash",
  "OUROBOROS_MODEL_FALLBACKS": "deepseek/deepseek-v4-pro",
  "CLAUDE_CODE_MODEL": "opus[1m]"
}
```

Все флаги `Local` для этих слотов должны быть выключены. В разделе `Providers` нужно задать API-ключи провайдеров, через которых доступны указанные cloud model IDs. Ключи не добавляйте в Git.

```bash
CORE="/path/to/ouroboros-core"
AUDIT_PROJECT="/path/to/audit-insight"

cd "$CORE"
source .venv/bin/activate

export OUROBOROS_SAFETY_MODE=full
export OUROBOROS_FILE_BROWSER_DEFAULT="$AUDIT_PROJECT"
export OUROBOROS_SERVER_HOST=127.0.0.1
export OUROBOROS_SERVER_PORT=8765

ouroboros server --host 127.0.0.1 --port 8765
```

Проверка в третьем терминале:

```bash
curl http://127.0.0.1:8765/api/health
```

Не перезапускайте Ouroboros во время активного аудита. Внешний stop/restart отменяет текущую задачу; Audit Insight может один раз повторить её с сохранённого запроса, но не следует использовать это как штатный способ перезапуска.

## 8. Запуск web-интерфейса — терминал 3

```bash
conda activate audit-agent
cd "/path/to/audit-insight"
python scripts/run_web.py
```

Откройте <http://127.0.0.1:7860>.

Порядок запуска всегда один:

1. Qdrant.
2. Ouroboros.
3. Audit Insight web.
4. После проверки health endpoints — запуск аудита в браузере.

## 9. Первый запуск

В web-чате можно ввести, например:

```text
Проведи предварительную проверку обоснованности цен покупки валюты. Сопоставь ВНД, заявки, исполнения, сделки, рыночные котировки, комиссии и данные Murex.
```

Промежуточные и итоговые файлы создаются в `outputs/runs/<run_id>/`. Общий журнал записывается в `logs/audit-insight.log`.

Полные артефакты тестового аудита лежат в `RUN-20260719T151515Z-7F19AE.zip`, а пакет итогового заключения — в `RUN-20260719T151515Z-7F19AE_CONCLUSION.zip`. Оба архива размещены в [общей папке Google Drive](https://drive.google.com/drive/folders/1MonROlCn1B_VijhhTCpeXW7RGL8e-vgn?usp=drive_link) рядом с входными файлами.

## 10. Остановка и повторный запуск

Останавливайте сервисы только после завершения активного аудита в обратном порядке:

1. Audit Insight web.
2. Ouroboros.
3. Qdrant.

Перед новым запуском проверьте, что старые процессы не занимают порты 6333, 8765 и 7860:

```bash
ss -ltnp | grep -E ':6333|:8765|:7860'
```

## 11. Диагностика

```bash
tail -f logs/audit-insight.log
```

- `Connection refused` для `:6333` — не запущен Qdrant.
- `Connection refused` для `:8765` — не запущен Ouroboros.
- `Server shut down ... task was interrupted` — Ouroboros был остановлен извне во время задачи.
- Если аудит уже сохранил `outputs/requests/<request_id>.result.json`, web вернёт результат, не ожидая бесконечной финализации Ouroboros.
