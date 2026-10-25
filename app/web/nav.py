"""Cabinet navigation: one source of truth for the sidebar, the active item and breadcrumbs."""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import func, select

from app.models import Booking, Business, Lead, utcnow


@dataclass(frozen=True)
class Leaf:
    title: str
    href: str
    badge: str = ""  # key into the badges dict


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    icon: str
    href: str = ""  # a group without children is a plain link
    children: tuple[Leaf, ...] = ()
    prefixes: tuple[str, ...] = field(default_factory=tuple)  # extra paths that belong to the group
    admin_only: bool = False
    badge: str = ""


NAV: tuple[Group, ...] = (
    Group("today", "Сегодня", "home", href="/app"),
    Group(
        "calendar",
        "Календарь",
        "calendar",
        children=(Leaf("Записи", "/app/calendar", "pending"), Leaf("Услуги и расписание", "/app/calendar/setup")),
        prefixes=("/app/calendar",),
    ),
    Group(
        "people",
        "Общение",
        "chat",
        children=(
            Leaf("Диалоги", "/app/conversations"),
            Leaf("Заявки", "/app/leads", "leads"),
            Leaf("Вопросы без ответа", "/app/gaps", "gaps"),
        ),
    ),
    Group(
        "bot",
        "Бот",
        "bot",
        children=(
            Leaf("Ответы", "/app/bot/answers"),
            Leaf("Документы", "/app/bot/documents"),
            Leaf("Шаблоны", "/app/bot/templates"),
            Leaf("Характер бота", "/app/bot/personality"),
            Leaf("Проверка", "/app/test"),
        ),
        prefixes=("/app/bot",),
    ),
    Group("reports", "Отчёты", "chart", href="/app/reports"),
    Group(
        "settings",
        "Настройки",
        "settings",
        children=(Leaf("Основные", "/app/settings"), Leaf("Каналы", "/app/channels"), Leaf("Тариф", "/app/billing")),
    ),
    Group("admin", "Администрирование", "shield", href="/admin", admin_only=True),
)


def _matches(path: str, href: str) -> bool:
    if href == "/app":
        return path == "/app"
    return path == href or path.startswith(href + "/")


def build(path: str, is_admin: bool, badges: dict[str, int]) -> tuple[list[dict], list[tuple[str, str]]]:
    """-> (sidebar tree with active/open flags, breadcrumbs [(title, href)])."""
    leaves = [(g, leaf) for g in NAV for leaf in g.children]
    # the most specific leaf wins, so /app/calendar/setup does not also light up "Записи"
    hit = max((x for x in leaves if _matches(path, x[1].href)), key=lambda x: len(x[1].href), default=None)
    tree: list[dict] = []
    crumbs: list[tuple[str, str]] = []
    for g in NAV:
        if g.admin_only and not is_admin:
            continue
        in_group = (
            (not g.children and _matches(path, g.href))
            or (hit is not None and hit[0] is g)
            or any(_matches(path, p) for p in g.prefixes)
        )
        item = {
            "key": g.key,
            "title": g.title,
            "icon": g.icon,
            "href": g.href,
            "active": in_group and not g.children,
            "open": in_group and bool(g.children),
            "badge": badges.get(g.badge, 0) if g.badge else 0,
            "children": [
                {
                    "title": leaf.title,
                    "href": leaf.href,
                    "active": hit is not None and hit[1] is leaf,
                    "badge": badges.get(leaf.badge, 0) if leaf.badge else 0,
                }
                for leaf in g.children
            ],
        }
        tree.append(item)
        if in_group:
            if g.children:
                crumbs.append((g.title, ""))
                if hit is not None and hit[0] is g:
                    crumbs.append((hit[1].title, hit[1].href))
            elif g.key != "today":  # the dashboard is already the first crumb ("Главная")
                crumbs.append((g.title, g.href))
    return tree, crumbs


def badges_for(db, user_id: int) -> dict[str, int]:
    """Small real counters for the sidebar (new leads, bookings awaiting confirmation, open gaps)."""
    from app.models import Gap

    biz = db.scalar(select(Business).where(Business.owner_id == user_id))
    if biz is None:
        return {}

    def n(stmt) -> int:
        return db.scalar(stmt) or 0

    return {
        "leads": n(select(func.count(Lead.id)).where(Lead.business_id == biz.id, Lead.status == "new")),
        "pending": n(
            select(func.count(Booking.id)).where(
                Booking.business_id == biz.id, Booking.status == "pending", Booking.start_utc > utcnow()
            )
        ),
        "gaps": n(select(func.count(Gap.id)).where(Gap.business_id == biz.id, Gap.status == "open")),
    }
