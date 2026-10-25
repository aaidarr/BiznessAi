# Otvet — самозапускаемый ИИ-администратор для малого бизнеса

Владелец вставляет адрес сайта → система собирает базу знаний → бот отвечает клиентам **только по этой базе** (виджет на сайте, Telegram, VK), записывает на свободное время и передаёт менеджеру всё, чего не знает.

Ключевая идея: **бот не врёт**. Ответ формируется только если в базе есть релевантный фрагмент; каждое число, телефон, ссылка и «модальное» слово («нельзя», «бесплатно», «скидка») в ответе сверяется с фрагментами; иначе ответ заменяется дословной выдержкой из базы.

## Быстрый старт (разработка)
```bash
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload   # http://localhost:8000, SQLite otvet.db, без ключей
```
В dev схема создаётся автоматически (`create_all`) и **не обновляется**: после изменения моделей удалите `otvet.db`. В проде схема только через `alembic upgrade head`.

Без ключей LLM бот работает в режиме `extractive`: отдаёт дословные предложения из базы.

## Проверки
```bash
pytest -q                                   # SQLite
TEST_DATABASE_URL=postgresql+psycopg://user@host/db pytest -q   # PostgreSQL + тест параллельной записи
ruff check app tests && ruff format --check app tests
mypy app
bandit -r app -ll
pip-audit -r requirements.txt
alembic upgrade head && alembic check       # миграции соответствуют моделям
```

## Документация
| Файл | Содержание |
|---|---|
| [docs/01-TZ.md](docs/01-TZ.md) | Техническое задание и правила достоверности |
| [docs/02-ARCHITECTURE.md](docs/02-ARCHITECTURE.md) | Архитектура и стек |
| [docs/03-THREAT-MODEL.md](docs/03-THREAT-MODEL.md) | Модель угроз |
| [docs/04-DATA-AND-API.md](docs/04-DATA-AND-API.md) | Данные и эндпоинты |
| [docs/05-DEPLOY.md](docs/05-DEPLOY.md) | Развёртывание, бэкап, откат, мониторинг |
| [docs/06-SECURITY-CHECKLIST.md](docs/06-SECURITY-CHECKLIST.md) | OWASP-чеклист и остаточные риски |
| [docs/07-RELEASE-REPORT.md](docs/07-RELEASE-REPORT.md) | Итоговый отчёт: что проверено, что нет, что осталось |
| [docs/08-CABINET.md](docs/08-CABINET.md) | Личный кабинет: исследование аналогов, структура, решения |

## Структура
```
app/            приложение (FastAPI): web/, services/, channels/, llm/
migrations/     Alembic
tests/          pytest (юнит, интеграция, безопасность, параллельная запись)
scripts/        deploy / rollback / backup / restore
```

## Подключение внешних сервисов
Все ключи — через `.env` (см. `.env.example`). Что работает без них и что требует доступа — таблица в [docs/01-TZ.md](docs/01-TZ.md) §8.
