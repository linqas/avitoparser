"""Рыночная аналитика по товарам: группы, медиана, отклонение, заметки.

Идея адаптирована из открытого avito-parser: сравниваем объявления одной
модели и объёма памяти. Группы меньше трёх объявлений не считаются рынком.
"""

from __future__ import annotations

import re
import statistics
from dataclasses import dataclass
from typing import Any, Iterable

MEMORY_PATTERN = re.compile(
    r"(?<!\d)(\d+(?:/\d+)?)\s*(?:ГБ|ТБ|GB|TB)(?!\w)", re.IGNORECASE
)
DEFECT_PATTERN = re.compile(
    r"(?<!не\s)бит(?:ый|ая|ое|ые)\b|заблок|блокировк|icloud|айклауд|"
    r"на\s+запчаст|на\s+разбор|под\s+восстановлен|не\s+включ|"
    r"не\s+работает|разбит|треснут|утоплен|после\s+воды|залит|"
    r"не\s+ловит|плата\s+мертв|донор",
    re.IGNORECASE,
)
OUTLIER_LOW = 0.5
OUTLIER_HIGH = 1.8
CHEAP_SHARE = 0.85
MIN_GROUP_SIZE = 3


@dataclass(frozen=True)
class GroupStats:
    group: str
    count: int
    used: int
    low: int
    q1: int
    median: int
    q3: int
    high: int


def group_title(title: str) -> str:
    parts = [part.strip() for part in str(title or "").split(",") if part.strip()]
    model = parts[0] if parts else str(title or "Без названия")
    memory = next(
        (part for part in parts[1:] if MEMORY_PATTERN.search(part)),
        "",
    )
    if memory:
        return f"{model}, {memory}"
    return model


def is_defect(title: str) -> bool:
    return bool(DEFECT_PATTERN.search(str(title or "")))


def market_stats(listings: Iterable[dict[str, Any]]) -> dict[str, GroupStats]:
    groups: dict[str, list[int]] = {}
    for listing in listings:
        price = listing.get("price")
        title = str(listing.get("title") or "")
        if not isinstance(price, int) or price <= 0 or is_defect(title):
            continue
        groups.setdefault(group_title(title), []).append(price)

    result: dict[str, GroupStats] = {}
    for group, prices in groups.items():
        if len(prices) < MIN_GROUP_SIZE:
            continue
        raw_median = statistics.median(prices)
        kept = sorted(
            price
            for price in prices
            if OUTLIER_LOW * raw_median <= price <= OUTLIER_HIGH * raw_median
        )
        if len(kept) < MIN_GROUP_SIZE:
            continue
        q1, median, q3 = statistics.quantiles(kept, n=4)
        result[group] = GroupStats(
            group=group,
            count=len(prices),
            used=len(kept),
            low=kept[0],
            q1=round(q1),
            median=round(median),
            q3=round(q3),
            high=kept[-1],
        )
    return result


def analyze(
    listings: Iterable[dict[str, Any]],
    *,
    query_label: str = "",
    city: str = "",
) -> list[dict[str, Any]]:
    """Возвращает строки для вкладки «Аналитика рынка»."""
    source = list(listings)
    stats = market_stats(source)
    rows: list[dict[str, Any]] = []
    for listing in source:
        price = listing.get("price")
        title = str(listing.get("title") or "")
        group = group_title(title)
        stats_entry = stats.get(group)
        market = stats_entry.median if stats_entry else None
        vs_market: float | None = None
        notes: list[str] = []
        if is_defect(title):
            notes.append("возможно неисправен / на запчасти")
        if market and isinstance(price, int):
            vs_market = price / market - 1
            if price < OUTLIER_LOW * market:
                notes.append("подозрительно дёшево")
            elif price <= CHEAP_SHARE * market:
                notes.append("дешевле рынка")
        if not market:
            notes.append("мало данных для рынка")
        rows.append(
            {
                "query": query_label,
                "city": city,
                "group": group,
                "listing": listing,
                "market": market,
                "vs_market": vs_market,
                "notes": "; ".join(notes),
                "stats": stats_entry,
            }
        )
    return rows
