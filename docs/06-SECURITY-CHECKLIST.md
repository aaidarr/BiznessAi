# Безопасность: чеклист и остаточные риски (Этап 6)

Ссылки на тесты — в `tests/`. «Проверено» = есть автоматический тест, прошедший в этой среде. Это не гарантия отсутствия уязвимостей и не заменяет независимый пентест.

## OWASP Top 10 (2021) → меры
| Риск | Меры | Статус |
|---|---|---|
| A01 Broken Access Control | Изоляция по `business_id`, `owned()` на всех объектах, `/admin` скрыт (404), CSRF на всех POST кабинета | Проверено: `test_tenant_isolation_idor`, `test_bot_of_one_tenant_never_uses_anothers_base`, `test_csrf_required_on_post`, `test_admin_hidden_from_regular_users` |
| A02 Cryptographic Failures | argon2id; токены каналов в Fernet; сессии хранятся хэшем; cookie `HttpOnly`/`SameSite=Lax`/`Secure` (https); HSTS | Проверено: `test_password_stored_as_argon2`, `test_session_token_stored_hashed`, `test_encryption_roundtrip_and_mask`, `test_cookie_secure_when_https` |
| A03 Injection | Только ORM/параметры; автоэкранирование Jinja; CSP без inline; промпт-инъекции гасит `guard` | Проверено: `test_xss_is_escaped_everywhere`, `test_pages_comply_with_strict_csp`, `test_injection_in_question_does_not_leak_facts`, `test_autotest_*` |
| A04 Insecure Design | Модель угроз (`03-THREAT-MODEL.md`); гейт до модели; запись — детерминированный сценарий без LLM | Документ + тесты бизнес-логики |
| A05 Misconfiguration | Прод не стартует с небезопасными настройками; docs/openapi отключены в prod; заголовки безопасности; контейнер без root, `read_only`, `cap_drop: ALL` | Проверено: `test_prod_config_refuses_unsafe_defaults`, `test_docs_disabled_in_prod`, `test_security_headers_and_cookie_flags`. Docker-настройки не запускались |
| A06 Vulnerable Components | Версии закреплены; `pip-audit` в CI | `pip-audit`: известных уязвимостей не найдено (дата прогона — в отчёте) |
| A07 Auth Failures | Лимиты входа (IP, email), блокировка 15 мин после 5 неудач, одинаковые ответы и выравнивание времени для несуществующего email | Проверено: `test_wrong_password_and_lockout`, `test_login_unknown_user_same_message`, `test_registration_rate_limit` |
| A08 Integrity Failures | Вебхуки с секретом; платёж перепроверяется по API; миграции под контролем версий | Проверено: `test_telegram_webhook_requires_secret`, `test_vk_confirmation_secret_and_reply`, `test_yookassa_webhook_does_not_trust_body` |
| A09 Logging Failures | Аудит входа/выхода/блокировок/каналов/удалений; токены и ПДн не логируются | Проверено частично: `test_audit_log_written_for_sensitive_actions`. Алертинг настраивает оператор |
| A10 SSRF | Краулер и проверка сайта: только http(s) 80/443, резолв и проверка IP, ручные редиректы ≤3, проверка IP сокета после подключения, лимиты размера/времени, robots.txt | Проверено: `test_unsafe_urls_rejected` (16 вариантов), `test_redirect_to_private_host_is_blocked`, `test_peer_ip_checked_after_connect_blocks_dns_rebinding` |

## Прочее
- **Загрузки:** только txt/md/pdf ≤ 2 МБ, проверка сигнатуры PDF, файл не сохраняется на диск, текст извлекается и режется на фрагменты (`test_upload_validation`).
- **Лимиты тела запроса:** 128 КБ (3 МБ для загрузки), `test_body_limit`.
- **Виджет:** разрешённые Origin на бизнес, лимиты, `textContent` вместо `innerHTML`.
- **Новые разделы кабинета (0.2.0):** изоляция арендаторов проверена для документов, исправлений ответов, карточки записи, заметки, переноса, ручной записи, выгрузок и истории клиента по телефону (`tests/test_cabinet.py`); CSV защищён от формул Excel; все страницы кабинета проходят проверку CSP.
- **Двойная запись:** блокировка строки ресурса + уникальный ключ; параллельный тест на PostgreSQL (`test_concurrency.py`).

## Остаточные риски (принятые или требующие внимания)
1. **Email не подтверждается, сброса пароля нет.** Возможны спам-аккаунты (ограничено лимитом 5 регистраций/час/IP); забытый пароль решает оператор вручную. Рекомендуется добавить подтверждение почты и сброс пароля до публичного запуска.
2. **Лимиты в памяти процесса.** При нескольких инстансах лимиты не общие; распределённый перебор паролей и DDoS закрывает WAF/CDN, а не приложение.
3. **Семантические ложные утверждения.** Guard проверяет числа, контакты, ссылки и «модальные» слова; нечисловое ложное утверждение («у нас есть Wi-Fi»), которого нет в базе, он поймает не всегда. Снижение риска: режим `extractive` (дословные выдержки), автопроверка, журнал с источниками.
4. **Поиск лексический.** Редкие синонимы и опечатки приводят к отказу (безопасная ошибка), а не к неверному ответу. Качество можно поднять эмбеддингами позже.
5. **DNS rebinding в краулере** смягчён проверкой IP сокета, но не исключён полностью при использовании HTTP-прокси.
6. **Telegram/VK/ЮKassa/LLM проверены на моках**, а не на боевых API.
7. **Нет независимого пентеста и нагрузочного теста.**
8. **Юридическая часть** (152-ФЗ, оферта, политика, согласие на обработку ПДн клиентов бизнеса, роль оператора ПДн) не проверялась юристом; соответствие не заявляется.
9. **Ключ `ENCRYPTION_KEY`:** потеря → токены каналов придётся ввести заново; компрометация → перевыпустить токены всех ботов.
