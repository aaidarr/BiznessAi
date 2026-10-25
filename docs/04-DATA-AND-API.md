# Данные и API (Этап 3)

Источник истины по схеме — `app/models.py`; миграции — `migrations/versions/` (проверены на PostgreSQL: up → down → up, `alembic check` без расхождений).

## Таблицы
| Таблица | Назначение | Важные ограничения |
|---|---|---|
| `users` | Владельцы и операторы | `email` уникален; пароль argon2id; счётчик неудачных входов и `locked_until` |
| `auth_sessions` | Сессии | В БД хранится только SHA-256 токена; у каждой сессии свой CSRF-токен |
| `businesses` | Арендатор (1:1 с владельцем) | `public_key` уникален; токены TG/VK зашифрованы (Fernet); `kb_version` сбрасывает кэш индекса |
| `documents` | Источники знаний: файл, сайт, шаблон | Удаление документа удаляет его фрагменты (`ON DELETE CASCADE`) |
| `kb_items` | Фрагменты базы знаний | Бот видит только `published = true`; `document_id` связывает с источником; `faq`/`manual` (написано владельцем) приоритетнее `page`/`file` |
| `services`, `resources`, `resource_services` | Услуги, специалисты, кто что оказывает | Удаление с историей записей → скрытие (`active = false`) |
| `working_hours`, `time_off` | Недельное расписание, выходные | Интервалы хранятся в минутах от полуночи по TZ бизнеса |
| `bookings` | Записи (`note` — заметка владельца, `channel = owner` для ручных) | Частичный уникальный индекс `(resource_id, start_utc) WHERE status != 'cancelled'` + блокировка строки ресурса при создании и переносе |
| `conversations`, `messages` | Диалоги и журнал | `UNIQUE(business_id, channel, external_id)`; удаление каскадом; хранение 180 дней |
| `gaps` | Вопросы без ответа | `UNIQUE(business_id, key)` — ключ из стемов вопроса |
| `leads` | Заявки | — |
| `jobs` | Фоновые задачи (краулер) | Один активный краул на бизнес |
| `payments` | Платежи ЮKassa | `provider_id` уникален; статус подтверждается запросом к API |
| `audit_log` | Аудит | Без секретов и ПДн в `detail` |

`businesses.tone` — тон общения (`friendly`/`formal`), попадает в системный промпт модели.

Все даты — `timestamptz`, в приложении всегда aware UTC (`UTCDateTime`).

## Публичные эндпоинты (без сессии)
| Метод и путь | Защита |
|---|---|
| `GET /healthz` | Проверяет БД |
| `GET /widget.js` | Статический скрипт, без HTML-инъекций (`textContent`) |
| `GET /api/widget/{key}/config`, `POST /api/widget/{key}/message`, `OPTIONS …` | Список разрешённых Origin на бизнес; лимиты 30/мин на IP, 15/мин на сессию, 600/мин на бизнес; длина ≤1000; идентификатор сессии 16–64 символа |
| `POST /hook/tg/{key}` | Заголовок `X-Telegram-Bot-Api-Secret-Token` (constant-time) |
| `POST /hook/vk/{key}` | `group_id` + `secret` из запроса (constant-time) |
| `POST /hook/yookassa` | Тело не доверяется: платёж перепроверяется запросом к API, сверяется сумма |
| `GET/POST /login`, `/register`, `POST /logout` | Лимиты, блокировка, Origin-проверка |

## Кабинет (`/app/*`, требуется сессия, все POST — с CSRF-токеном)
- `/app` — «Сегодня».
- `/app/calendar?view=week|month|day&date=&resource=&cancelled=` · `/app/calendar/new` · `/app/calendar/booking/{id}` (+ `confirm`, `cancel`, `note`, `move`) · `/app/calendar/setup` (+ `service`, `resource`, `resource/{id}/schedule|timeoff|delete`, `timeoff/{id}/delete`).
- `/app/bot/answers` (+ `add`, `publish-all`, `{id}/update|toggle|delete`) · `/app/bot/documents` (+ `upload`, `import-site`, `{id}/publish|delete`) · `/app/bot/templates[/{key}]` · `/app/bot/personality` · `/app/bot/fix/{message_id}`.
- `/app/reports?days=7|30|90` · `/app/reports/export/{leads|bookings|dialogs}.csv`.
- `/app/test` (+ `message`, `reset`, `autotest`) · `/app/channels` (+ `verify-site`, `toggle`, `telegram`, `telegram/disconnect`, `vk`, `vk/disconnect`) · `/app/leads` · `/app/gaps` · `/app/conversations` · `/app/billing` (+ `checkout`) · `/app/settings`. Старый `/app/kb` перенаправляет на `/app/bot/answers`.

Любой объект по `id` проверяется на принадлежность бизнесу (`owned()`); чужой `id` → 404 (тест `test_tenant_isolation_idor`).

## Оператор
`/admin` (только `is_admin`, иначе 404): список бизнесов, диалоги за месяц, токены, блокировка, ручная смена тарифа, журнал аудита. Первый администратор: `python -m app.cli create-admin EMAIL`.
