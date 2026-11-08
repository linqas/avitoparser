"""Фильтры объявлений Avito Radar.

Фильтры применяются уже после сбора карточек. Если Avito не показал поле,
объявление не отбрасывается молча: для строгих фильтров действует правило
«неизвестно — значит не подходит», а причина отказа пишется в лог.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

logger = logging.getLogger("avito_radar.filters")

DELIVERY_LABELS = {
    "есть": "require",
    "нет": "exclude",
    "не важно": "any",
}
class FilterError(ValueError):
    pass


def split_keywords(value: str) -> tuple[str, ...]:
    """Разбирает строку «слово, фраза; ещё» в нормализованные условия."""
    items = re.split(r"[,;\n]+", value or "")
    result: list[str] = []
    for item in items:
        normalized = (
            item.strip().casefold().replace("ё", "е").replace("  ", " ")
        )
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


@dataclass(slots=True)
class FilterCriteria:
    price_min: int | None = None
    price_max: int | None = None
    delivery: str = "any"  # require | exclude | any
    seller_type: str = "any"  # any | private | company
    city: str = ""
    include_words: tuple[str, ...] = ()
    exclude_words: tuple[str, ...] = ()
    query: str = ""
    model_mode: str = "strict"
    smart_filter: bool = False
    category: str = ""
    memory: str = ""
    color: str = ""
    sim: str = ""
    condition: str = ""

    def validate(self) -> None:
        if self.price_min is not None and self.price_max is not None:
            if self.price_min > self.price_max:
                raise FilterError("Цена «от» не может быть больше цены «до»")
        if self.price_min is not None and self.price_min < 0:
            raise FilterError("Цена «от» не может быть отрицательной")
        if self.price_max is not None and self.price_max < 0:
            raise FilterError("Цена «до» не может быть отрицательной")
        if self.delivery not in {"require", "exclude", "any"}:
            raise FilterError("Некорректный фильтр Авито Доставки")


@dataclass(slots=True)
class FilterResult:
    accepted: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[tuple[dict[str, Any], str]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.accepted) + len(self.rejected)


def _normalize_city(value: Any) -> str:
    text = (
        str(value or "")
        .strip()
        .casefold()
        .replace("ё", "е")
    )
    text = re.sub(r"^(?:г\.?\s*|город\s+)", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,.")


QUERY_ALIASES = {
    "ыпхоне": "iphone",
    "айфон": "iphone",
    "про": "pro",
    "плюс": "plus",
    "макс": "max",
    "мини": "mini",
    "самсунг": "samsung",
    "сяоми": "xiaomi",
    "редми": "redmi",
    "ултра": "ultra",
    "ноут": "note",
    # Объём памяти: в запросе часто пишут «Gb», в заголовке Avito — «ГБ».
    "gb": "гб",
    "гб": "гб",
    "tb": "тб",
    "тб": "тб",
}
MODEL_QUALIFIERS = {
    "pro",
    "max",
    "plus",
    "mini",
    "ultra",
    "se",
    "air",
    "fe",
    "lite",
    "note",
}


def _query_tokens(value: str) -> tuple[str, ...]:
    text = str(value or "").casefold().replace("ё", "е")
    words = re.findall(r"[a-zа-я0-9]+", text)
    return tuple(QUERY_ALIASES.get(word, word) for word in words)


def _title_matches_query(title: str, query: str, mode: str = "strict") -> bool:
    wanted = _query_tokens(query)
    if not wanted:
        return True
    have = set(_query_tokens(title))
    if any(word not in have for word in wanted):
        return False
    # «iPhone 15 Pro» не должен превращаться в «15 Pro Max»/«15 Plus».
    if mode == "strict" and any(char.isdigit() for char in query):
        for word in have:
            if word in MODEL_QUALIFIERS and word not in wanted:
                return False
    return True


SMART_BAD_PHRASES = (
    "на запчасти",
    "запчаст",
    "нерабоч",
    "не работает",
    "не включается",
    "не заряжается",
    "разбит",
    "треснул",
    "дефект",
    "утоплен",
    "water damage",
    "icloud",
    "заблокирован",
    "аксессуар",
    "чехол",
    "плёнк",
    "пленк",
    "защитное стекло",
    "кабель",
    "переходник",
    "адаптер",
    "док-станц",
    "только коробка",
    "пустая коробка",
    "box only",
)


COLOR_ALIASES = {
    "черный": ("черн", "black"),
    "чёрный": ("черн", "black"),
    "белый": ("бел", "white"),
    "синий": ("син", "blue"),
    "серый": ("сер", "gray", "grey"),
    "зеленый": ("зелен", "green"),
    "зелёный": ("зелен", "green"),
    "красный": ("красн", "red"),
    "серебристый": ("серебр", "silver"),
    "золотой": ("золот", "gold"),
    "фиолетовый": ("фиолет", "purple"),
    "розовый": ("розов", "pink"),
    "желтый": ("желт", "yellow"),
    "жёлтый": ("желт", "yellow"),
    "голубой": ("голуб", "cyan"),
}
COLOR_TERMS = tuple(
    term for aliases in COLOR_ALIASES.values() for term in aliases
)


def _spec_text(value: Any) -> str:
    return str(value or "").casefold().replace("ё", "е")


def _memory_matches(text: str, memory: str) -> bool:
    value = str(memory or "").strip()
    if not value:
        return True
    number = value.rstrip("гбgb ").strip()
    if not number:
        return True
    return bool(re.search(rf"(?<!\d){re.escape(number)}\s*(?:гб|gb|g)(?!\d)", text))


def _has_color_word(text: str) -> bool:
    return any(term in text for term in COLOR_TERMS)


def _color_matches(text: str, color: str) -> bool:
    wanted = _spec_text(color)
    if not wanted:
        return True
    aliases = COLOR_ALIASES.get(wanted, (wanted,))
    return any(alias in text for alias in aliases)


def _sim_matches(text: str, sim: str) -> bool:
    wanted = _spec_text(sim)
    if not wanted:
        return True
    if wanted in {"esim", "e-sim", "есим"}:
        return "esim" in text or "e-sim" in text or "есим" in text
    if wanted in {"1", "1sim", "1 sim"}:
        return "1 sim" in text or "1 сим" in text or "одна sim" in text
    if wanted in {"2", "2sim", "2 sim"}:
        return (
            "2 sim" in text
            or "2 сим" in text
            or "dual sim" in text
            or "две sim" in text
        )
    return wanted in text


def _sim_known(text: str) -> bool:
    return any(
        token in text
        for token in (
            "1 sim",
            "2 sim",
            "dual sim",
            "esim",
            "e-sim",
            "1 сим",
            "2 сим",
            "две sim",
            "есим",
        )
    )


def _condition_matches(text: str, condition: str) -> bool:
    wanted = _spec_text(condition)
    if not wanted:
        return True
    if wanted in {"new", "новое", "новый"}:
        return "нов" in text
    if wanted in {"used", "бу", "б/у"}:
        return "б/у" in text or "бу" in text or "used" in text
    if wanted in {"refurbished", "восстановленное"}:
        return "восстанов" in text or "refurb" in text
    return wanted in text


def _condition_known(text: str) -> bool:
    return any(
        token in text
        for token in ("нов", "б/у", "бу", "used", "восстанов", "refurb")
    )


def smart_filter_reason(title: str) -> str | None:
    text = _normalize_city(title)
    for phrase in SMART_BAD_PHRASES:
        if phrase in text:
            return phrase
    return None


def listing_passes(
    listing: dict[str, Any], criteria: FilterCriteria
) -> tuple[bool, str]:
    price = listing.get("price")
    if criteria.price_min is not None:
        if price is None:
            return False, "цена не указана, а задана нижняя граница"
        if int(price) < criteria.price_min:
            return False, f"цена {price} меньше {criteria.price_min}"
    if criteria.price_max is not None:
        if price is None:
            return False, "цена не указана, а задана верхняя граница"
        if int(price) > criteria.price_max:
            return False, f"цена {price} больше {criteria.price_max}"

    seller = str(listing.get("seller") or "").casefold()
    company_words = ("компания", "агентство", "магазин", "застройщик", "продавец-магазин")
    if criteria.seller_type == "private" and any(
        word in seller for word in company_words
    ):
        return False, "продавец отмечен как компания"
    if criteria.seller_type == "company" and not any(
        word in seller for word in company_words
    ):
        return False, "продавец не отмечен как компания"
    delivery = listing.get("delivery") or "unknown"
    if criteria.delivery == "require" and delivery != "yes":
        return False, "нет подтверждённой Авито Доставки"
    if criteria.delivery == "exclude" and delivery == "yes":
        return False, "есть Авито Доставка (выбран вариант «нет»)"

    if criteria.city:
        wanted = _normalize_city(criteria.city)
        actual = _normalize_city(listing.get("city"))
        # Мягкий режим: Avito часто не указывает город в карточке. Такие
        # объявления не отбрасываем, но явно чужие города скрываем.
        if actual and wanted not in actual:
            return False, f"город «{listing.get('city')}» не совпадает"

    title = _normalize_city(listing.get("title"))
    for word in criteria.include_words:
        if word and word not in title:
            return False, f"в заголовке нет обязательного слова «{word}»"
    for word in criteria.exclude_words:
        if word and word in title:
            return False, f"в заголовке есть исключённое слово «{word}»"

    if criteria.smart_filter:
        smart_reason = smart_filter_reason(title)
        if smart_reason:
            return False, f"смарт-фильтр: похоже на неисправность/аксессуар («{smart_reason}»)"

    spec_text = _spec_text(title)
    if criteria.memory and not _memory_matches(spec_text, criteria.memory):
        return False, f"нет памяти {criteria.memory} ГБ"
    if criteria.color:
        if _has_color_word(spec_text) and not _color_matches(spec_text, criteria.color):
            return False, f"цвет не совпадает с «{criteria.color}»"
    if criteria.sim:
        if _sim_known(spec_text) and not _sim_matches(spec_text, criteria.sim):
            return False, f"SIM не совпадает с «{criteria.sim}»"
    if criteria.condition:
        if _condition_known(spec_text) and not _condition_matches(
            spec_text, criteria.condition
        ):
            return False, f"состояние не совпадает с «{criteria.condition}»"

    if criteria.query and not _title_matches_query(
        str(listing.get("title") or ""), criteria.query, criteria.model_mode
    ):
        return False, f"не совпадает с моделью запроса «{criteria.query}»"

    return True, ""


def apply_filters(
    listings: Iterable[dict[str, Any]], criteria: FilterCriteria
) -> FilterResult:
    criteria.validate()
    result = FilterResult()
    for listing in listings:
        passed, reason = listing_passes(listing, criteria)
        if passed:
            result.accepted.append(listing)
        else:
            result.rejected.append((listing, reason))
            logger.info(
                "Отфильтровано «%s» (id %s): %s",
                listing.get("title", "без названия")[:60],
                listing.get("id", "?"),
                reason,
            )
    logger.info(
        "Фильтры: принято %s, отклонено %s из %s",
        len(result.accepted),
        len(result.rejected),
        result.total,
    )
    return result


def criteria_from_values(
    *,
    price_min: str | int | None,
    price_max: str | int | None,
    delivery_label: str,
    city: str,
    include_words: str = "",
    exclude_words: str = "",
) -> FilterCriteria:
    """Собирает критерии из строк GUI (пустая цена = без ограничения)."""
    from utils import parse_price

    def optional_price(value: str | int | None) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        parsed = parse_price(value)
        if parsed is None:
            raise FilterError(f"Некорректная цена: {value}")
        return parsed

    criteria = FilterCriteria(
        price_min=optional_price(price_min),
        price_max=optional_price(price_max),
        delivery=DELIVERY_LABELS.get(delivery_label, "any"),
        city=(city or "").strip(),
        include_words=split_keywords(include_words),
        exclude_words=split_keywords(exclude_words),
    )
    criteria.validate()
    return criteria


@dataclass(slots=True)
class QueryConfig:
    """Сохранённый запрос со своими локальными фильтрами."""

    query: str
    city: str = ""
    local_priority: bool = False
    price_min: int | None = None
    price_max: int | None = None
    delivery: str = "any"
    include_words: str = ""
    exclude_words: str = ""
    max_listings: int = 50
    pages: int = 1
    primed: bool = True
    source_url: str = ""
    id: int | None = None
    sort_order: int = 0
    avito_sort: int = 0
    model_mode: str = "strict"
    seller_type: str = "any"
    smart_filter: bool = False
    category: str = ""
    memory: str = ""
    color: str = ""
    sim: str = ""
    condition: str = ""
    is_active: bool = True
    notify_new: bool = True
    notify_price: bool = True
    notify_old: bool = False

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "QueryConfig":
        return cls(
            id=int(row["id"]) if row.get("id") is not None else None,
            query=str(row.get("query") or ""),
            city=str(row.get("city") or ""),
            local_priority=bool(row.get("local_priority")),
            price_min=row.get("price_min"),
            price_max=row.get("price_max"),
            delivery=str(row.get("delivery") or "any"),
            include_words=str(row.get("include_words") or ""),
            exclude_words=str(row.get("exclude_words") or ""),
            max_listings=int(row.get("max_listings") or 50),
            pages=max(1, min(5, int(row.get("pages") or 1))),
            primed=bool(row.get("primed", 1)),
            source_url=str(row.get("source_url") or ""),
            sort_order=int(row.get("sort_order") or 0),
            avito_sort=int(row.get("avito_sort") or 0),
            model_mode=str(row.get("model_mode") or "strict"),
            seller_type=str(row.get("seller_type") or "any"),
            smart_filter=bool(row.get("smart_filter", 0)),
            category=str(row.get("category") or ""),
            memory=str(row.get("memory") or ""),
            color=str(row.get("color") or ""),
            sim=str(row.get("sim") or ""),
            condition=str(row.get("condition") or ""),
            is_active=bool(row.get("is_active", 1)),
            notify_new=bool(row.get("notify_new", 1)),
            notify_price=bool(row.get("notify_price", 1)),
            notify_old=bool(row.get("notify_old", 0)),
        )

    def criteria(self) -> FilterCriteria:
        criteria = FilterCriteria(
            price_min=int(self.price_min) if self.price_min is not None else None,
            price_max=int(self.price_max) if self.price_max is not None else None,
            delivery=self.delivery,
            city=self.city,
            include_words=split_keywords(self.include_words),
            exclude_words=split_keywords(self.exclude_words),
            query=self.query,
            model_mode=self.model_mode,
            seller_type=self.seller_type,
            smart_filter=self.smart_filter,
            category=self.category,
            memory=self.memory,
            color=self.color,
            sim=self.sim,
            condition=self.condition,
        )
        criteria.validate()
        return criteria

    def query_key(self) -> str:
        query_id = self.id if self.id is not None else 0
        include = ",".join(split_keywords(self.include_words))
        exclude = ",".join(split_keywords(self.exclude_words))
        return "|".join(
            [
                f"id={query_id}",
                # Название из ссылки — только подпись, поэтому не должно
                # сбрасывать историю наблюдений при переименовании.
                "" if self.source_url else self.query.strip().casefold(),
                self.city.strip().casefold(),
                str(self.price_min if self.price_min is not None else ""),
                str(self.price_max if self.price_max is not None else ""),
                self.delivery,
                "local=1" if self.local_priority else "local=0",
                f"limit={self.max_listings}",
                include,
                exclude,
                self.source_url,
                "smart=1" if self.smart_filter else "smart=0",
                self.category,
                self.memory,
                self.color,
                self.sim,
                self.condition,
            ]
        )
