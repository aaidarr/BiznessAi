"""Human-editable weekly schedule: 'Пн-Пт 10:00-13:00, 14:00-19:00' per line."""

from __future__ import annotations

import re

DAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
_INTERVAL = re.compile(r"^(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})$")


class ScheduleError(ValueError):
    pass


def _parse_days(token: str) -> list[int]:
    token = token.lower().strip().rstrip(":")
    if token in ("ежедневно", "каждый день", "все дни"):
        return list(range(7))
    out: set[int] = set()
    for part in token.split(","):
        part = part.strip()
        if "-" in part:
            a, b = (x.strip() for x in part.split("-", 1))
            if a not in DAYS or b not in DAYS:
                raise ScheduleError(f"Неизвестные дни: «{part}»")
            i, j = DAYS.index(a), DAYS.index(b)
            out.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
        elif part in DAYS:
            out.add(DAYS.index(part))
        else:
            raise ScheduleError(f"Неизвестный день: «{part}»")
    return sorted(out)


def parse_schedule(text: str) -> list[tuple[int, int, int]]:
    """-> [(weekday, start_min, end_min)]. Raises ScheduleError with a user-readable message."""
    result: dict[int, list[tuple[int, int]]] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^(ежедневно|каждый день|все дни|[а-яА-Я,\- ]+?)\s+(\d.*)$", line)
        if not m:
            raise ScheduleError(f"Не понял строку: «{line}». Пример: Пн-Пт 10:00-19:00")
        days = _parse_days(m.group(1))
        for iv in m.group(2).split(","):
            mm = _INTERVAL.match(iv.strip())
            if not mm:
                raise ScheduleError(f"Не понял время: «{iv.strip()}». Пример: 10:00-19:00")
            h1, m1, h2, m2 = (int(x) for x in mm.groups())
            if h1 > 23 or h2 > 24 or m1 > 59 or m2 > 59 or (h2 == 24 and m2):
                raise ScheduleError(f"Некорректное время: «{iv.strip()}»")
            s, e = h1 * 60 + m1, h2 * 60 + m2
            if e <= s:
                raise ScheduleError(f"Конец раньше начала: «{iv.strip()}»")
            for d in days:
                result.setdefault(d, []).append((s, e))
    out: list[tuple[int, int, int]] = []
    for d, ivs in result.items():
        ivs.sort()
        if len(ivs) > 4:
            raise ScheduleError("Не больше 4 интервалов в день")
        for (_, e1), (s2, _) in zip(ivs, ivs[1:], strict=False):
            if s2 < e1:
                raise ScheduleError("Интервалы пересекаются")
        out.extend((d, s, e) for s, e in ivs)
    return sorted(out)


def _fmt(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def format_schedule(rows: list[tuple[int, int, int]]) -> str:
    by_day: dict[int, list[tuple[int, int]]] = {}
    for d, s, e in sorted(rows):
        by_day.setdefault(d, []).append((s, e))
    groups: list[tuple[str, list[int]]] = []
    for d in sorted(by_day):
        key = ", ".join(f"{_fmt(s)}-{_fmt(e)}" for s, e in by_day[d])
        if groups and groups[-1][0] == key and groups[-1][1][-1] == d - 1:
            groups[-1][1].append(d)
        else:
            groups.append((key, [d]))
    lines = []
    for key, days in groups:
        label = DAYS[days[0]].capitalize()
        if len(days) > 1:
            label += "-" + DAYS[days[-1]].capitalize()
        lines.append(f"{label} {key}")
    return "\n".join(lines)
