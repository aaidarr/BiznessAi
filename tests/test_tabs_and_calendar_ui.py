"""Page tabs, the accordion sidebar and the compact calendar with click-to-book."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import Booking, Business, Lead, Resource, ResourceService, Service, User, WorkingHours
from app.services import booking
from tests.helpers import make_client, post, register

STATIC = Path(__file__).resolve().parents[1] / "app" / "web" / "static"


@pytest.fixture
def client():
    return make_client()


@pytest.fixture
def owner(client, db):
    register(client)
    return client, db.scalar(select(Business))


def tabs_of(html: str) -> list[tuple[str, str]]:
    m = re.search(r'<div class="tabs" data-tabs.*?</div>\s*</div>', html, re.S)
    assert m, "no page tabs"
    return re.findall(r'<a href="#([\w-]+)" id="tab-[\w-]+" aria-controls="[\w-]+">([^<]+)</a>', m.group(0))


def default_of(html: str) -> str:
    return re.search(r'data-tabs data-default="([^"]*)"', html).group(1)


def check_tabs(html: str) -> list[str]:
    items = tabs_of(html)
    assert len(items) >= 2
    for target, label in items:
        assert len(re.findall(rf'\bid="{re.escape(target)}"', html)) == 1, (
            f"tab «{label}» needs exactly one panel #{target}"
        )
    return [label for _, label in items]


def add_calendar(db, biz, hours=(600, 1080)):
    svc = Service(business_id=biz.id, name="Стрижка", duration_min=60, price_rub=800)
    res = Resource(business_id=biz.id, name="Анна")
    db.add_all([svc, res])
    db.flush()
    db.add(ResourceService(resource_id=res.id, service_id=svc.id))
    for wd in range(7):
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=hours[0], end_min=hours[1]))
    db.commit()
    return svc, res


# ---------------------------------------------------------------- sidebar accordion


def test_server_opens_only_the_current_section_and_script_closes_the_rest():
    js = (STATIC / "cabinet.js").read_text(encoding="utf-8")
    assert 'addEventListener("toggle"' in js and "other.open = false" in js
    assert "localStorage" not in js


@pytest.mark.parametrize(
    "path", ["/app", "/app/bot/answers", "/app/calendar", "/app/leads", "/app/billing", "/app/reports"]
)
def test_never_more_than_one_section_is_open(owner, path):
    c, _ = owner
    html = c.get(path).text
    assert len(re.findall(r'<details class="nav-group" data-nav-key="\w+" open>', html)) <= 1


# ---------------------------------------------------------------- tabs


def test_reports_are_split_into_five_tabs_in_the_right_order(owner):
    c, _ = owner
    html = c.get("/app/reports").text
    assert check_tabs(html) == ["Обзор", "Каналы и ответы", "Услуги и время", "Темы и пробелы", "Тариф и выгрузка"]
    order = [
        'id="sec-overview"',
        "Диалоги по дням",
        'id="sec-channels"',
        "Что отвечал бот",
        'id="sec-svc"',
        "Записи по услугам",
        "Обращения по часам",
        'id="sec-topics"',
        "Чего не хватает",
        'id="sec-plan"',
        "Выгрузка в таблицу",
    ]
    positions = [html.index(x) for x in order]
    assert positions == sorted(positions)
    assert default_of(html) == ""  # the first tab is shown


def test_other_long_pages_use_tabs_too(owner, db):
    c, _ = owner
    assert check_tabs(c.get("/app/channels").text) == ["Публикация", "Виджет", "Telegram", "VK"]
    assert check_tabs(c.get("/app/calendar/setup").text) == ["Услуги", "Специалисты"]
    assert check_tabs(c.get("/app/bot/templates/entertainment").text) == ["Факты о бизнесе", "Услуги", "Параметры"]
    assert check_tabs(c.get("/app/bot/templates/universal").text) == ["Факты о бизнесе", "Параметры"]
    u = db.scalar(select(User))
    u.is_admin = True
    db.commit()
    assert check_tabs(c.get("/admin").text) == ["Бизнесы", "Журнал аудита"]


def test_template_form_keeps_one_form_around_every_tab(owner):
    c, _ = owner
    html = c.get("/app/bot/templates/entertainment").text
    form = re.search(r'<form method="post" action="/app/bot/templates/entertainment".*?</form>', html, re.S).group(0)
    for panel in ("sec-fields", "sec-services", "sec-params"):
        assert f'id="{panel}"' in form  # hidden panels are still submitted together
    assert 'name="svc_0_on"' in form and 'name="publish"' in form
    assert re.search(r'<button type="submit" form="tpl-form">Создать ответы</button>', html)  # submit from any tab


def test_answers_page_has_list_and_add_tabs(owner):
    c, _ = owner
    html = c.get("/app/bot/answers").text
    labels = check_tabs(html)
    assert labels[0].startswith("Ответы (") and labels[1] == "Добавить ответ"
    add = re.search(r'<div id="sec-add" class="card">.*?</form>', html, re.S).group(0)
    assert 'action="/app/bot/answers/add"' in add and "<details" not in add  # a real tab, not a folded block
    lst = html[html.index('id="sec-list"') : html.index('id="sec-add"')]
    assert 'role="search"' in lst and "Найдено:" in lst


def test_documents_default_tab_depends_on_having_sources(owner, db):
    c, _ = owner
    assert default_of(c.get("/app/bot/documents").text) == "sec-add"  # nothing yet: start with adding
    tok = re.search(r'name="csrf" value="([^"]+)"', c.get("/app/bot/documents").text).group(1)
    c.post(
        "/app/bot/documents/upload",
        data={"csrf": tok},
        files={"file": ("a.txt", "Работаем с 10:00 до 22:00.".encode())},
    )
    html = c.get("/app/bot/documents").text
    assert check_tabs(html) == ["Источники", "Добавить источник"] and default_of(html) == "sec-sources"


def test_test_page_opens_on_the_result_after_an_autotest(owner):
    c, _ = owner
    post(
        c,
        "/app/bot/answers/add",
        {"title": "Режим работы", "text": "Работаем с 10:00 до 22:00.", "publish": "1"},
        "/app/bot/answers",
    )
    plain = c.get("/app/test").text
    assert check_tabs(plain) == ["Чат", "Автопроверка"] and default_of(plain) == "sec-chat"
    result = post(c, "/app/test/autotest", {}, "/app/test").text
    assert check_tabs(result) == ["Чат", "Автопроверка", "Результат"] and default_of(result) == "sec-results"


def test_booking_card_tabs_follow_the_booking_state(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    b = booking.create_booking(
        db,
        biz,
        svc,
        booking.compute_slots(db, biz, svc)[2].start_utc,
        "Мария",
        "+79001234567",
        "owner",
        None,
        by_owner=True,
    )
    db.commit()
    assert check_tabs(c.get(f"/app/calendar/booking/{b.id}").text) == ["Запись", "Перенос", "Заметка"]
    booking.cancel_booking(b)
    db.commit()
    assert check_tabs(c.get(f"/app/calendar/booking/{b.id}").text) == ["Запись", "Заметка"]


def test_tab_ids_are_unique_on_every_page(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    u = db.scalar(select(User))
    u.is_admin = True
    db.commit()
    for path in [
        "/app/reports",
        "/app/channels",
        "/app/bot/documents",
        "/app/bot/templates/entertainment",
        "/app/calendar/setup",
        "/app/bot/answers",
        "/app/test",
        "/admin",
        "/app/calendar",
    ]:
        ids = re.findall(r'\bid="([\w-]+)"', c.get(path).text)
        assert len(ids) == len(set(ids)), (path, sorted({i for i in ids if ids.count(i) > 1}))


def test_tabs_are_progressive_enhancement(owner):
    css = (STATIC / "app.css").read_text(encoding="utf-8")
    js = (STATIC / "cabinet.js").read_text(encoding="utf-8")
    assert (
        "[hidden] { display: none !important; }" in css
        and ".tabs-list a[aria-selected" in css
        and "position: sticky" in css[css.index(".tabs {") :]
    )
    for needle in [
        "data-tabs",
        "panel.hidden",
        "aria-selected",
        "tabpanel",
        "ArrowRight",
        "hashchange",
        "replaceState",
    ]:
        assert needle in js.replace("x.panel.hidden", "panel.hidden"), needle
    assert js.count(".scrollIntoView(") <= 2  # never on the sticky bar itself (it drags the page)
    c, _ = owner
    html = c.get("/app/channels").text
    assert all(
        f'id="{p}"' in html for p in ("sec-publish", "sec-widget", "sec-telegram", "sec-vk")
    )  # no-JS: everything is in the page
    assert "hidden" not in re.search(r'id="sec-widget"[^>]*>', html).group(0)  # the server never hides a panel


# ---------------------------------------------------------------- compact calendar toolbar


def test_calendar_toolbar_is_one_row_with_inline_controls(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    html = c.get("/app/calendar").text
    bar = re.search(r'<div class="cal-bar">.*?</div>\n\n', html, re.S).group(0)
    for part in [
        'class="cal-nav"',
        'class="cal-title"',
        'name="resource"',
        'name="cancelled"',
        'class="seg"',
        "data-new-booking",
    ]:
        assert part in bar, part
    assert "data-autosubmit" in bar
    assert re.findall(r"Применить", bar) == ["Применить"] and "<noscript>" in bar  # only as a no-JS fallback
    assert (
        'class="card toolbar"' not in html and "Загрузка</a>" not in html
    )  # the tall toolbar card and its tabs are gone
    assert "Услуги и расписание" in bar and 'href="/app/calendar/setup"' in bar


def test_week_grid_carries_what_a_click_needs(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    day = date.today().isoformat()
    html = c.get(f"/app/calendar?view=week&date={day}").text
    grid = re.search(r'<div class="calgrid[^"]*" role="table"[^>]*>', html).group(0)
    assert 'data-start-min="600"' in grid and 'data-step="30"' in grid
    cols = re.findall(r'<div class="dcol [^"]*" data-date="(\d{4}-\d{2}-\d{2})"></div>', html)
    assert len(cols) == 7 and cols == sorted(cols)


def test_month_cells_know_their_date(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    html = c.get(f"/app/calendar?view=month&date={date.today().isoformat()}").text
    cells = re.findall(r'<div class="mcell[^"]*" role="cell" data-date="(\d{4}-\d{2}-\d{2})">', html)
    assert len(cells) in (28, 35, 42) and len(set(cells)) == len(cells)


def test_new_booking_dialog_has_every_field_and_the_quick_view_exists(owner, db):
    c, biz = owner
    add_calendar(db, biz)
    html = c.get("/app/calendar").text
    dlg = re.search(r'<dialog id="new-booking".*?</dialog>', html, re.S).group(0)
    for field in [
        'name="name"',
        'name="phone"',
        'name="service_id"',
        'name="resource_id"',
        'name="day"',
        'name="at"',
        'name="note"',
        'name="csrf"',
    ]:
        assert field in dlg, field
    assert 'id="nb-slots"' in dlg and 'data-duration="60"' in dlg and 'action="/app/calendar/new"' in dlg
    assert "Любой свободный" in dlg
    assert '<dialog id="booking-quick"' in html and 'id="bq-actions"' in html


def test_booking_dialog_without_services_points_to_setup(owner):
    c, _ = owner
    html = c.get("/app/calendar").text
    dlg = re.search(r'<dialog id="new-booking".*?</dialog>', html, re.S).group(0)
    assert 'id="nb-form"' not in dlg and 'href="/app/calendar/setup"' in dlg


def test_booking_links_carry_data_for_the_quick_view(owner, db):
    c, biz = owner
    svc, _ = add_calendar(db, biz)
    b = booking.create_booking(
        db,
        biz,
        svc,
        booking.compute_slots(db, biz, svc)[2].start_utc,
        'Мария "Ж"',
        "+79001234567",
        "owner",
        None,
        by_owner=True,
        note="любит чай",
    )
    db.commit()
    day = b.start_utc.astimezone(booking.tz_of(biz)).date().isoformat()
    html = c.get(f"/app/calendar?view=week&date={day}").text
    ev = re.search(r'<a class="ev [^>]*data-booking[^>]*>', html).group(0)
    assert f'data-href="/app/calendar/booking/{b.id}"' in ev and 'data-service="Стрижка"' in ev
    assert 'data-client="Мария &#34;Ж&#34;"' in ev  # attribute values are escaped
    assert (
        'data-phone="+79001234567"' in ev
        and 'data-status="confirmed"' in ev
        and 'data-note="любит чай"' in ev
        and 'data-price="800"' in ev
    )
    assert f'href="/app/calendar/booking/{b.id}"' in ev  # a plain link underneath: works without JS and with ctrl-click


def test_script_handles_click_to_book_quick_view_and_shortcut():
    js = (STATIC / "cabinet.js").read_text(encoding="utf-8")
    for needle in [
        "data-new-booking",
        "slot-ghost",
        "slot-hover",
        "newDlg.show()",
        "data-start-min",
        ".dcol",
        ".mcell",
        "data-booking",
        "quickDlg.showModal",
        "newDlg.showModal",
        'e.key === "n"',
        "data-slots-url",
        "ctrlKey",
        "data-autosubmit",
    ]:
        assert needle in js, needle
    assert "innerHTML" not in js  # client names are placed with textContent only


# ---------------------------------------------------------------- free times endpoint


def test_free_times_endpoint_lists_the_day_and_hides_taken_and_past_times(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    day = (datetime.now(UTC) + timedelta(days=3)).astimezone(booking.tz_of(biz)).date()
    r = c.get(f"/app/calendar/slots?service_id={svc.id}&day={day.isoformat()}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    times = r.json()["times"]
    assert times[0] == "10:00" and times[-1] == "17:00" and len(times) == 15  # 60 min service, 30 min step, 10-18
    local = datetime(day.year, day.month, day.day, 12, 0, tzinfo=booking.tz_of(biz))
    booking.create_booking(db, biz, svc, local.astimezone(UTC), "Мария", "+79001234567", "owner", None, by_owner=True)
    db.commit()
    taken = c.get(f"/app/calendar/slots?service_id={svc.id}&day={day.isoformat()}").json()["times"]
    assert (
        "12:00" not in taken and "11:30" not in taken and "12:30" not in taken and "11:00" in taken and "13:00" in taken
    )
    past = c.get(f"/app/calendar/slots?service_id={svc.id}&day=2020-01-01").json()["times"]
    assert past == []  # nothing can be booked in the past


def test_owner_free_times_ignore_the_lead_time_but_not_the_clock(db, biz):
    svc, _ = add_calendar(db, biz)
    biz.min_lead_minutes = 24 * 60
    db.commit()
    tz = booking.tz_of(biz)
    now = datetime(2026, 11, 2, 7, 0, tzinfo=UTC)  # 10:00 in Moscow
    day = now.astimezone(tz).date()
    times = booking.day_free_times(db, biz, svc, day, now=now)
    assert (
        times[0] == "10:30" and times[-1] == "17:00"
    )  # the bot would offer nothing today; the owner may book from now on
    assert booking.compute_slots(db, biz, svc, now)[0].start_utc > now + timedelta(hours=20)


def test_free_times_resource_filter_and_validation(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    other = Resource(business_id=biz.id, name="Олег")
    db.add(other)
    db.flush()
    db.add(ResourceService(resource_id=other.id, service_id=svc.id))
    db.add(WorkingHours(resource_id=other.id, weekday=2, start_min=840, end_min=960))  # Wed 14-16 only
    db.commit()
    wed = next(
        date.today() + timedelta(days=i) for i in range(1, 15) if (date.today() + timedelta(days=i)).weekday() == 2
    )
    q = f"service_id={svc.id}&day={wed.isoformat()}"
    assert c.get(f"/app/calendar/slots?{q}&resource_id={other.id}").json()["times"] == ["14:00", "14:30", "15:00"]
    assert len(c.get(f"/app/calendar/slots?{q}&resource_id={res.id}").json()["times"]) == 15
    assert c.get(f"/app/calendar/slots?service_id={svc.id}&day=not-a-date").status_code == 422
    assert c.get(f"/app/calendar/slots?service_id={svc.id}&day=9999-12-31").status_code == 422
    assert c.get(f"/app/calendar/slots?service_id={svc.id}").status_code == 422


def test_free_times_endpoint_is_private_and_tenant_scoped(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    biz_b = db.scalar(select(Business).where(Business.name == "B"))
    svc_a, res_a = add_calendar(db, biz_a)
    day = (date.today() + timedelta(days=2)).isoformat()
    assert make_client().get(f"/app/calendar/slots?service_id={svc_a.id}&day={day}").status_code == 303  # not signed in
    assert b.get(f"/app/calendar/slots?service_id={svc_a.id}&day={day}").status_code == 404  # someone else's service
    svc_b, _ = add_calendar(db, biz_b)
    assert (
        b.get(f"/app/calendar/slots?service_id={svc_b.id}&day={day}&resource_id={res_a.id}").status_code == 404
    )  # someone else's specialist


# ---------------------------------------------------------------- client autocomplete


def test_booking_form_suggests_known_clients_of_this_business_only(db):
    a, b = make_client(), make_client()
    register(a, "a@example.com", "A")
    register(b, "b@example.com", "B")
    biz_a = db.scalar(select(Business).where(Business.name == "A"))
    biz_b = db.scalar(select(Business).where(Business.name == "B"))
    svc, _ = add_calendar(db, biz_a)
    booking.create_booking(
        db,
        biz_a,
        svc,
        booking.compute_slots(db, biz_a, svc)[2].start_utc,
        "Мария",
        "+79001234567",
        "owner",
        None,
        by_owner=True,
    )
    db.add(Lead(business_id=biz_a.id, name="Олег", phone="+79005553322", comment="x"))
    db.add(Lead(business_id=biz_a.id, name="Мария-дубль", phone="+79001234567", comment="x"))  # same phone: listed once
    db.add(Lead(business_id=biz_b.id, name="ЧужойКлиент", phone="+79990000000", comment="x"))
    db.commit()
    html = a.get("/app/calendar").text
    options = re.findall(
        r'<option value="([^"]+)" data-phone="([^"]*)"></option>',
        re.search(r'<datalist id="nb-clients">.*?</datalist>', html, re.S).group(0),
    )
    assert sorted(options) == [("Мария", "+79001234567"), ("Олег", "+79005553322")]
    assert "ЧужойКлиент" not in html and "ЧужойКлиент" not in b.get("/app/calendar").text.replace("</datalist>", "")


def test_manual_booking_from_the_dialog_still_works_end_to_end(owner, db):
    c, biz = owner
    svc, res = add_calendar(db, biz)
    start = booking.compute_slots(db, biz, svc)[3].start_utc.astimezone(booking.tz_of(biz))
    r = post(
        c,
        "/app/calendar/new",
        {
            "service_id": str(svc.id),
            "resource_id": str(res.id),
            "day": start.date().isoformat(),
            "at": start.strftime("%H:%M"),
            "name": "Иван",
            "phone": "8 900 111-22-33",
        },
        "/app/calendar",
    )
    assert r.status_code == 303
    b = db.scalar(select(Booking))
    assert (b.client_name, b.channel, b.resource_id, b.client_phone) == ("Иван", "owner", res.id, "+79001112233")


def test_week_grid_shades_days_off_and_lunch_break(db, kb_biz):
    from datetime import date

    from app.models import Resource, WorkingHours
    from app.services import calview

    res = Resource(business_id=kb_biz.id, name="Анна")
    db.add(res)
    db.flush()
    for wd in range(5):  # Mon-Fri 10-13 and 14-19
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=600, end_min=780))
        db.add(WorkingHours(resource_id=res.id, weekday=wd, start_min=840, end_min=1140))
    db.add(WorkingHours(resource_id=res.id, weekday=5, start_min=660, end_min=900))  # Sat 11-15
    db.commit()
    v = calview.week_view(db, kb_biz, date(2026, 11, 4))
    assert v["start_min"] == 600 and v["rows"] == 18  # grid follows the opening hours: 10:00-19:00
    kinds = {(b["col"], b["kind"]) for b in v["closed"]}
    assert (2, "break") in kinds  # Monday lunch 13:00-14:00
    assert (7, "off") in kinds and (8, "off") in kinds  # Saturday outside 11-15, Sunday closed
    lunch = next(b for b in v["closed"] if b["col"] == 2 and b["kind"] == "break")
    assert (lunch["row"], lunch["span"]) == (2 + 6, 2)
