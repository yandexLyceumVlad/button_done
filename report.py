"""Отчётные периоды 
    сентябрь — январь:  01.09.YYYY — 31.01.YYYY+1
    февраль — август:   01.02.YYYY — 31.08.YYYY

"""

from __future__ import annotations

import re
from datetime import date
from typing import Iterable, Optional, Sequence

MONTHS = {
    "января": 1,
    "февраля": 2,
    "марта": 3,
    "апреля": 4,
    "мая": 5,
    "июня": 6,
    "июля": 7,
    "августа": 8,
    "сентября": 9,
    "октября": 10,
    "ноября": 11,
    "декабря": 12,
}

_MONTH_PATTERN = "|".join(MONTHS)

_DOT_DATE = re.compile(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?!\d)")
_WORD_DATE = re.compile(
    rf"(?<!\d)(\d{{1,2}})\s+({_MONTH_PATTERN})\s+(\d{{4}})(?!\d)",
    re.IGNORECASE,
)
_ISO_DATE = re.compile(r"(?<!\d)(\d{4})-(\d{1,2})-(\d{1,2})(?!\d)")
_YEAR_ONLY = re.compile(r"(?<!\d)(20\d{2})(?!\d)")


def _make_date(year: int, month: int, day: int) -> Optional[date]:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def extract_dates(value: str) -> list[date]:
    """Возвращает все даты из строки в порядке появления в тексте."""
    if not value:
        return []

    found: list[tuple[int, date]] = []

    for match in _DOT_DATE.finditer(value):
        day, month, year = (int(part) for part in match.groups())
        if year < 100:
            year += 2000
        parsed = _make_date(year, month, day)
        if parsed is not None:
            found.append((match.start(), parsed))

    for match in _WORD_DATE.finditer(value):
        day = int(match.group(1))
        month = MONTHS[match.group(2).lower()]
        year = int(match.group(3))
        parsed = _make_date(year, month, day)
        if parsed is not None:
            found.append((match.start(), parsed))

    for match in _ISO_DATE.finditer(value):
        year, month, day = (int(part) for part in match.groups())
        parsed = _make_date(year, month, day)
        if parsed is not None:
            found.append((match.start(), parsed))

    found.sort(key=lambda item: item[0])

    # Убираем дубли, которые могли появиться из-за пересечения шаблонов.
    result: list[date] = []
    for _, parsed in found:
        if not result or result[-1] != parsed:
            result.append(parsed)
    return result


def normalize_dates(value: str, kind: str) -> Optional[str]:
    """Приводит введённую строку к формату, который понимает отчёт.

    Для КПК оставляем одну дату (дату выдачи), для конкурса — диапазон.
    Возвращает None, если ни одной полной даты не найдено.
    """
    dates = extract_dates(value)
    if not dates:
        return None

    if kind == "курс":
        return dates[-1].strftime("%d.%m.%Y")
    if len(dates) == 1:
        return dates[0].strftime("%d.%m.%Y")
    return f"{dates[0].strftime('%d.%m.%Y')} – {dates[-1].strftime('%d.%m.%Y')}"


def document_date(value: str, kind: str) -> Optional[date]:
    """Дата, по которой документ попадает в отчётный период."""
    dates = extract_dates(value)
    if not dates:
        return None
    # КПК: дата выдачи (одна). Конкурс: конец диапазона.
    return dates[-1]


def current_period(today: Optional[date] = None) -> tuple[date, date]:
    """Текущий отчётный период, ближайший к сегодняшней дате."""
    today = today or date.today()
    year = today.year

    if today.month >= 9:
        return date(year, 9, 1), date(year + 1, 1, 31)
    if today.month == 1:
        return date(year - 1, 9, 1), date(year, 1, 31)
    return date(year, 2, 1), date(year, 8, 31)


def document_in_period(
    dates_value: str,
    kind: str,
    start: date,
    end: date,
) -> bool:
    parsed = document_date(dates_value, kind)
    if parsed is not None:
        return start <= parsed <= end

    # Запасной вариант для старых записей, где указан только год.
    match = _YEAR_ONLY.search(dates_value or "")
    if match:
        year = int(match.group(1))
        return year in (start.year, end.year)

    return False


def filter_period(
    documents: Iterable,
    start: date,
    end: date,
) -> list:
    return [
        document
        for document in documents
        if document_in_period(document.dates or "", document.kind, start, end)
    ]


def format_period(start: date, end: date) -> str:
    return f"{start.strftime('%d.%m.%Y')} – {end.strftime('%d.%m.%Y')}"


def format_date(value: date) -> str:
    return value.strftime("%d.%m.%Y")


def sort_documents_by_date(documents: Sequence) -> list:
    """Сортировка по дате документа (сначала более поздние)."""

    def key(document):
        parsed = document_date(document.dates or "", document.kind)
        return parsed or date.min

    return sorted(documents, key=key, reverse=True)
