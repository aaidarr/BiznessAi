# Архитектура и стек (Этап 2–3)

## Решение: модульный монолит
Один FastAPI-процесс + фоновый воркер (поток) в том же образе. Микросервисы не нужны при ~10 диалогах/мин.

## Стек и обоснование
| Слой | Выбор | Почему |
|---|---|---|
| Язык/веб | Python 3.12+, FastAPI | Быстрая разработка, типы, async для вебхуков |
| ORM/миграции | SQLAlchemy 2, Alembic | Стандарт, поддержка Postgres/SQLite |
| БД | PostgreSQL 16 (прод), SQLite (dev/тесты) | Транзакции для записи на слоты. pgvector не берём: поиск лексический (BM25 + стемминг), база арендатора мала |
| Поиск | BM25 по стеммам (snowball ru), индекс в памяти на арендатора | Без внешних зависимостей, детерминирован, тестируем. Эмбеддинги — после MVP |
| UI | Jinja2 + немного JS | Нет сборки фронтенда, CSRF проще, меньше поверхности атаки |
| Пароли | argon2id | Рекомендация OWASP |
| Секреты каналов | Fernet (cryptography) | Токены TG/VK не хранятся открытым текстом |
| HTTP-клиент | httpx | Краулер, LLM, TG/VK, мок-транспорт для тестов |
| Тесты | pytest, ruff, mypy, bandit, pip-audit | Покрытие логики, стиль, типы, SAST, уязвимости зависимостей |
| Деплой | Docker, docker-compose (app + postgres), GitHub Actions | Воспроизводимо |

## Модули
```
app/
  main.py, config.py, db.py, models.py, security.py, cli.py
  llm/        base, extractive, yandex, openai_compat
  services/   kb, crawler, retrieval, guard, assistant, booking, leads, billing, audit, notify, autotest, worker, report
  channels/   telegram, vk, widget (api)
  web/        auth, cabinet, admin, api  + templates/ + static/
migrations/   Alembic
tests/
```

## Поток сообщения
```
канал(виджет/TG/VK) → ChannelAdapter → assistant.handle(conversation, text)
   ├─ лимит тарифа / блокировка / владелец-режим
   ├─ активный сценарий записи? → booking flow (детерминированный)
   ├─ интент: запись / менеджер / отмена / приветствие
   └─ иначе: retrieval → guard.gate → llm.generate → guard.verify → ответ
журнал: Message(outcome, source_ids), Gap при отказе, Lead при передаче
```

## Мультиарендность
Все таблицы бизнеса содержат `business_id`. Веб-слой получает `Business` только через зависимость `current_business` (из сессии). Запросы всегда фильтруются по нему; тесты изоляции проверяют чужие id (IDOR).

## Запись и двойное бронирование
Расчёт слотов: рабочие интервалы ресурса в TZ бизнеса − брони (pending/confirmed) − выходные − минимальное время подготовки. Создание брони: транзакция, `SELECT ... FOR UPDATE` по ресурсу (Postgres), проверка пересечения, вставка; уникальный ключ `(resource_id, start_utc)` как страховка.

## Окружения
`APP_ENV=dev|test|prod`. В `prod` приложение не стартует без `SECRET_KEY`, `ENCRYPTION_KEY`, `DATABASE_URL`, `PUBLIC_BASE_URL` и при небезопасных значениях по умолчанию.

## Масштабирование
Один инстанс + Postgres. Узкие места и план: rate limit → Redis; индекс поиска → pgvector/внешний; воркер → отдельный процесс (`python -m app.worker`).
