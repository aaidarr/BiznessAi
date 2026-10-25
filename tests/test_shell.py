"""The cabinet shell: sidebar groups, breadcrumbs, sign-in redirects and the home page."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from app.models import Business, Gap, KBItem, Lead, Resource, ResourceService, Service, WorkingHours
from app.services import booking, dashboard
from app.web import nav
from tests.helpers import PASSWORD, make_client, post, register

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"


@pytest.fixture
def client():
    return make_client()


@pytest.fixture
def owner(client, db):
    register(client)
    return client, db.scalar(select(Business))


def open_groups(html: str) -> set[str]:
    return set(re.findall(r'<details class="nav-group" data-nav-key="(\w+)" open>', html))


def all_groups(html: str) -> set[str]:
    return set(re.findall(r'<details class="nav-group" data-nav-key="(\w+)"', html))


# ---------------------------------------------------------------- navigation model


def active_leaf(tree):
    return [(g["key"], c["title"]) for g in tree for c in g["children"] if c["active"]]


def test_direct_items_open_without_groups():
    tree, crumbs = nav.build("/app", False, {})
    assert [g["key"] for g in tree if g["active"]] == ["today"] and not any(g["open"] for g in tree)
    assert crumbs == []
    tree, crumbs = nav.build("/app/reports", False, {})
    assert [g["key"] for g in tree if g["active"]] == ["reports"] and crumbs == [("Отчёты", "/app/reports")]


def test_the_parent_group_opens_for_the_current_page():
    for path, group, leaf in [
        ("/app/calendar", "calendar", "Записи"),
        ("/app/calendar/setup", "calendar", "Услуги и расписание"),
        ("/app/conversations", "people", "Диалоги"),
        ("/app/leads", "people", "Заявки"),
        ("/app/bot/answers", "bot", "Ответы"),
        ("/app/bot/templates/clinic", "bot", "Шаблоны"),
        ("/app/test", "bot", "Проверка"),
        ("/app/billing", "settings", "Тариф"),
        ("/app/channels", "settings", "Каналы"),
    ]:
        tree, _ = nav.build(path, False, {})
        assert [g["key"] for g in tree if g["open"]] == [group], path
        assert active_leaf(tree) == [(group, leaf)], path


def test_most_specific_leaf_wins():
    tree, _ = nav.build("/app/calendar/setup", False, {})
    assert active_leaf(tree) == [("calendar", "Услуги и расписание")]  # not also "Записи"
    tree, _ = nav.build("/app/calendar/booking/7", False, {})
    assert active_leaf(tree) == [("calendar", "Записи")]


def test_pages_outside_any_leaf_still_open_their_section():
    tree, crumbs = nav.build("/app/bot/fix/12", False, {})
    assert [g["key"] for g in tree if g["open"]] == ["bot"] and active_leaf(tree) == []
    assert crumbs == [("Бот", "")]


def test_breadcrumbs():
    _, crumbs = nav.build("/app/calendar/setup", False, {})
    assert crumbs == [("Календарь", ""), ("Услуги и расписание", "/app/calendar/setup")]
    _, crumbs = nav.build("/app/test", False, {})
    assert crumbs == [("Бот", ""), ("Проверка", "/app/test")]


def test_admin_item_only_for_admins_and_counters():
    assert "admin" not in [g["key"] for g in nav.build("/app", False, {})[0]]
    tree, _ = nav.build("/admin", True, {})
    assert [g["key"] for g in tree if g["active"]] == ["admin"]
    tree, _ = nav.build("/app/leads", False, {"leads": 3, "pending": 2, "gaps": 0})
    badges = {c["title"]: c["badge"] for g in tree for c in g["children"]}
    assert badges["Заявки"] == 3 and badges["Записи"] == 2 and badges["Вопросы без ответа"] == 0


def test_every_cabinet_url_belongs_to_some_navigation_item():
    leaves = {c.href for g in nav.NAV for c in g.children} | {g.href for g in nav.NAV if g.href}
    for must in [
        "/app",
        "/app/calendar",
        "/app/calendar/setup",
        "/app/conversations",
        "/app/leads",
        "/app/gaps",
        "/app/bot/answers",
        "/app/bot/documents",
        "/app/bot/templates",
        "/app/bot/personality",
        "/app/test",
        "/app/reports",
        "/app/settings",
        "/app/channels",
        "/app/billing",
        "/admin",
    ]:
        assert must in leaves, must


# ---------------------------------------------------------------- rendered shell


def test_shell_is_only_for_signed_in_users(client):
    html = client.get("/").text
    assert 'class="sidebar"' not in html and 'class="app-shell"' not in html and 'href="/register"' in html
    assert 'class="sidebar"' not in client.get("/login").text


def test_sidebar_markup_and_accessibility(owner):
    c, _ = owner
    html = c.get("/app").text
    assert 'class="sidebar"' in html and 'aria-label="Главное меню"' in html
    assert all_groups(html) == {"calendar", "people", "bot", "settings"}
    assert open_groups(html) == set()  # the dashboard has no sub-pages: every group is collapsed
    assert 'aria-current="page"' in html and 'class="nav-item is-active"' in html
    assert 'class="skip-link"' in html and 'id="main"' in html
    assert "data-nav-toggle" in html and 'aria-controls="sidebar"' in html and "data-nav-close" in html
    assert 'id="confirm-dialog"' in html


def test_only_the_current_section_is_expanded(owner):
    c, _ = owner
    assert open_groups(c.get("/app/bot/answers").text) == {"bot"}
    assert open_groups(c.get("/app/calendar/setup").text) == {"calendar"}
    assert open_groups(c.get("/app/billing").text) == {"settings"}
    html = c.get("/app/bot/answers").text
    assert re.search(r'<a class="nav-sub-item is-active" href="/app/bot/answers" aria-current="page">', html)
    assert 'href="/app/bot/documents" class' not in html and html.count("is-active") >= 1


def test_breadcrumbs_are_rendered(owner):
    c, _ = owner
    html = c.get("/app/calendar/setup").text
    crumbs = re.search(r'<nav class="crumbs".*?</nav>', html, re.S).group(0)
    assert crumbs.index("Главная") < crumbs.index("Календарь") < crumbs.index("Услуги и расписание")
    assert crumbs.count('aria-current="page"') == 1
    assert '<span aria-current="page">Главная</span>' in re.search(
        r'<nav class="crumbs".*?</nav>', c.get("/app").text, re.S
    ).group(0)
    tpl = re.search(r'<nav class="crumbs".*?</nav>', c.get("/app/bot/templates/clinic").text, re.S).group(0)
    assert "Шаблоны" in tpl and "Стоматология" in tpl


def test_account_menu(owner):
    c, _ = owner
    html = c.get("/app").text
    assert "data-account" in html and "Выйти" in html and "a@example.com" in html
    assert re.search(r'class="avatar"[^>]*>\s*A@\s*<', html)  # first two characters of the e-mail


def test_new_pages_get_breadcrumb_tails(owner, db):
    c, biz = owner
    assert "Исправление ответа" not in c.get("/app").text
    svc = Service(business_id=biz.id, name="Стрижка", duration_min=60)
    res = Resource(business_id=biz.id, name="Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=600, end_min=1080))
    db.commit()
    start = booking.compute_slots(db, biz, svc)[2].start_utc
    b = booking.create_booking(db, biz, svc, start, "Мария", "+79001234567", "owner", None, by_owner=True)
    db.commit()
    html = c.get(f"/app/calendar/booking/{b.id}").text
    assert f"Запись #{b.id}" in html and open_groups(html) == {"calendar"}


# ---------------------------------------------------------------- sign-in behaviour


def test_login_always_lands_on_the_dashboard(client):
    register(client)
    post(client, "/logout")
    r = client.post("/login", data={"email": "a@example.com", "password": PASSWORD})
    assert r.status_code == 303 and r.headers["location"] == "/app"
    r = client.post("/login", data={"email": "a@example.com", "password": PASSWORD})  # a second sign-in too
    assert r.headers["location"] == "/app"


def test_signed_in_user_is_sent_away_from_login_and_register(client):
    register(client)
    for path in ("/login", "/register"):
        r = client.get(path)
        assert r.status_code == 303 and r.headers["location"] == "/app", path


def test_moving_between_pages_never_bounces_to_the_dashboard(client):
    register(client)
    for path in ["/app/reports", "/app/bot/answers", "/app/calendar/setup", "/app/settings", "/app/reports"]:
        r = client.get(path)
        assert r.status_code == 200, path  # no redirect: the user stays where they asked to be


def test_guests_and_signed_out_users_go_to_login(client):
    assert client.get("/app/reports").headers["location"] == "/login"
    register(client)
    assert client.get("/app/reports").status_code == 200
    post(client, "/logout")
    r = client.get("/app")
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/login").status_code == 200  # signed out: the form is shown again


# ---------------------------------------------------------------- dashboard


def test_dashboard_of_a_new_business_shows_honest_empty_states(owner):
    c, _ = owner
    html = c.get("/app").text
    assert "Главная" in html and "Добр" in html  # greeting
    assert "Событий пока нет" in html and "Записей пока нет" in html
    assert re.search(
        r'Ответов из базы</span>\s*<span class="kpi-line"><span class="kpi-value">—</span>', html
    )  # no invented percentage
    assert "У бота нет ни одного опубликованного ответа" in html
    assert "Календарь не настроен" in html and "Каналы выключены" in html
    for link in [
        "/app/bot/answers",
        "/app/bot/templates",
        "/app/bot/documents",
        "/app/reports",
        "/app/test",
        "/app/calendar",
    ]:
        assert f'href="{link}"' in html


def test_dashboard_shows_real_events_and_needs_attention(owner, db):
    c, biz = owner
    db.add(Lead(business_id=biz.id, name="Иван", phone="+79001112233", comment="цены"))
    db.add(Gap(business_id=biz.id, key="парковк", sample="Есть ли парковка?", count=3))
    db.add(KBItem(business_id=biz.id, kind="faq", title="Режим", text="Работаем с 10:00 до 22:00", published=False))
    db.commit()
    html = c.get("/app").text
    assert "Заявка: +79001112233 Иван" in html and "Нет ответа: «Есть ли парковка?»" in html
    # six things need attention; the page shows the four most urgent and counts the rest
    assert "Новых заявок без ответа: 1" in html and "Вопросов, на которые бот не знал ответа: 1" in html
    assert "У бота нет ни одного опубликованного ответа" in html  # errors come first
    assert "Неопубликованных ответов" not in html and "Ещё 2 пункта" in html
    assert re.search(r'<h2 id="h-attn">Требует внимания</h2><span class="count">6</span>', html)
    # counters in the sidebar come from the same real rows
    assert re.search(r'href="/app/leads"[^>]*>\s*<span>Заявки</span><span class="count">1</span>', html)
    assert "Событий пока нет" not in html


def test_dashboard_pending_booking_and_plan_warning(owner, db, monkeypatch):
    from app.services import billing

    c, biz = owner
    biz.auto_confirm_bookings = False
    svc = Service(business_id=biz.id, name="Стрижка", duration_min=60, price_rub=800)
    res = Resource(business_id=biz.id, name="Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=600, end_min=1080))
    db.commit()
    booking.create_booking(
        db, biz, svc, booking.compute_slots(db, biz, svc)[1].start_utc, "Мария", "+79001234567", "widget", None
    )
    db.commit()
    monkeypatch.setitem(billing.PLANS, "free", billing.Plan("free", "Бесплатный", 0, 0))
    html = c.get("/app").text
    assert "Записей ждут подтверждения: 1" in html and "Лимит диалогов исчерпан" in html
    assert "Мария" in html and "Стрижка" in html  # upcoming bookings list


def test_dashboard_greeting_by_time_of_day():
    assert dashboard.greeting(7) == "Доброе утро" and dashboard.greeting(13) == "Добрый день"
    assert dashboard.greeting(20) == "Добрый вечер" and dashboard.greeting(2) == "Доброй ночи"


def test_relative_time_labels():
    from zoneinfo import ZoneInfo

    now = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
    tz = ZoneInfo("Europe/Moscow")
    assert dashboard._ago(now, now - timedelta(seconds=10), tz) == "только что"
    assert dashboard._ago(now, now - timedelta(minutes=5), tz) == "5 мин назад"
    assert dashboard._ago(now, now - timedelta(hours=3), tz) == "3 ч назад"
    assert dashboard._ago(now, now - timedelta(days=2), tz) == "23.10 15:00"


# ---------------------------------------------------------------- design-system guards


def test_styles_have_the_documented_tokens_and_no_leftovers():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    for token in ["--accent:", "--surface:", "--line:", "--r:", "--sidebar:", "--topbar:"]:
        assert token in css
    for cls in [
        ".btn-primary",
        ".btn-secondary",
        ".btn-danger",
        ".is-loading",
        "dialog.modal",
        ".nav-group",
        ".crumbs",
        ".kpi",
    ]:
        assert cls.split()[0] in css or cls in css, cls
    assert "prefers-color-scheme: dark" not in css  # the product is a single light theme
    assert "gradient" not in css and "backdrop-filter" not in css  # no decorative effects


def test_script_implements_the_interactions_the_markup_promises():
    js = (STATIC / "cabinet.js").read_text(encoding="utf-8")
    for needle in ["showModal", "nav-open", "is-loading", "Escape", "aria-expanded", "data-ask", "other.open = false"]:
        assert needle in js, needle
    assert "localStorage" not in js  # the accordion needs no memory: the server opens the section of the current page
    assert "innerHTML" not in js and "eval(" not in js


def test_destructive_forms_use_the_modal(owner, db):
    c, biz = owner
    db.add(KBItem(business_id=biz.id, kind="faq", title="x", text="y", published=True))
    db.commit()
    html = c.get("/app/bot/answers").text
    assert re.search(r'<form method="post" action="/app/bot/answers/\d+/delete" data-confirm="[^"]+">', html)


# ---------------------------------------------------------------- one-screen home page


def test_home_lists_are_capped_so_the_page_stays_on_one_screen(owner, db):
    c, biz = owner
    svc = Service(business_id=biz.id, name="Стрижка", duration_min=30)
    res = Resource(business_id=biz.id, name="Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=0, end_min=1440))
    for i in range(9):
        db.add(Lead(business_id=biz.id, name=f"Клиент{i}", phone=f"+7900000000{i}", comment="x"))
    db.commit()
    for k in range(6):
        booking.create_booking(
            db,
            biz,
            svc,
            booking.compute_slots(db, biz, svc)[k * 3].start_utc,
            f"Гость{k}",
            "+79001234567",
            "owner",
            None,
            by_owner=True,
        )
    db.commit()
    html = c.get("/app").text
    events = re.search(r'<h2 id="h-ev">.*?</section>', html, re.S).group(0)
    upcoming = re.search(r'<h2 id="h-up">.*?</section>', html, re.S).group(0)
    attention = re.search(r'<h2 id="h-attn">.*?</section>', html, re.S).group(0)
    assert events.count('<li class="list-row">') == 4
    assert upcoming.count('<li class="list-row">') == 3
    assert attention.count('<li class="list-row">') <= 4
    assert "/app/conversations" in events and "/app/calendar" in upcoming  # links to the full lists


def test_home_empty_states_are_one_short_line(owner):
    c, _ = owner
    html = c.get("/app").text
    assert re.search(
        r'<p class="empty-line">Событий пока нет\. <a href="/app/channels">Подключить каналы</a></p>', html
    )
    assert re.search(
        r'<p class="empty-line">Записей пока нет\. <a href="/app/calendar">Открыть календарь</a></p>', html
    )
    assert "empty-state" not in html  # the tall empty panels are gone from the home page


def test_home_shows_bot_status(owner, db):
    c, biz = owner
    assert "У бота нет ответов" in c.get("/app").text
    db.add(KBItem(business_id=biz.id, kind="faq", title="Режим", text="Работаем с 10:00", published=True))
    db.commit()
    assert "Бот на паузе: каналы выключены" in c.get("/app").text
    biz.channels_enabled = True
    db.commit()
    html = c.get("/app").text
    assert "Бот отвечает клиентам" in html and 'class="status status-ok"' in html


def test_home_plan_is_one_line_in_the_header_with_real_plan_data(owner, db):
    c, biz = owner

    def chip(html: str) -> str:
        return re.search(r'<a class="plan-chip[^"]*" href="/app/billing" title="([^"]*)">([^<]*)</a>', html).groups()

    title, text = chip(c.get("/app").text)
    assert "Тариф «Бесплатный»" in text and "0 из 100 диалогов" in text and "без срока" in title
    biz.plan, biz.plan_until = "start", datetime.now(UTC) + timedelta(days=10)
    db.commit()
    title, text = chip(c.get("/app").text)
    assert "Тариф «Старт»" in text and "из 1000 диалогов" in text and "оплачен до" in title
    biz.plan_until = datetime.now(UTC) - timedelta(days=1)
    db.commit()
    title, text = chip(c.get("/app").text)
    assert "Тариф «Бесплатный»" in text and "срок истёк" in title  # an expired plan falls back honestly
    assert 'class="panel plan"' not in c.get("/app").text  # the plan no longer takes a block of its own


def test_home_charts_show_real_14_day_activity_and_honest_empty_state(owner, db):
    c, biz = owner
    html = c.get("/app").text
    assert "Записи и диалоги за 14 дней" in html and "ещё не было" in html and 'class="spark"' not in html
    from app.models import Conversation

    db.add(Conversation(business_id=biz.id, channel="widget", external_id="x1"))
    db.commit()
    html = c.get("/app").text
    assert 'class="chart-svg"' in html and "Диалоги: <b>1</b>" in html and "Записи: <b>0</b>" in html
    assert html.count('class="spark"') >= 1  # the dialogs card got a sparkline
    assert "style=" not in html  # CSP: charts are plain SVG attributes


def test_series_helpers_count_per_local_day():
    from datetime import date

    tz = ZoneInfo("Europe/Moscow")
    today = date(2026, 11, 14)
    at = [
        datetime(2026, 11, 14, 5, 0, tzinfo=UTC),
        datetime(2026, 11, 14, 22, 30, tzinfo=UTC),
        datetime(2026, 10, 31, 5, 0, tzinfo=UTC),
    ]
    v = dashboard._per_day(at, tz, today)
    assert len(v) == 14 and v[-1] == 1 and v[0] == 0  # 22:30 UTC is already the 15th in Moscow, 31 Oct is out of range
    sp = dashboard._spark(v, date(2026, 11, 1))
    assert len(sp["bars"]) == 14 and sp["bars"][-1]["tip"] == "14.11: 1"


def test_home_layout_never_uses_zoom_or_hidden_overflow_tricks():
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    dash = css[css.index("Home page: one-screen layout") : css.index("/* ---------- page tabs")]
    for forbidden in ["zoom:", "scale(", "height: 100vh", "max-height"]:
        assert forbidden not in dash.replace("min-height", ""), forbidden
    # `overflow: hidden` is only allowed to truncate one line of text with an ellipsis, never to clip a container
    for rule in re.findall(r"\{([^}]*)\}", dash):
        if "overflow: hidden" in rule:
            assert "text-overflow: ellipsis" in rule, rule
