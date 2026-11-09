"""CustomTkinter-интерфейс Avito Radar."""

from __future__ import annotations

import ctypes
import json
import logging
import math
import os
import queue
import re
import secrets
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox

import market
from bot_listener import BotListener
from filters import (
    DELIVERY_LABELS,
    FilterError,
    QueryConfig,
    apply_filters,
    criteria_from_values,
)
try:
    from http_scraper import HttpAvitoScraper
except Exception:  # curl_cffi/avito_http может отсутствовать
    HttpAvitoScraper = None  # type: ignore[assignment]
from notifier import TelegramNotifier
from reporter import (
    DetailsPanel,
    ListingsTable,
    MarketTable,
    ReporterError,
    export_xlsx,
)
from scraper import (
    AvitoBlockedError,
    AvitoScraper,
    ScraperCancelled,
    ScraperError,
    ScraperUnavailableError,
)
from storage import Storage
from utils import (
    APP_DIR,
    RedactingFormatter,
    app_path,
    build_effective_source_url,
    get_telegram_token,
    is_avito_search_url,
    load_config,
    model_query_from_title,
    now_moscow,
    open_listing_url,
    parse_avito_search_url,
    save_config_scalar,
    save_scraper_setting,
    save_telegram_token,
    set_preferred_browser,
)

logger = logging.getLogger("avito_radar.gui")


def apply_light_titlebar(window: Any) -> None:
    """Светлый системный заголовок Windows вместо чёрной полосы."""
    if os.name != "nt":
        return
    try:
        hwnd = ctypes.windll.user32.GetParent(window.winfo_id())
        if not hwnd:
            hwnd = window.winfo_id()
        enabled = ctypes.c_int(0)
        for attribute in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                hwnd, attribute, ctypes.byref(enabled), ctypes.sizeof(enabled)
            )
        caption = ctypes.c_uint(0x00F2F2F2)
        text = ctypes.c_uint(0x001F1F1F)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 35, ctypes.byref(caption), ctypes.sizeof(caption)
        )
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            hwnd, 36, ctypes.byref(text), ctypes.sizeof(text)
        )
    except Exception:
        logger.debug("Не удалось применить светлый заголовок окна", exc_info=True)

MODE_ONCE = "один прогон"
MODE_DURATION = "N минут"
MODE_EVERY_N = "цикл каждые N минут"
MODE_UNTIL_STOP = "до ручной остановки"
MODES = [MODE_ONCE, MODE_DURATION, MODE_EVERY_N, MODE_UNTIL_STOP]
CATEGORY_LABELS = {
    "Все категории": "",
    "Телефоны": "telefony",
    "Ноутбуки": "noutbuki",
    "Планшеты": "planshety",
    "Комплектующие": "tovary_dlya_kompyutera",
}
CATEGORY_REVERSE = {value: label for label, value in CATEGORY_LABELS.items()}
MEMORY_LABELS = ["Любая", "64", "128", "256", "512", "1024"]
SIM_LABELS = ["Любая", "1 SIM", "2 SIM", "eSIM"]
SIM_VALUES = {"Любая": "", "1 SIM": "1", "2 SIM": "2", "eSIM": "esim"}
SIM_REVERSE = {value: label for label, value in SIM_VALUES.items()}
CONDITION_LABELS = ["Любое", "Новое", "Б/у", "Восстановленное"]
CONDITION_VALUES = {
    "Любое": "",
    "Новое": "new",
    "Б/у": "used",
    "Восстановленное": "refurbished",
}
CONDITION_REVERSE = {value: label for label, value in CONDITION_VALUES.items()}

DELIVERY_VALUES = ["есть", "нет", "не важно"]


def _mode_storage_value(mode_label: str) -> str:
    return {
        MODE_ONCE: "once",
        MODE_DURATION: "duration",
        MODE_EVERY_N: "every_n",
        MODE_UNTIL_STOP: "until_stop",
    }.get(mode_label, "once")


class GuiLogHandler(logging.Handler):
    def __init__(self, ui_queue: queue.Queue):
        super().__init__()
        self.ui_queue = ui_queue
        self.setFormatter(
            RedactingFormatter(
                "%(asctime)s | %(levelname)s | %(message)s", datefmt="%H:%M:%S"
            )
        )

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.ui_queue.put(("log", (self.format(record), record.levelno)))
        except Exception:
            pass


class MonitorWorker(threading.Thread):
    """Единственный поток скрейпинга: страницы открываются последовательно."""

    def __init__(
        self,
        *,
        storage: Storage,
        notifier: TelegramNotifier,
        config: dict[str, Any],
        config_path: str | Path,
        queries: list[QueryConfig],
        recipients: list[int],
        mode_label: str,
        interval_minutes: int,
        sort_key: str,
        on_status: Callable[[str], None],
        on_error: Callable[[str, bool], None],
        on_cycle: Callable[
            [list[dict[str, Any]], dict[str, int], dict[str, int], list[dict[str, Any]]],
            None,
        ],
        on_finished: Callable[[str], None],
        on_browser_visibility: Callable[[bool], None] | None = None,
    ):
        super().__init__(name="AvitoRadarMonitor", daemon=True)
        self.storage = storage
        self.notifier = notifier
        self.config = config
        self.config_path = Path(config_path)
        self.queries = list(queries)
        self.recipients = list(dict.fromkeys(int(item) for item in recipients))
        self.mode_label = mode_label
        self.interval_minutes = max(1, int(interval_minutes))
        self.sort_key = sort_key
        self.notifications = dict(config.get("notifications", {}))
        self.on_status = on_status
        self.on_error = on_error
        self.on_cycle = on_cycle
        self.on_finished = on_finished
        self.on_browser_visibility = on_browser_visibility
        self.stop_event = threading.Event()
        self._reported_messages: set[str] = set()
        self._captcha_failures = 0
        self._captcha_alert_sent = False
        self._scraper: AvitoScraper | None = None
        self._auto_export_path = APP_DIR / "exports" / "avito_radar_auto.xlsx"
        self._last_rejected: list[dict[str, Any]] = []
        self._last_cycle_listings: list[dict[str, Any]] = []
        self._last_cycle_summary: dict[str, int] = {}
        self._market_by_id: dict[str, dict[str, Any]] = {}
        self._cycle_query_listings: dict[str, list[dict[str, Any]]] = {}
        self.scan_limit = max(
            1, int(self.config.get("scraper", {}).get("scan_limit", 50))
        )
        self.fresh_hours = max(
            0.0, float(self.config.get("app", {}).get("fresh_hours", 3))
        )
        self.date_enrich_limit = max(
            -1, int(self.config.get("app", {}).get("date_enrich_limit", 5))
        )
        self.missing_check = bool(
            self.config.get("app", {}).get("missing_check", False)
        )
        self.browser_toggle_available = bool(
            self.config.get("scraper", {}).get("hidden_window", True)
            and self.config.get("scraper", {}).get("transport", "auto")
            in {"browser", "auto"}
        )

    def request_stop(self) -> None:
        self.stop_event.set()

    def _report_error(self, message: str, modal: bool = False) -> None:
        logger.error("%s", message)
        # Одинаковые ошибки не показываем модально повторно в рамках запуска.
        if modal and message not in self._reported_messages:
            self._reported_messages.add(message)
            self.on_error(message, True)
        elif not modal:
            self.on_error(message, False)

    def _is_fresh(self, listing: dict[str, Any]) -> bool:
        if self.fresh_hours <= 0:
            return True
        raw = listing.get("published_at")
        if not raw:
            return False
        try:
            published = datetime.fromisoformat(str(raw))
        except (TypeError, ValueError):
            return False
        now = datetime.now(published.tzinfo) if published.tzinfo else datetime.now()
        age = (now - published).total_seconds()
        return age <= self.fresh_hours * 3600

    def _notify_event(self, event: dict[str, Any]) -> None:
        if not self.recipients:
            return
        report = None
        try:
            if event["kind"] == "new":
                report = self.notifier.notify_new(self.recipients, event["listing"])
            elif event["kind"] == "price":
                report = self.notifier.notify_price_change(
                    self.recipients, event["listing"], event.get("old_price")
                )
            elif event["kind"] == "missing":
                report = self.notifier.notify_missing(self.recipients, event["listing"])
        except Exception as exc:
            self._report_error(f"Ошибка Telegram: {exc}", modal=False)
            return
        if report and report.failed:
            first = report.failed[0][1]
            # Показываем ошибку неверного токена/chat_id и в GUI, но не чаще
            # одного раза на одинаковый текст в рамках запуска.
            self._report_error(f"Ошибка Telegram: {first}", modal=True)

    @staticmethod
    def _digest_listing(listing: dict[str, Any]) -> dict[str, Any]:
        fields = (
            "id",
            "title",
            "price",
            "published_at",
            "city",
            "metro",
            "delivery",
            "views",
            "status",
            "url",
            "market_diff",
            "market_median",
        )
        return {key: listing.get(key) for key in fields}

    def _notification_allowed(self, event: dict[str, Any]) -> bool:
        settings = self.notifications or {}
        kind = str(event.get("kind") or "new")
        query_config = getattr(self, "_query_by_key", {}).get(
            str(event.get("query_key") or "")
        )
        if kind == "new" and not bool(
            query_config.notify_new if query_config else settings.get("new", True)
        ):
            return False
        if kind == "price" and not bool(
            query_config.notify_price
            if query_config
            else settings.get("price", True)
        ):
            return False
        listing = event.get("listing") or {}
        keywords = [
            item.strip().casefold()
            for item in str(settings.get("keywords", "") or "").split(",")
            if item.strip()
        ]
        if keywords:
            title = str(listing.get("title") or "").casefold()
            if not all(word in title for word in keywords):
                return False
        return True

    def _send_digests(
        self,
        events: list[dict[str, Any]],
        query_meta: dict[str, QueryConfig],
    ) -> None:
        """Одна сводка с кнопками на каждый запрос вместо сообщения на лот."""
        if not self.recipients:
            return
        if self.notifications.get("snapshot", True):
            represented = {
                (str(event.get("query_key") or ""), str((event.get("listing") or {}).get("id")))
                for event in events
            }
            for query_key, listings in self._cycle_query_listings.items():
                if query_key not in query_meta:
                    continue
                for listing in listings:
                    key = (query_key, str(listing.get("id")))
                    if key in represented:
                        continue
                    events.append(
                        {
                            "kind": "new",
                            "listing": listing,
                            "query_key": query_key,
                        }
                    )
                    represented.add(key)
        if not events:
            return
        grouped: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            key = str(event.get("query_key") or "")
            grouped.setdefault(key, []).append(event)
        for query_key, group in grouped.items():
            if self.stop_event.is_set():
                break
            new_ids = [
                str((event.get("listing") or {}).get("id"))
                for event in group
                if event.get("kind") == "new"
            ]
            allowed_new = set(
                self.storage.filter_unnotified(query_key, new_ids, "new")
            )
            group = [
                event
                for event in group
                if event.get("kind") != "new"
                or str((event.get("listing") or {}).get("id")) in allowed_new
            ]
            if not group:
                continue
            query_config = query_meta.get(query_key)
            payload: dict[str, Any] = {
                "query_key": query_key,
                "query": query_config.query if query_config else "Без запроса",
                "city": (
                    query_config.city
                    if query_config and query_config.city
                    else "Вся Россия"
                ),
                "recipients": list(self.recipients),
                "new": [],
                "price": [],
                "missing": [],
            }
            try:
                market_stats = market.market_stats(
                    self.storage.get_listings("active")
                )
            except Exception:
                market_stats = {}
            for event in group:
                if not self._notification_allowed(event):
                    continue
                listing = event.get("listing") or {}
                price = listing.get("price")
                stat = market_stats.get(market.group_title(str(listing.get("title") or "")))
                if stat and isinstance(price, int) and stat.median:
                    listing["market_diff"] = price / stat.median - 1
                kind = str(event.get("kind") or "new")
                if kind not in {"new", "price", "missing"}:
                    continue
                item: dict[str, Any] = {
                    "kind": kind,
                    "listing": self._digest_listing(event.get("listing") or {}),
                }
                if kind == "price":
                    item["old_price"] = event.get("old_price")
                payload[kind].append(item)
            sort_key = str(self.notifications.get("sort", "date"))
            for key in ("new", "price", "missing"):
                if sort_key in {"price", "price_asc"}:
                    payload[key].sort(
                        key=lambda item: (
                            (item.get("listing") or {}).get("price") is None,
                            (item.get("listing") or {}).get("price") or 0,
                        )
                    )
                elif sort_key == "price_desc":
                    payload[key].sort(
                        key=lambda item: (
                            (item.get("listing") or {}).get("price") is None,
                            -float((item.get("listing") or {}).get("price") or 0),
                        )
                    )
                elif sort_key == "market_diff":
                    payload[key].sort(
                        key=lambda item: (
                            not isinstance(
                                (item.get("listing") or {}).get("market_diff"),
                                (int, float),
                            ),
                            float(
                                (item.get("listing") or {}).get("market_diff") or 0
                            ),
                        ),
                        reverse=True,
                    )
                else:
                    payload[key].sort(
                        key=lambda item: str(
                            (item.get("listing") or {}).get("published_at") or ""
                        ),
                        reverse=True,
                    )
            total = sum(len(payload[key]) for key in ("new", "price", "missing"))
            if not total:
                continue
            digest_id = secrets.token_hex(4)
            payload["id"] = digest_id
            try:
                self.storage.save_digest(digest_id, payload)
            except Exception:
                logger.exception("Не удалось сохранить сводку Telegram")
                continue
            try:
                report = self.notifier.notify_digest(
                    self.recipients, digest_id, payload
                )
            except Exception as exc:
                self._report_error(f"Ошибка Telegram: {exc}", modal=False)
                continue
            if report.failed:
                self._report_error(
                    f"Ошибка Telegram: {report.failed[0][1]}", modal=True
                )
            else:
                self.storage.mark_notified(
                    query_key,
                    [
                        str((event.get("listing") or {}).get("id"))
                        for event in group
                        if event.get("kind") == "new"
                    ],
                    "new",
                )
                logger.info(
                    "Telegram: отправлена сводка по запросу «%s» (%s событий)",
                    payload["query"],
                    total,
                )

    def _notify_special(self, text: str) -> None:
        if not self.recipients:
            return
        report = self.notifier.send_many(self.recipients, text, context="служебное")
        if report.failed:
            self._report_error(
                f"Ошибка Telegram: {report.failed[0][1]}", modal=True
            )

    def _refresh_gui(self) -> None:
        try:
            self.on_cycle(
                self._last_cycle_listings,
                self.storage.get_stats(),
                self._last_cycle_summary,
                self._last_rejected,
            )
        except Exception:
            logger.debug("Не удалось обновить таблицу", exc_info=True)

    def _auto_export(self) -> None:
        try:
            listings = self._last_cycle_listings
            history = {
                str(listing["id"]): self.storage.get_price_history(str(listing["id"]))
                for listing in listings
            }
            path = export_xlsx(
                listings,
                self._auto_export_path,
                sort_key=self.sort_key,
                price_history=history,
                rejected=self._last_rejected,
            )
            logger.info("Автоматический XLSX обновлён: %s", path)
        except ReporterError as exc:
            self._report_error(str(exc))
        except Exception as exc:
            logger.exception("Ошибка автоматического экспорта XLSX")
            self._report_error(f"Не удалось обновить XLSX: {exc}")

    def _run_cycle(self) -> bool:
        """Один цикл. Возвращает False, если цикл был прерван/пропущен."""
        assert self._scraper is not None
        self.on_status("Сбор объявлений…")
        logger.info("—" * 60)
        logger.info(
            "Новый цикл: запросов %s, получателей %s",
            len(self.queries),
            len(self.recipients),
        )
        events: list[dict[str, Any]] = []
        rejected: list[dict[str, Any]] = []
        successful: list[tuple[str, list[str], str]] = []
        query_meta = {query.query_key(): query for query in self.queries}
        initial_query_keys: set[str] = set()
        current_accepted: dict[str, dict[str, Any]] = {}
        partial_query_keys: set[str] = set()
        found_total = 0
        aborted = False
        abort_notification: str | None = None
        abort_query = ""
        abort_city = ""
        aborted_block = False
        self._cycle_query_listings = {}
        self._query_by_key = {
            query_config.query_key(): query_config for query_config in self.queries
        }

        for query_config in self.queries:
            if not query_config.is_active:
                continue
            if self.stop_event.is_set():
                self._last_cycle_listings = list(current_accepted.values())
                return False
            query = query_config.query
            try:
                criteria = query_config.criteria()
            except FilterError as exc:
                logger.error("Некорректные фильтры запроса «%s»: %s", query, exc)
                abort_query = query
                abort_city = query_config.city
                aborted = True
                abort_notification = str(exc)
                break
            broad = query_config.model_mode == "broad"
            model_query = model_query_from_title(query) if broad else query
            search_query = model_query or query
            query_key = query_config.query_key()
            pages = max(
                1,
                min(
                    30,
                    math.ceil(
                        max(1, query_config.max_listings)
                        / max(1, int(self.scan_limit))
                    ),
                ),
            )
            initial_scan = not query_config.primed
            if initial_scan:
                initial_query_keys.add(query_key)
                logger.info(
                    "Первый цикл запроса «%s»: запоминаю текущую выдачу без уведомлений",
                    query,
                )
            direct_url = None
            if query_config.source_url and not broad:
                direct_url = build_effective_source_url(
                    query_config.source_url,
                    city=criteria.city,
                    price_min=criteria.price_min,
                    price_max=criteria.price_max,
                    local_priority=query_config.local_priority,
                    avito_sort=query_config.avito_sort,
                )
                logger.info("Использую исходную ссылку Avito как основу поиска")
            try:
                raw_listings = self._scraper.search(
                    search_query,
                    city=criteria.city,
                    price_min=criteria.price_min,
                    price_max=criteria.price_max,
                    limit=self.scan_limit,
                    local_priority=query_config.local_priority,
                    direct_url=direct_url,
                    pages=pages,
                    avito_sort=query_config.avito_sort,
                    category="" if broad else query_config.category,
                )
            except ScraperCancelled:
                logger.info("Сбор остановлен пользователем")
                self._last_cycle_listings = list(current_accepted.values())
                return False
            except AvitoBlockedError as exc:
                abort_query = query
                abort_city = criteria.city
                aborted = True
                aborted_block = True
                abort_notification = str(exc)
                logger.warning("Блокировка Avito по запросу «%s»: %s", query, exc)
                break
            except ScraperUnavailableError as exc:
                abort_query = query
                abort_city = criteria.city
                aborted = True
                abort_notification = str(exc)
                logger.error("%s", exc)
                break
            except ScraperError as exc:
                abort_query = query
                abort_city = criteria.city
                aborted = True
                abort_notification = str(exc)
                logger.error("Ошибка сбора по запросу «%s»: %s", query, exc)
                break

            found_total += len(raw_listings)
            if not raw_listings:
                logger.info("По запросу «%s» объявлений не найдено", query)
            if broad:
                filter_criteria = replace(
                    criteria,
                    query=model_query or query,
                    category="",
                    memory="",
                    color="",
                    sim="",
                    condition="",
                )
            elif query_config.source_url:
                filter_criteria = replace(
                    criteria,
                    query="",
                    city="",
                    category="",
                    memory="",
                    color="",
                    sim="",
                    condition="",
                )
            else:
                filter_criteria = criteria
            filter_result = apply_filters(raw_listings, filter_criteria)
            for listing in filter_result.accepted:
                if criteria.city:
                    actual_city = str(listing.get("city") or "").casefold()
                    listing["_scope"] = (
                        "city"
                        if actual_city
                        and criteria.city.casefold() in actual_city
                        else "other"
                    )
                else:
                    listing["_scope"] = "all"
                listing["_query_key"] = query_key
                listing["_query_label"] = query
            if criteria.city and (not query_config.source_url or broad):
                fallback_criteria = replace(filter_criteria, city="")
                logger.info(
                    "В городе «%s» подходит %s из %s; добираю объявления из других городов",
                    criteria.city,
                    len(filter_result.accepted),
                    query_config.max_listings,
                )
                try:
                    other_raw = self._scraper.search(
                        search_query,
                        city="",
                        price_min=criteria.price_min,
                        price_max=criteria.price_max,
                        limit=self.scan_limit,
                        local_priority=False,
                        direct_url=None,
                        pages=pages,
                        avito_sort=query_config.avito_sort,
                        category="" if broad else query_config.category,
                    )
                except ScraperCancelled:
                    logger.info("Сбор остановлен пользователем во время добора по России")
                    self._last_cycle_listings = list(current_accepted.values())
                    return False
                except ScraperError as exc:
                    logger.warning("Не удалось добрать объявления из других городов: %s", exc)
                else:
                    found_total += len(other_raw)
                    other_result = apply_filters(other_raw, fallback_criteria)
                    for listing in other_result.accepted:
                        listing["_scope"] = "other"
                        listing["_query_key"] = query_key
                        listing["_query_label"] = query
                    for listing, _reason in other_result.rejected:
                        listing["_query_key"] = query_key
                        listing["_query_label"] = query
                    filter_result.accepted.extend(other_result.accepted)
                    filter_result.rejected.extend(other_result.rejected)
            for rejected_listing, reason in filter_result.rejected:
                rejected_listing["_query_key"] = query_key
                rejected_listing["_query_label"] = query
                rejected.append(
                    {
                        "query": query,
                        "query_key": query_key,
                        "listing": rejected_listing,
                        "reason": reason,
                    }
                )
            unique_accepted: list[dict[str, Any]] = []
            seen_accepted_ids: set[str] = set()
            for listing in filter_result.accepted:
                listing_id = str(listing.get("id") or "")
                if not listing_id or listing_id in seen_accepted_ids:
                    continue
                seller = str(listing.get("seller") or "")
                if seller and self.storage.is_seller_blacklisted(seller):
                    rejected.append(
                        {
                            "query": query,
                            "query_key": query_key,
                            "listing": listing,
                            "reason": "продавец в чёрном списке",
                        }
                    )
                    continue
                seen_accepted_ids.add(listing_id)
                unique_accepted.append(listing)
            selected = unique_accepted[: query_config.max_listings]
            if len(unique_accepted) > len(selected):
                partial_query_keys.add(query_key)
                logger.info(
                    "Подходящих больше лимита (%s); проверка снятия для этого "
                    "запроса пропущена, чтобы не пометить лишние объявления.",
                    query_config.max_listings,
                )
            if selected:
                # Не открываем карточки повторно: дата и доставка, уже
                # сохранённые в SQLite, используются как кэш.
                reused_ids: set[str] = set()
                new_candidates: list[dict[str, Any]] = []
                date_candidates: list[dict[str, Any]] = []
                for listing in selected:
                    try:
                        known = self.storage.get_listing(str(listing.get("id") or ""))
                    except Exception:
                        known = None
                    if not known:
                        new_candidates.append(listing)
                        date_candidates.append(listing)
                        continue
                    listing_id = str(listing.get("id") or "")
                    current_date = listing.get("published_at")
                    if known.get("published_at") and (
                        not current_date or "T00:00:00" in str(current_date)
                    ):
                        listing["published_at"] = known["published_at"]
                        reused_ids.add(listing_id)
                    if listing.get("delivery") in (None, "", "unknown"):
                        known_delivery = known.get("delivery")
                        if known_delivery not in (None, "", "unknown"):
                            listing["delivery"] = known_delivery
                            reused_ids.add(listing_id)
                    published = listing.get("published_at")
                    if not published or "T00:00:00" in str(published):
                        date_candidates.append(listing)
                if reused_ids:
                    logger.info(
                        "Использованы сохранённые дата/доставка для %s объявлений",
                        len(reused_ids),
                    )
                if initial_scan:
                    logger.info(
                        "Первый цикл: карточки для уточнения дат не открываются"
                    )
                elif self.date_enrich_limit == 0:
                    logger.info("Проверка дат в карточках отключена")
                elif date_candidates:
                    candidates = (
                        list(date_candidates)
                        if self.date_enrich_limit < 0
                        else date_candidates[: self.date_enrich_limit]
                    )
                    limit_label = (
                        "все" if self.date_enrich_limit < 0 else self.date_enrich_limit
                    )
                    logger.info(
                        "Открываю карточки для дат: %s из %s без точной даты "
                        "(лимит %s за цикл)",
                        len(candidates),
                        len(date_candidates),
                        limit_label,
                    )
                    self.on_status("Уточняю даты публикации…")
                    try:
                        self._scraper.enrich_listing_dates(candidates)
                    except ScraperCancelled:
                        logger.info("Уточнение дат остановлено пользователем")
                        self._last_cycle_listings = list(current_accepted.values())
                        return False
            current_ids: list[str] = []
            for listing in selected:
                if self.stop_event.is_set():
                    self._last_cycle_listings = list(current_accepted.values())
                    return False
                result = self.storage.upsert_listing(listing)
                result.listing["_scope"] = listing.get("_scope")
                result.listing["_query_key"] = query_key
                result.listing["_query_label"] = query
                current_ids.append(result.listing["id"])
                current_accepted[result.listing["id"]] = result.listing
                if result.created:
                    if initial_scan:
                        logger.info(
                            "Новое объявление запомнено без уведомления "
                            "(первый цикл запроса): %s",
                            result.listing.get("title", "")[:70],
                        )
                    elif (
                        not self._is_fresh(result.listing)
                        and not bool(
                            query_config.notify_old
                            or self.notifications.get("old", False)
                        )
                    ):
                        logger.info(
                            "Старое объявление не отправлено (старше %.1f ч): %s",
                            self.fresh_hours,
                            result.listing.get("title", "")[:70],
                        )
                    else:
                        events.append(
                            {
                                "kind": "new",
                                "listing": result.listing,
                                "query_key": query_key,
                            }
                        )
                if result.price_changed and not initial_scan:
                    events.append(
                        {
                            "kind": "price",
                            "listing": result.listing,
                            "old_price": result.old_price,
                            "query_key": query_key,
                        }
                    )
                if result.restored:
                    logger.info(
                        "Объявление снова в выдаче: %s",
                        result.listing.get("title", "")[:70],
                    )
                if result.new_price is None and result.listing.get("price") is not None:
                    logger.info(
                        "Цена не прочитана, сохранена прежняя: %s",
                        result.listing.get("title", "")[:70],
                    )
            if initial_scan:
                # Первый цикл: отправить весь снимок запроса, а не только
                # объявления, которых раньше не было в базе.
                sent_ids = {
                    str(event["listing"].get("id"))
                    for event in events
                    if event.get("query_key") == query_key
                    and event.get("kind") == "new"
                }
                for listing_id in current_ids:
                    listing = current_accepted.get(listing_id)
                    if listing is None or str(listing_id) in sent_ids:
                        continue
                    events.append(
                        {
                            "kind": "new",
                            "listing": listing,
                            "query_key": query_key,
                        }
                    )
            self._cycle_query_listings[query_key] = [
                current_accepted[listing_id]
                for listing_id in current_ids
                if listing_id in current_accepted
            ]
            self.storage.record_seen(query_key, current_ids)
            successful.append((query_key, current_ids, query))
            if initial_scan:
                if query_config.id is not None:
                    self.storage.mark_query_primed(query_config.id)
                query_config.primed = True
            logger.info(
                "Запрос «%s»: просмотрено %s, подходит %s, взято %s, "
                "отклонено %s, новых %s",
                query,
                len(raw_listings),
                len(unique_accepted),
                len(selected),
                len(filter_result.rejected),
                sum(
                    1
                    for event in events
                    if event["kind"] == "new"
                    and event["listing"]["id"] in set(current_ids)
                ),
            )

        new_count = sum(1 for event in events if event["kind"] == "new")
        price_count = sum(1 for event in events if event["kind"] == "price")
        missing_count = 0

        if (
            not aborted
            and not self.stop_event.is_set()
            and self.missing_check
        ):
            missing_candidates: dict[str, tuple[str, str]] = {}
            for query_key, current_ids, query_label in successful:
                if query_key in partial_query_keys:
                    continue
                candidates = self.storage.find_missing_seen_ids(query_key, current_ids)
                if candidates:
                    self.storage.mark_seen_missing(query_key, candidates)
                    for listing_id in candidates:
                        missing_candidates.setdefault(
                            listing_id, (query_key, query_label)
                        )
            for listing_id, (query_key, query_label) in missing_candidates.items():
                became_missing, listing = self.storage.refresh_listing_status(listing_id)
                if became_missing and listing:
                    if query_key in initial_query_keys:
                        logger.info(
                            "Снятие запомнено без уведомления (первый цикл): %s",
                            listing.get("title", "")[:70],
                        )
                        continue
                    events.append(
                        {
                            "kind": "missing",
                            "listing": listing,
                            "query_key": query_key,
                        }
                    )
                    missing_count += 1
                    logger.info(
                        "Возможно снято с публикации: %s",
                        listing.get("title", "")[:70],
                    )
        elif aborted:
            logger.warning(
                "Цикл прерван; проверка исчезновения объявлений пропущена, "
                "чтобы не создать ложные уведомления"
            )

        if aborted and abort_notification:
            if aborted_block:
                self._notify_special(
                    TelegramNotifier.blocked_text(abort_query, abort_city)
                )
            else:
                self._notify_special(
                    TelegramNotifier.error_text(abort_notification, abort_query)
                )

        self._send_digests(events, query_meta)

        accepted_total = len(current_accepted)
        self._last_cycle_listings = list(current_accepted.values())
        summary = {
            "found": found_total,
            "accepted": accepted_total,
            "rejected": len(rejected),
            "new": new_count,
            "price": price_count,
            "missing": missing_count,
            "errors": 1 if aborted else 0,
        }
        self._last_rejected = rejected
        self._last_cycle_summary = summary
        logger.info(
            "Цикл завершён: найдено %s, принято %s, отклонено %s, новых %s, "
            "изменение цены %s, снято %s%s",
            found_total,
            accepted_total,
            len(rejected),
            new_count,
            price_count,
            missing_count,
            " (с ошибкой)" if aborted else "",
        )
        self._refresh_gui()
        self._auto_export()
        return not aborted

    def _wait_minutes(self, minutes: int, label: str) -> None:
        seconds = max(0, int(minutes * 60))
        if seconds == 0:
            return
        until = datetime.now() + timedelta(seconds=seconds)
        self.on_status(
            f"{label} Следующий цикл примерно в {until.strftime('%H:%M')}."
        )
        logger.info("%s Следующий цикл примерно в %s", label, until.strftime("%H:%M"))
        end = time.monotonic() + seconds
        while not self.stop_event.is_set():
            # В перерыве между циклами воркер не вызывает scraper._pause(),
            # поэтому обрабатываем кнопку «Браузер» здесь.
            if self._scraper is not None:
                self._scraper._process_toggle_request()
            remaining = end - time.monotonic()
            if remaining <= 0:
                break
            self.stop_event.wait(min(0.5, remaining))

    def run(self) -> None:
        reason = "Работа завершена"
        started_at = time.monotonic()
        try:
            if not self.recipients:
                logger.warning(
                    "Получатели не выбраны: объявления будут собираться без Telegram"
                )
            logger.info(
                "Мониторинг запущен. Режим: %s; интервал/длительность: %s мин; "
                "получателей: %s",
                self.mode_label,
                self.interval_minutes,
                len(self.recipients),
            )
            transport = str(
                self.config.get("scraper", {}).get("transport", "playwright")
            ).lower()
            strategy = str(
                self.config.get("scraper", {}).get("captcha_strategy", "legacy")
            ).lower()
            retry_minutes = max(
                1,
                int(
                    self.config.get("scraper", {}).get("captcha_retry_minutes", 10)
                    if strategy != "legacy"
                    else 5
                ),
            )
            logger.info("Транспорт сбора: %s; стратегия капчи: %s", transport, strategy)
            if strategy in {"auto_only", "manual_then_auto"}:
                # Новые стратегии всегда делают полный первый снимок.
                self.notifications["snapshot"] = True
            if (
                strategy in {"auto_only", "manual_then_auto"}
                or transport in {"http", "auto", "aggressive"}
            ) and HttpAvitoScraper is not None:
                scraper_class = HttpAvitoScraper
            else:
                scraper_class = AvitoScraper
            self._scraper = scraper_class(
                self.config,
                stop_event=self.stop_event,
                on_browser_visibility=self.on_browser_visibility,
            )
            try:
                self._scraper.start()
            except ScraperUnavailableError as exc:
                self._report_error(str(exc), modal=True)
                reason = "Не удалось запустить браузер"
                return

            duration_deadline = (
                started_at + self.interval_minutes * 60
                if self.mode_label == MODE_DURATION
                else None
            )

            while not self.stop_event.is_set():
                cycle_ok = self._run_cycle()
                if strategy != "legacy":
                    if cycle_ok:
                        self._captcha_failures = 0
                        self._captcha_alert_sent = False
                    else:
                        self._captcha_failures += 1
                        alert_after = max(
                            1,
                            int(
                                self.config.get("scraper", {}).get(
                                    "captcha_fail_alert_after", 3
                                )
                            ),
                        )
                        if (
                            self._captcha_failures >= alert_after
                            and not self._captcha_alert_sent
                        ):
                            self._captcha_alert_sent = True
                            text = (
                                "Не удалось автоматически пройти проверку Avito "
                                f"{self._captcha_failures} раз. "
                                "Продолжаю попытки с паузой."
                            )
                            for chat_id in self.recipients:
                                try:
                                    self.notifier.send_message(
                                        chat_id, text, disable_notification=True
                                    )
                                except Exception as exc:
                                    logger.debug("Тихое уведомление не отправлено: %s", exc)
                if self.stop_event.is_set():
                    reason = "Остановлено пользователем"
                    break
                if self.mode_label == MODE_ONCE:
                    reason = "Один прогон завершён"
                    break
                if self.mode_label == MODE_DURATION:
                    if time.monotonic() >= (duration_deadline or 0):
                        reason = (
                            f"Режим «{MODE_DURATION}»: прошло "
                            f"{self.interval_minutes} мин"
                        )
                        break
                    if cycle_ok:
                        self.on_status("Следующий цикл сразу: режим N минут…")
                        self.stop_event.wait(5)
                    else:
                        # После ошибки/капчи не долбим Avito каждые 5 секунд.
                        self._wait_minutes(
                            retry_minutes,
                            f"После ошибки или блокировки пауза {retry_minutes} минут.",
                        )
                    continue
                if cycle_ok:
                    self._wait_minutes(
                        self.interval_minutes,
                        f"Перерыв {self.interval_minutes} мин.",
                    )
                else:
                    self._wait_minutes(
                        retry_minutes,
                        f"После ошибки пауза {retry_minutes} минут. Затем новый цикл.",
                    )
            if reason == "Работа завершена" and self.stop_event.is_set():
                reason = "Остановлено пользователем"
        except ScraperCancelled:
            reason = "Остановлено пользователем"
            logger.info("Сбор остановлен")
        except Exception as exc:
            logger.exception("Непредвиденная ошибка мониторинга")
            self._report_error(f"Непредвиденная ошибка: {exc}", modal=True)
            reason = "Завершено с ошибкой"
        finally:
            if self._scraper:
                try:
                    self._scraper.close()
                except Exception:
                    logger.debug("Ошибка закрытия браузера", exc_info=True)
            self._refresh_gui()
            logger.info("Мониторинг остановлен: %s", reason)
            try:
                self.on_finished(reason)
            except Exception:
                pass


DELIVERY_REVERSE = {value: label for label, value in DELIVERY_LABELS.items()}
DELIVERY_UI_VALUES = ["Есть", "Нет", "Не важно"]
DELIVERY_UI_TO_INTERNAL = {
    "Есть": "есть",
    "Нет": "нет",
    "Не важно": "не важно",
}


class BotJobManager(threading.Thread):
    """Последовательный запуск заданий, созданных в Telegram."""

    def __init__(
        self,
        storage: Storage,
        notifier: TelegramNotifier,
        config: dict[str, Any],
        config_path: str | Path,
        ui_queue: queue.Queue,
        is_manual_busy: Callable[[], bool],
    ) -> None:
        super().__init__(name="AvitoRadarBotJobs", daemon=True)
        self.storage = storage
        self.notifier = notifier
        self.config = config
        self.config_path = Path(config_path)
        self.ui_queue = ui_queue
        self.is_manual_busy = is_manual_busy
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self.current_job_id: int | None = None
        self.current_worker: MonitorWorker | None = None

    def wake(self) -> None:
        self._wake_event.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        if self.current_worker is not None:
            self.current_worker.request_stop()

    def _job_worker(self, job: dict[str, Any]) -> None:
        query_row = self.storage.get_saved_query(int(job["query_id"]))
        if not query_row:
            self.storage.update_bot_job(int(job["id"]), status="error")
            return
        query = QueryConfig.from_row(query_row)
        query.is_active = True
        mode = MODE_ONCE if job["mode"] == "once" else MODE_EVERY_N
        interval = max(1, int(job.get("interval_minutes") or 15))
        job_id = int(job["id"])

        def on_cycle(listings, stats, cycle, rejected):
            next_run = ""
            if job["mode"] == "interval":
                from datetime import timedelta
                next_run = (
                    datetime.now().astimezone()
                    + timedelta(minutes=interval)
                ).strftime("%H:%M")
            self.storage.update_bot_job(
                job_id, first_run_done=1, next_run_at=next_run
            )
            self.ui_queue.put(
                ("cycle", {"listings": listings, "stats": stats, "cycle": cycle, "rejected": rejected})
            )

        worker = MonitorWorker(
            storage=self.storage,
            notifier=self.notifier,
            config=self.config,
            config_path=self.config_path,
            queries=[query],
            recipients=[int(job["chat_id"])],
            mode_label=mode,
            interval_minutes=interval,
            sort_key="date",
            on_status=lambda text: self.ui_queue.put(("status", text)),
            on_error=lambda text, modal: self.ui_queue.put(
                ("error", {"message": text, "modal": modal})
            ),
            on_cycle=on_cycle,
            on_finished=lambda reason: self.ui_queue.put(("bot_job_finished", reason)),
            on_browser_visibility=lambda visible: self.ui_queue.put(
                ("browser_visibility", visible)
            ),
        )
        if not int(job.get("first_run_done") or 0):
            # Для задания из Telegram первый прогон всегда полный снимок.
            worker.notifications["snapshot"] = True
        self.current_job_id = int(job["id"])
        self.current_worker = worker
        self.storage.update_bot_job(
            int(job["id"]),
            status="running",
            last_run_at=datetime.now().isoformat(timespec="seconds"),
        )
        self.storage.set_collector_state(
            {
                "status": "running",
                "kind": "telegram",
                "job_id": int(job["id"]),
                "query": query.query,
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )
        worker.start()
        worker.join()
        latest = self.storage.get_bot_job(int(job["id"])) or {}
        stopped = str(latest.get("status") or "") == "stopped"
        self.storage.update_bot_job(
            int(job["id"]),
            status="stopped" if stopped else ("done" if job["mode"] == "once" else "stopped"),
            first_run_done=1,
            last_run_at=datetime.now().isoformat(timespec="seconds"),
        )
        self.storage.set_collector_state({"status": "idle"})
        self.current_job_id = None
        self.current_worker = None
        self.ui_queue.put(("bot_jobs_changed", None))

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self.current_worker is not None and self.current_worker.is_alive():
                job = (
                    self.storage.get_bot_job(self.current_job_id)
                    if self.current_job_id is not None
                    else None
                )
                if job and str(job.get("status")) == "stopped":
                    self.current_worker.request_stop()
                self._wake_event.wait(2)
                self._wake_event.clear()
                continue
            if self.is_manual_busy():
                self._wake_event.wait(5)
                self._wake_event.clear()
                continue
            jobs = [
                job
                for job in self.storage.list_bot_jobs(active_only=True)
                if job.get("mode") in {"once", "interval"}
                and str(job.get("status")) in {"pending", "waiting"}
            ]
            if not jobs:
                self._wake_event.wait(5)
                self._wake_event.clear()
                continue
            self._job_worker(jobs[0])


class QueryCard(ctk.CTkFrame):
    """Карточка одного запроса со своими фильтрами."""

    def __init__(
        self,
        master: Any,
        app: "AvitoRadarApp",
        query_config: QueryConfig,
        index: int,
    ):
        super().__init__(
            master,
            fg_color=("#ffffff", "#1f2020"),
            corner_radius=10,
            border_width=1,
            border_color=("#d9d9d9", "#303233"),
        )
        self.app = app
        self.db_id = query_config.id
        self.sort_order = query_config.sort_order
        self.avito_sort = int(query_config.avito_sort or 0)
        self.source_url = (query_config.source_url or "").strip()
        self.origin = str(query_config.origin or "app")
        if self.source_url and self.avito_sort == 0:
            try:
                self.avito_sort = int(
                    parse_avito_search_url(self.source_url).get("avito_sort", 0)
                )
            except Exception:
                self.avito_sort = 0
        self.source_frame: ctk.CTkFrame | None = None
        self.index = index
        self._loading = True
        self._expanded = False
        self.is_active_var = ctk.BooleanVar(value=query_config.is_active)
        self.query_notify_new_var = ctk.BooleanVar(value=query_config.notify_new)
        self.query_notify_price_var = ctk.BooleanVar(value=query_config.notify_price)
        self.query_notify_old_var = ctk.BooleanVar(value=query_config.notify_old)
        self.seller_type_var = ctk.StringVar(
            value={
                "private": "Частные",
                "company": "Компании",
            }.get(query_config.seller_type, "Все")
        )
        self.query_var = ctk.StringVar(value=query_config.query)
        self.city_var = ctk.StringVar(value=query_config.city)
        self.local_priority_var = ctk.BooleanVar(value=query_config.local_priority)
        self.price_min_var = ctk.StringVar(
            value="" if query_config.price_min is None else str(query_config.price_min)
        )
        self.price_max_var = ctk.StringVar(
            value="" if query_config.price_max is None else str(query_config.price_max)
        )
        self.include_var = ctk.StringVar(value=query_config.include_words)
        self.exclude_var = ctk.StringVar(value=query_config.exclude_words)
        self.smart_filter_var = ctk.BooleanVar(value=query_config.smart_filter)
        self.category_var = ctk.StringVar(
            value=CATEGORY_REVERSE.get(query_config.category, "Все категории")
        )
        self.memory_var = ctk.StringVar(value=query_config.memory or "Любая")
        self.color_var = ctk.StringVar(value=query_config.color)
        self.sim_var = ctk.StringVar(
            value=SIM_REVERSE.get(query_config.sim, "Любая")
        )
        self.condition_var = ctk.StringVar(
            value=CONDITION_REVERSE.get(query_config.condition, "Любое")
        )

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(7, 5))
        self.toggle_button = ctk.CTkButton(
            header,
            text="Свернуть ▴",
            width=88,
            height=26,
            fg_color=("#e3e9f2", "#263442"),
            hover_color=("#d2dce9", "#31465a"),
            text_color=("#1e4d78", "#b9d8f5"),
            command=self._toggle,
        )
        self.toggle_button.pack(side="left", anchor="n")
        self.active_checkbox = ctk.CTkCheckBox(
            header,
            text="Активен",
            variable=self.is_active_var,
            command=self._on_change,
            font=ctk.CTkFont(size=10),
            checkbox_width=16,
            checkbox_height=16,
        )
        self.active_checkbox.pack(side="left", anchor="n", padx=(6, 0))
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.pack(side="left", fill="x", expand=True, padx=(8, 4))
        self.title_label = ctk.CTkLabel(
            title_box,
            text="",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
            justify="left",
        )
        self.title_label.pack(fill="x", anchor="w")
        self.summary_label = ctk.CTkLabel(
            title_box,
            text="",
            font=ctk.CTkFont(size=10),
            text_color=("#6f7c8c", "#93a0ae"),
            anchor="w",
            justify="left",
            wraplength=370,
        )
        self.summary_label.pack(fill="x", anchor="w")
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=8, pady=(0, 5))
        ctk.CTkButton(
            actions,
            text="Проверить",
            width=96,
            height=25,
            font=ctk.CTkFont(size=11),
            fg_color=("#e3f0e3", "#263a26"),
            hover_color=("#d2e5d2", "#304a30"),
            text_color=("#246b24", "#b9e3b9"),
            command=self._preview,
        ).pack(side="left")
        if getattr(self, "origin", "app") == "telegram":
            ctk.CTkButton(
                actions,
                text="Остановить",
                width=100,
                height=25,
                font=ctk.CTkFont(size=11),
                fg_color=("#f2e3c2", "#4a3b1d"),
                hover_color=("#e5d3aa", "#5c4a24"),
                text_color=("#7a5200", "#ffe0a3"),
                command=lambda: self.app._stop_telegram_query(self),
            ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            actions,
            text="Удалить",
            width=86,
            height=25,
            font=ctk.CTkFont(size=11),
            fg_color=("#f0e2e2", "#3a2626"),
            hover_color=("#e4d3d3", "#4a3030"),
            text_color=("#8f2020", "#ffb3b3"),
            command=lambda: self.app._delete_query_card(self),
        ).pack(side="left", padx=(6, 0))

        self.body = ctk.CTkFrame(self, fg_color="transparent")
        self.body.pack(fill="x", padx=8, pady=(0, 8))

        if self.source_url:
            self._build_source_block()

        ctk.CTkLabel(
            self.body, text="Поисковый запрос", anchor="w", font=ctk.CTkFont(size=11)
        ).pack(fill="x")
        self.query_entry = ctk.CTkEntry(
            self.body, textvariable=self.query_var, placeholder_text="Например: iPhone 15"
        )
        self.query_entry.pack(fill="x", pady=(1, 5))
        for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>"):
            self.query_entry.bind(sequence, self._paste_query_entry)
            inner = getattr(self.query_entry, "_entry", None)
            if inner is not None:
                inner.bind(sequence, self._paste_query_entry)
        self.source_note = ctk.CTkLabel(
            self.body,
            text=(
                "Ссылка используется как есть: фильтры Avito из неё сохраняются."
                if self.source_url
                else ""
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        )
        self.source_note.pack(fill="x")
        if not self.source_url:
            self.source_note.pack_forget()

        mode_row = ctk.CTkFrame(self.body, fg_color="transparent")
        self.mode_row = mode_row
        mode_row.pack(fill="x", pady=2)
        ctk.CTkLabel(mode_row, text="Режим модели", width=130, anchor="w").pack(side="left")
        self.model_mode_var = ctk.StringVar(
            value="Строгий" if query_config.model_mode == "strict" else "Широкий"
        )
        self.model_mode_combo = ctk.CTkComboBox(
            mode_row,
            values=["Строгий", "Широкий"],
            variable=self.model_mode_var,
            command=lambda _value: self._on_change(),
            width=180,
        )
        self.model_mode_combo.pack(side="left")
        ctk.CTkLabel(
            self.body,
            text=(
                "Строгий: Pro не пропустит Pro Max и другие модели. "
                "Широкий: достаточно слов запроса, варианты модели допускаются."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        ).pack(fill="x", pady=(0, 2))

        spec_row = ctk.CTkFrame(self.body, fg_color="transparent")
        spec_row.pack(fill="x", pady=2)
        ctk.CTkLabel(spec_row, text="Категория", width=130, anchor="w").pack(side="left")
        self.category_menu = ctk.CTkOptionMenu(
            spec_row,
            values=list(CATEGORY_LABELS),
            variable=self.category_var,
            command=lambda _v: self._on_change(),
            width=180,
            dynamic_resizing=False,
        )
        self.category_menu.pack(side="left")
        spec_row_memory = ctk.CTkFrame(self.body, fg_color="transparent")
        spec_row_memory.pack(fill="x", pady=2)
        ctk.CTkLabel(spec_row_memory, text="Память", width=130, anchor="w").pack(side="left")
        self.memory_menu = ctk.CTkOptionMenu(
            spec_row_memory,
            values=MEMORY_LABELS,
            variable=self.memory_var,
            command=lambda _v: self._on_change(),
            width=180,
            dynamic_resizing=False,
        )
        self.memory_menu.pack(side="left")

        spec_row2 = ctk.CTkFrame(self.body, fg_color="transparent")
        spec_row2.pack(fill="x", pady=2)
        ctk.CTkLabel(spec_row2, text="Цвет", width=130, anchor="w").pack(side="left")
        self.color_entry = ctk.CTkEntry(
            spec_row2, textvariable=self.color_var, placeholder_text="любой", width=180
        )
        self.color_entry.pack(side="left")
        self.color_entry.bind("<KeyRelease>", lambda _event: self._on_change())
        self.color_entry.bind("<FocusOut>", lambda _event: self._on_change())
        spec_row_sim = ctk.CTkFrame(self.body, fg_color="transparent")
        spec_row_sim.pack(fill="x", pady=2)
        ctk.CTkLabel(spec_row_sim, text="SIM", width=130, anchor="w").pack(side="left")
        self.sim_menu = ctk.CTkOptionMenu(
            spec_row_sim,
            values=SIM_LABELS,
            variable=self.sim_var,
            command=lambda _v: self._on_change(),
            width=180,
            dynamic_resizing=False,
        )
        self.sim_menu.pack(side="left")
        spec_row3 = ctk.CTkFrame(self.body, fg_color="transparent")
        spec_row3.pack(fill="x", pady=2)
        ctk.CTkLabel(spec_row3, text="Состояние", width=130, anchor="w").pack(side="left")
        self.condition_menu = ctk.CTkOptionMenu(
            spec_row3,
            values=CONDITION_LABELS,
            variable=self.condition_var,
            command=lambda _v: self._on_change(),
            width=180,
            dynamic_resizing=False,
        )
        self.condition_menu.pack(side="left")

        self.smart_filter_checkbox = ctk.CTkCheckBox(
            self.body,
            text="Исключать неисправные и аксессуары",
            variable=self.smart_filter_var,
            command=self._on_change,
            font=ctk.CTkFont(size=11),
            checkbox_width=18,
            checkbox_height=18,
        )
        self.smart_filter_checkbox.pack(anchor="w", pady=(3, 2))
        self.smart_filter_hint = ctk.CTkLabel(
            self.body,
            text="Смарт-фильтр убирает запчасти, «не работает», чехлы, кабели и похожий мусор.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        )
        self.smart_filter_hint.pack(fill="x", pady=(0, 3))
        if self.source_url:
            for frame in (
                spec_row,
                spec_row_memory,
                spec_row2,
                spec_row_sim,
                spec_row3,
            ):
                frame.pack_forget()
            self.smart_filter_checkbox.pack_forget()
            self.smart_filter_hint.pack_forget()
            ctk.CTkLabel(
                self.body,
                text="Характеристики задаются ссылкой Avito. Для других фильтров вставьте новую ссылку.",
                font=ctk.CTkFont(size=10),
                text_color=("#777777", "#8f8f8f"),
                anchor="w",
                justify="left",
                wraplength=395,
            ).pack(fill="x", pady=(0, 3))

        query_notify_row = ctk.CTkFrame(self.body, fg_color="transparent")
        query_notify_row.pack(fill="x", pady=(2, 3))
        ctk.CTkLabel(
            query_notify_row, text="Уведомления", width=100, anchor="w"
        ).pack(side="left")
        ctk.CTkCheckBox(
            query_notify_row,
            text="Новые",
            variable=self.query_notify_new_var,
            command=self._on_change,
            checkbox_width=16,
            checkbox_height=16,
        ).pack(side="left")
        ctk.CTkCheckBox(
            query_notify_row,
            text="Цена",
            variable=self.query_notify_price_var,
            command=self._on_change,
            checkbox_width=16,
            checkbox_height=16,
        ).pack(side="left", padx=(10, 0))
        ctk.CTkCheckBox(
            query_notify_row,
            text="Старые",
            variable=self.query_notify_old_var,
            command=self._on_change,
            checkbox_width=16,
            checkbox_height=16,
        ).pack(side="left", padx=(10, 0))

        city_row = ctk.CTkFrame(self.body, fg_color="transparent")
        city_row.pack(fill="x", pady=2)
        ctk.CTkLabel(city_row, text="Город", width=56, anchor="w").pack(side="left")
        ctk.CTkEntry(
            city_row,
            textvariable=self.city_var,
            placeholder_text="Вся Россия",
        ).pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            self.body,
            text="Пусто — поиск по всей России.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
        ).pack(fill="x", pady=(0, 2))
        self.local_priority_checkbox = ctk.CTkCheckBox(
            self.body,
            text="Сначала в выбранном городе",
            variable=self.local_priority_var,
            command=self._on_change,
            font=ctk.CTkFont(size=11),
            checkbox_width=18,
            checkbox_height=18,
        )
        self.local_priority_checkbox.pack(anchor="w", pady=(2, 1))
        ctk.CTkLabel(
            self.body,
            text="Приоритет применяется только если указан город.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
        ).pack(fill="x", pady=(0, 3))

        limit_row = ctk.CTkFrame(self.body, fg_color="transparent")
        limit_row.pack(fill="x", pady=2)
        ctk.CTkLabel(
            limit_row, text="Объявлений за прогон", width=150, anchor="w"
        ).pack(side="left")
        self.max_listings_combo = ctk.CTkComboBox(
            limit_row,
            values=["5", "10", "20", "30", "50"],
            width=100,
            command=lambda _v: self._on_change(),
        )
        self.max_listings_combo.set(str(query_config.max_listings))
        self.max_listings_combo.pack(side="left")
        self.max_listings_combo.bind(
            "<KeyRelease>", lambda _event: self._on_change()
        )
        self.max_listings_combo.bind(
            "<FocusOut>", lambda _event: self._on_change()
        )
        self.max_listings_combo.bind(
            "<Return>", lambda _event: self._on_change()
        )
        ctk.CTkLabel(
            limit_row,
            text="можно ввести своё",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            self.body,
            text="Страницы подбираются автоматически: до 50 объявлений на страницу.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
        ).pack(fill="x", pady=(0, 2))

        sort_row = ctk.CTkFrame(self.body, fg_color="transparent")
        sort_row.pack(fill="x", pady=2)
        ctk.CTkLabel(sort_row, text="Сортировка Авито", width=130, anchor="w").pack(side="left")
        self.avito_sort_labels = {
            "По умолчанию": 0,
            "Дешевле": 1,
            "Дороже": 2,
            "По дате": 104,
            "По размеру скидки": 172297,
        }
        reverse_sort = {value: label for label, value in self.avito_sort_labels.items()}
        self.avito_sort_var = ctk.StringVar(
            value=reverse_sort.get(self.avito_sort, "По умолчанию")
        )
        self.avito_sort_combo = ctk.CTkComboBox(
            sort_row,
            values=list(self.avito_sort_labels),
            variable=self.avito_sort_var,
            command=lambda _value: self._on_change(),
            width=180,
        )
        self.avito_sort_combo.pack(side="left")
        self.sort_hint = ctk.CTkLabel(
            self.body,
            text="Сортировка применяется к выдаче Avito, а не к таблице.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        )
        self.sort_hint.pack(fill="x", pady=(0, 2))
        sort_row.pack_forget()
        if spec_row.winfo_manager():
            sort_row.pack(fill="x", pady=2, before=spec_row)
        else:
            sort_row.pack(fill="x", pady=2, before=self.mode_row)
        self.sort_hint.pack_forget()
        self.sort_hint.pack(fill="x", pady=(0, 2), after=sort_row)

        price_row = ctk.CTkFrame(self.body, fg_color="transparent")
        price_row.pack(fill="x", pady=2)
        ctk.CTkLabel(price_row, text="Цена, ₽", width=56, anchor="w").pack(side="left")
        ctk.CTkEntry(
            price_row,
            textvariable=self.price_min_var,
            placeholder_text="без мин.",
            width=92,
        ).pack(side="left")
        ctk.CTkLabel(price_row, text="—", width=18).pack(side="left")
        ctk.CTkEntry(
            price_row,
            textvariable=self.price_max_var,
            placeholder_text="без макс.",
            width=92,
        ).pack(side="left")

        ctk.CTkLabel(
            self.body,
            text="Авито Доставка",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", pady=(5, 1))
        self.delivery_segment = ctk.CTkSegmentedButton(
            self.body,
            values=DELIVERY_UI_VALUES,
            command=lambda _v: self._on_change(),
            height=28,
        )
        self.delivery_segment.set(
            {"require": "Есть", "exclude": "Нет"}.get(
                query_config.delivery, "Не важно"
            )
        )
        self.delivery_segment.pack(fill="x")

        ctk.CTkLabel(
            self.body, text="Продавцы", anchor="w", font=ctk.CTkFont(size=11)
        ).pack(fill="x", pady=(5, 1))
        self.seller_type_combo = ctk.CTkSegmentedButton(
            self.body,
            values=["Все", "Частные", "Компании"],
            variable=self.seller_type_var,
            command=lambda _value: self._on_change(),
            height=28,
        )
        self.seller_type_combo.pack(fill="x")

        ctk.CTkLabel(
            self.body,
            text="Обязательные слова в заголовке",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", pady=(5, 0))
        ctk.CTkEntry(
            self.body,
            textvariable=self.include_var,
            placeholder_text="не важно: 128 ГБ, eSIM",
        ).pack(fill="x", pady=(1, 2))

        ctk.CTkLabel(
            self.body,
            text="Исключить слова в заголовке",
            anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(fill="x", pady=(4, 0))
        ctk.CTkEntry(
            self.body,
            textvariable=self.exclude_var,
            placeholder_text="ничего не исключать: битый",
        ).pack(fill="x", pady=(1, 1))
        ctk.CTkLabel(
            self.body,
            text="Обязательные слова должны встречаться все; любого исключённого слова достаточно для отказа.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        ).pack(fill="x", pady=(1, 2))

        self.body.pack_forget()
        self.toggle_button.configure(text="Фильтры ▾")
        self._loading = False
        for variable in (
            self.query_var,
            self.city_var,
            self.price_min_var,
            self.price_max_var,
            self.include_var,
            self.exclude_var,
        ):
            variable.trace_add("write", lambda *_: self._on_change())
        self._update_title()

    def _build_source_block(self) -> None:
        self.source_frame = ctk.CTkFrame(
            self.body,
            fg_color=("#eef4fb", "#1b2a38"),
            corner_radius=8,
        )
        self.source_frame.pack(fill="x", pady=(0, 7))
        header = ctk.CTkFrame(self.source_frame, fg_color="transparent")
        header.pack(fill="x", padx=8, pady=(6, 2))
        ctk.CTkLabel(
            header,
            text="Источник: ссылка Avito",
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#285a86", "#9dc9f0"),
        ).pack(side="left")
        ctk.CTkButton(
            header,
            text="Очистить ссылку",
            width=112,
            height=23,
            fg_color=("#d4e3f2", "#263f55"),
            hover_color=("#c3d7ea", "#34536e"),
            text_color=("#1e4d78", "#b9d8f5"),
            command=self._clear_source_url,
        ).pack(side="right")
        display = self.source_url
        if len(display) > 100:
            display = display[:97] + "…"
        ctk.CTkLabel(
            self.source_frame,
            text=display,
            anchor="w",
            justify="left",
            wraplength=395,
            font=ctk.CTkFont(size=9),
            text_color=("#49677f", "#9aaebb"),
        ).pack(fill="x", padx=8, pady=(0, 2))
        ctk.CTkLabel(
            self.source_frame,
            text=(
                "Поиск идёт по этой ссылке. Заполненные город, цена и приоритет "
                "ниже переопределяют её параметры; название запроса — только для отображения."
            ),
            anchor="w",
            justify="left",
            wraplength=395,
            font=ctk.CTkFont(size=9),
            text_color=("#49677f", "#9aaebb"),
        ).pack(fill="x", padx=8, pady=(0, 6))

    def _paste_query_entry(self, _event: Any = None) -> str:
        try:
            text = str(self.clipboard_get() or "").strip()
        except Exception:
            return "break"
        if text:
            self.query_entry.insert("insert", text)
            self.query_entry.icursor("end")
            self._on_change()
        return "break"

    def _duplicate(self) -> None:
        try:
            config = self.get_config(validate=True)
            row = self.app.storage.save_saved_query(
                None,
                query=config.query,
                city=config.city,
                local_priority=config.local_priority,
                max_listings=config.max_listings,
                pages=config.pages,
                price_min=config.price_min,
                price_max=config.price_max,
                delivery=config.delivery,
                seller_type=config.seller_type,
                include_words=config.include_words,
                exclude_words=config.exclude_words,
                source_url=config.source_url,
                avito_sort=config.avito_sort,
                model_mode=config.model_mode,
                smart_filter=config.smart_filter,
                category=config.category,
                memory=config.memory,
                color=config.color,
                sim=config.sim,
                condition=config.condition,
                is_active=True,
                notify_new=config.notify_new,
                notify_price=config.notify_price,
                notify_old=config.notify_old,
            )
            self.app._create_query_card(QueryConfig.from_row(row))
            self.app._schedule_queries_save()
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Не удалось скопировать запрос:\n{exc}")

    def _preview(self) -> None:
        try:
            config = self.get_config(validate=True)
        except Exception as exc:
            messagebox.showerror("Avito Radar", str(exc))
            return
        self.app._preview_query_async(config)

    def _clear_source_url(self) -> None:
        if not self.source_url:
            return
        self.source_url = ""
        if self.source_frame is not None:
            self.source_frame.destroy()
            self.source_frame = None
        self.source_note.configure(text="")
        self.source_note.pack_forget()
        self._update_title()
        self.app._schedule_queries_save()

    def _update_title(self) -> None:
        query = self.query_var.get().strip() or "Новый запрос"
        label = query[:48]
        if getattr(self, "origin", "app") == "telegram":
            label = f"{label} · Telegram"
        self.title_label.configure(text=label)
        parts: list[str] = []
        city = self.city_var.get().strip()
        if city:
            priority = " (сначала здесь)" if self.local_priority_var.get() else ""
            parts.append(f"{city}{priority}")
        else:
            parts.append("Вся Россия")
        if self.price_min_var.get().strip():
            parts.append(f"от {self.price_min_var.get().strip()} ₽")
        if self.price_max_var.get().strip():
            parts.append(f"до {self.price_max_var.get().strip()} ₽")
        if self.include_var.get().strip():
            parts.append("+ " + self.include_var.get().strip())
        if self.exclude_var.get().strip():
            parts.append("− " + self.exclude_var.get().strip())
        if self.smart_filter_var.get():
            parts.append("смарт-фильтр")
        if self.source_url:
            parts.append("по ссылке Avito")
        if getattr(self, "origin", "app") == "telegram" and self.db_id is not None:
            try:
                jobs = [
                    job
                    for job in self.app.storage.list_bot_jobs()
                    if int(job.get("query_id") or 0) == int(self.db_id)
                ]
                if jobs:
                    status = str(jobs[-1].get("status") or "pending")
                    labels = {
                        "pending": "ожидает",
                        "running": "выполняется",
                        "done": "выполнено",
                        "stopped": "остановлено",
                        "error": "ошибка",
                    }
                    parts.append("Telegram: " + labels.get(status, status))
            except Exception:
                pass
        limit = self.max_listings_combo.get().strip()
        if limit:
            parts.append(f"до {limit} объявл.")
        self.summary_label.configure(
            text=" · ".join(parts) if parts else "Без дополнительных фильтров"
        )

    def set_index(self, index: int) -> None:
        self.index = index
        self._update_title()

    def _on_change(self) -> None:
        if self._loading:
            return
        self._update_title()
        self.app._schedule_queries_save()

    def _toggle(self) -> None:
        if self._expanded:
            self.body.pack_forget()
            self.toggle_button.configure(text="Фильтры ▾")
            self._expanded = False
        else:
            self.body.pack(fill="x", padx=8, pady=(0, 8), after=self.toggle_button.master)
            self.toggle_button.configure(text="Свернуть ▴")
            self._expanded = True

    def get_config(self, *, validate: bool = True) -> QueryConfig:
        query = self.query_var.get().strip()
        if not query:
            raise FilterError("Поисковый запрос не заполнен")
        limit_text = self.max_listings_combo.get().strip()
        if not re.fullmatch(r"\d{1,3}", limit_text):
            raise FilterError(
                f"Запрос «{query}»: лимит объявлений должен быть числом от 1 до 500"
            )
        max_listings = int(limit_text)
        if not 1 <= max_listings <= 500:
            raise FilterError(
                f"Запрос «{query}»: лимит объявлений должен быть от 1 до 500"
            )
        try:
            criteria = criteria_from_values(
                price_min=self.price_min_var.get(),
                price_max=self.price_max_var.get(),
                delivery_label=DELIVERY_UI_TO_INTERNAL.get(
                    self.delivery_segment.get(), "не важно"
                ),
                city=self.city_var.get(),
                include_words=self.include_var.get(),
                exclude_words=self.exclude_var.get(),
            )
        except FilterError as exc:
            if validate:
                raise FilterError(f"Запрос «{query}»: {exc}") from exc
            raise
        return QueryConfig(
            id=self.db_id,
            query=query,
            city=criteria.city,
            local_priority=bool(self.local_priority_var.get()),
            price_min=criteria.price_min,
            price_max=criteria.price_max,
            delivery=criteria.delivery,
            seller_type={
                "Частные": "private",
                "Компании": "company",
            }.get(self.seller_type_var.get(), "any"),
            include_words=self.include_var.get().strip(),
            exclude_words=self.exclude_var.get().strip(),
            max_listings=max_listings,
            source_url=self.source_url,
            sort_order=self.sort_order,
            avito_sort=self.avito_sort_labels.get(
                self.avito_sort_var.get(), 0
            ),
            model_mode="broad" if self.model_mode_var.get() == "Широкий" else "strict",
            smart_filter=bool(self.smart_filter_var.get()),
            category=CATEGORY_LABELS.get(self.category_var.get(), ""),
            memory="" if self.memory_var.get() == "Любая" else self.memory_var.get(),
            color=self.color_var.get().strip(),
            sim=SIM_VALUES.get(self.sim_var.get(), ""),
            condition=CONDITION_VALUES.get(self.condition_var.get(), ""),
            is_active=bool(self.is_active_var.get()),
            notify_new=bool(self.query_notify_new_var.get()),
            notify_price=bool(self.query_notify_price_var.get()),
            notify_old=bool(self.query_notify_old_var.get()),
        )

    def mark_saved(self, row: dict[str, Any]) -> None:
        self.db_id = int(row["id"])
        self.sort_order = int(row.get("sort_order") or 0)
        self.avito_sort = int(row.get("avito_sort") or 0)
        self.model_mode = str(row.get("model_mode") or "strict")
        self.smart_filter_var.set(bool(row.get("smart_filter", 0)))
        self.category_var.set(
            CATEGORY_REVERSE.get(str(row.get("category") or ""), "Все категории")
        )
        self.memory_var.set(str(row.get("memory") or "Любая"))
        self.color_var.set(str(row.get("color") or ""))
        self.sim_var.set(SIM_REVERSE.get(str(row.get("sim") or ""), "Любая"))
        self.condition_var.set(
            CONDITION_REVERSE.get(str(row.get("condition") or ""), "Любое")
        )
        self.is_active_var.set(bool(row.get("is_active", 1)))
        self.query_notify_new_var.set(bool(row.get("notify_new", 1)))
        self.query_notify_price_var.set(bool(row.get("notify_price", 1)))
        self.query_notify_old_var.set(bool(row.get("notify_old", 0)))
        self.seller_type = str(row.get("seller_type") or "any")
        self.seller_type_var.set(
            {
                "private": "Частные",
                "company": "Компании",
            }.get(self.seller_type, "Все")
        )
        reverse = {value: label for label, value in self.avito_sort_labels.items()}
        self.avito_sort_var.set(reverse.get(self.avito_sort, "По умолчанию"))
        self.model_mode_var.set(
            "Широкий" if self.model_mode == "broad" else "Строгий"
        )


class AvitoRadarApp(ctk.CTk):
    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        config: dict[str, Any] | None = None,
    ):
        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title("Avito Radar")
        self.geometry("1320x900")
        self.minsize(1120, 760)
        self.grid_columnconfigure(0, weight=0, minsize=470)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.config_path = Path(config_path) if config_path else APP_DIR / "config.toml"
        self.config = config or load_config(self.config_path)
        self._last_worker_status = "Ожидание"
        self._status_restore_after_id: str | None = None
        set_preferred_browser(str(self.config.get("app", {}).get("browser", "auto")))
        self.browser_toggle_available = bool(
            self.config.get("scraper", {}).get("hidden_window", True)
            and self.config.get("scraper", {}).get("transport", "auto")
            in {"browser", "auto"}
        )
        self.ui_queue: queue.Queue = queue.Queue()
        self.worker: MonitorWorker | None = None
        self.bot_listener: BotListener | None = None
        self.query_cards: list[QueryCard] = []
        self._query_save_after_id: str | None = None
        self.last_rejected: list[dict[str, Any]] = []
        self.last_cycle_listings: list[dict[str, Any]] = []
        self.recipient_vars: dict[int, Any] = {}
        self._recipients_loaded = False
        self._last_modal: tuple[str, float] = ("", 0.0)
        self._closing = False
        self._log_handler = GuiLogHandler(self.ui_queue)
        self._log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(self._log_handler)

        try:
            self.storage = Storage(
                app_path(self.config, "database_path", "data/avito_radar.db")
            )
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar",
                f"Не удалось открыть базу данных:\n{exc}\n\nПроверьте config.toml.",
            )
            self.destroy()
            raise
        self.notifier = TelegramNotifier(self.config_path)
        self._build_ui()
        self.after(150, lambda: apply_light_titlebar(self))
        self._load_query_cards()
        self._refresh_recipients()
        self.table.set_data([], [])
        self.market_table.set_rows([])
        self._market_by_id = {}
        self._restore_ui_state()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind_all("<Control-KeyPress>", self._global_paste_event, add="+")
        self.bind_all("<Shift-Insert>", self._global_paste, add="+")
        self.bind_all("<<Paste>>", self._global_paste, add="+")
        self.bind("<FocusIn>", lambda _event: apply_light_titlebar(self), add="+")
        self.bot_manager = BotJobManager(
            self.storage,
            self.notifier,
            self.config,
            self.config_path,
            self.ui_queue,
            lambda: bool(getattr(self, "worker", None) and self.worker.is_alive()),
        )
        self.bot_manager.start()
        self._poll_after_id = self.after(120, self._poll_queue)
        self._start_bot_listener()
        count = len(self.storage.get_subscribers())
        logger.info(
            "Avito Radar запущен. Подписчиков: %s. Лог: %s",
            count,
            app_path(self.config, "log_path", "logs/avito_radar.log"),
        )

    # ------------------------------- UI build -------------------------------

    def _build_ui(self) -> None:
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, columnspan=2, sticky="ew", padx=18, pady=(14, 6))
        header.grid_columnconfigure(0, weight=1)
        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_box,
            text="Avito Radar",
            font=ctk.CTkFont(size=26, weight="bold"),
            anchor="w",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_box,
            text="Мониторинг объявлений Avito и уведомления в Telegram",
            text_color=("#555555", "#a9a9a9"),
            font=ctk.CTkFont(size=12),
            anchor="w",
        ).pack(anchor="w")
        self.status_label = ctk.CTkLabel(
            header,
            text="Ожидание",
            corner_radius=14,
            fg_color=("#e7e7e7", "#2d2f31"),
            text_color=("#333333", "#e0e0e0"),
            padx=16,
            pady=7,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        self.status_label.grid(row=0, column=1, sticky="e")

        left_panel = ctk.CTkFrame(self, fg_color="transparent")
        left_panel.grid(row=1, column=0, sticky="nsew", padx=(14, 7), pady=6)
        left_panel.grid_rowconfigure(0, weight=1)
        left_panel.grid_columnconfigure(0, weight=1)
        self.settings_shell = ctk.CTkFrame(
            left_panel,
            width=452,
            fg_color=("#f4f5f7", "#171818"),
            corner_radius=12,
        )
        self.settings_shell.grid(row=0, column=0, sticky="nsew")
        self.settings_shell.grid_rowconfigure(0, weight=1)
        self.settings_shell.grid_columnconfigure(0, weight=1)
        self.settings_canvas = tk.Canvas(
            self.settings_shell,
            background="#171818",
            highlightthickness=0,
            bd=0,
            yscrollincrement=14,
        )
        self.settings_canvas.grid(row=0, column=0, sticky="nsew", padx=(2, 0), pady=2)
        self.settings_scrollbar = ctk.CTkScrollbar(
            self.settings_shell,
            orientation="vertical",
            command=self.settings_canvas.yview,
            width=14,
            fg_color="#171818",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        self.settings_scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, 2), pady=2)
        self.settings_canvas.configure(yscrollcommand=self.settings_scrollbar.set)
        self.settings = ctk.CTkFrame(
            self.settings_canvas,
            fg_color=("#f4f5f7", "#171818"),
            corner_radius=0,
        )
        self._settings_window = self.settings_canvas.create_window(
            (0, 0), window=self.settings, anchor="nw"
        )
        self.settings.bind(
            "<Configure>",
            lambda _event: self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all")
            ),
        )
        self.settings_canvas.bind(
            "<Configure>",
            lambda event: self.settings_canvas.itemconfigure(
                self._settings_window, width=event.width
            ),
        )
        self.settings_canvas.bind_all(
            "<MouseWheel>", self._on_settings_wheel, add="+"
        )
        self.settings.grid_columnconfigure(0, weight=1)
        self._build_action_bar(left_panel)

        table_frame = ctk.CTkFrame(self, corner_radius=12)
        table_frame.grid(row=1, column=1, sticky="nsew", padx=(7, 14), pady=6)
        table_frame.grid_rowconfigure(4, weight=1)
        table_frame.grid_columnconfigure(0, weight=1)
        table_frame.grid_columnconfigure(1, weight=0)
        query_row = ctk.CTkFrame(table_frame, fg_color="transparent")
        query_row.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 2))
        query_row.grid_columnconfigure(0, minsize=130)
        query_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            query_row, text="Запрос", font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        self.query_select_var = ctk.StringVar(value="Все запросы")
        self.query_select_menu = ctk.CTkComboBox(
            query_row,
            values=["Все запросы"],
            variable=self.query_select_var,
            command=lambda _value: self._apply_query_filter(),
            width=310,
        )
        self.query_select_menu.grid(row=0, column=1, sticky="w", padx=(8, 12))
        self.query_select_menu.set("Все запросы")
        self.filter_stats_label = ctk.CTkLabel(
            query_row,
            text="Цикл ещё не запускался",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=("#555555", "#a8a8a8"),
        )
        self.filter_stats_label.grid(row=0, column=2, sticky="w")
        quick_row = ctk.CTkFrame(table_frame, fg_color="transparent")
        quick_row.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 5))
        quick_row.grid_columnconfigure(0, minsize=130)
        quick_row.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(
            quick_row, text="Быстрый поиск", font=ctk.CTkFont(size=11), anchor="w"
        ).grid(row=0, column=0, sticky="w")
        self.quick_filter_var = ctk.StringVar()
        self.quick_filter_entry = ctk.CTkEntry(
            quick_row,
            textvariable=self.quick_filter_var,
            placeholder_text="заголовок или город",
        )
        self.quick_filter_entry.grid(row=0, column=1, sticky="ew", padx=(8, 6))
        self.quick_filter_entry.bind(
            "<KeyRelease>",
            lambda _event: self.table.set_quick_filter(self.quick_filter_var.get()),
        )
        ctk.CTkButton(
            quick_row,
            text="Сбросить",
            width=80,
            height=24,
            command=self._clear_table_filters,
        ).grid(row=0, column=2, padx=(0, 6))
        self.filters_toggle = ctk.CTkButton(
            quick_row,
            text="Фильтры ▾",
            width=90,
            height=24,
            fg_color=("#e3e9f2", "#263442"),
            hover_color=("#d2dce9", "#31465a"),
            text_color=("#1e4d78", "#b9d8f5"),
            command=self._toggle_advanced_filters,
        )
        self.filters_toggle.grid(row=0, column=3)
        filter_row = ctk.CTkFrame(table_frame, fg_color="transparent")
        self.advanced_filters = filter_row
        filter_row.grid(row=3, column=0, sticky="ew", padx=14, pady=(0, 5))
        filter_row.grid_remove()
        ctk.CTkLabel(
            filter_row, text="Цена", width=130, anchor="w",
            font=ctk.CTkFont(size=11),
        ).pack(side="left")
        self.price_from_var = ctk.StringVar()
        self.price_to_var = ctk.StringVar()
        self.price_from_entry = ctk.CTkEntry(
            filter_row, textvariable=self.price_from_var, placeholder_text="Цена от", width=100
        )
        self.price_from_entry.pack(side="left", padx=(5, 0))
        self.price_to_entry = ctk.CTkEntry(
            filter_row, textvariable=self.price_to_var, placeholder_text="Цена до", width=100
        )
        self.price_to_entry.pack(side="left", padx=(4, 8))
        self.only_delivery_var = ctk.BooleanVar()
        ctk.CTkCheckBox(
            filter_row,
            text="С доставкой",
            variable=self.only_delivery_var,
            width=110,
            checkbox_width=16,
            checkbox_height=16,
            command=self._apply_table_filters,
        ).pack(side="left")
        self.only_favorite_var = ctk.BooleanVar()
        ctk.CTkCheckBox(
            filter_row,
            text="★ Избранное",
            variable=self.only_favorite_var,
            width=110,
            checkbox_width=16,
            checkbox_height=16,
            command=self._apply_table_filters,
        ).pack(side="left", padx=(4, 0))
        ctk.CTkLabel(filter_row, text="Выгода", font=ctk.CTkFont(size=11)).pack(side="left", padx=(8, 0))
        self.min_discount_var = ctk.StringVar(value="Любая")
        self.min_discount_combo = ctk.CTkComboBox(
            filter_row,
            values=["Любая", "5%", "10%", "20%"],
            variable=self.min_discount_var,
            command=lambda _v: self._apply_table_filters(),
            width=105,
        )
        self.min_discount_combo.pack(side="left", padx=(5, 0))
        self.min_discount_combo.bind(
            "<Return>", lambda _event: self._apply_table_filters()
        )
        self.min_discount_combo.bind(
            "<FocusOut>", lambda _event: self._apply_table_filters()
        )
        for entry in (self.price_from_entry, self.price_to_entry):
            entry.bind("<KeyRelease>", lambda _event: self._apply_table_filters())
            entry.bind("<FocusOut>", lambda _event: self._apply_table_filters())
        self.results_tabs = ctk.CTkTabview(table_frame, anchor="w")
        self.results_tabs.grid(row=4, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.details_panel = DetailsPanel(
            table_frame,
            width=320,
            on_open=lambda url: open_listing_url(url),
            on_copy=self._copy_listing_link,
            on_favorite=self._toggle_favorite,
            on_blacklist=self._blacklist_seller,
            on_close=self._hide_details,
        )
        self.details_panel.grid(
            row=0, column=1, rowspan=5, sticky="nsew", padx=(0, 8), pady=(10, 8)
        )
        self.details_panel.grid_remove()
        self.details_panel.clear()
        tab_results = self.results_tabs.add("Результаты")
        tab_market = self.results_tabs.add("Аналитика рынка")
        for tab in (tab_results, tab_market):
            tab.grid_rowconfigure(0, weight=1)
            tab.grid_columnconfigure(0, weight=1)
        self.table = ListingsTable(tab_results, on_select=self._show_listing_details)
        self.table.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)
        self.market_table = MarketTable(tab_market)
        self.market_table.grid(row=0, column=0, sticky="nsew", padx=6, pady=6)

        self._build_log_panel()
        self._build_settings()
        self._fix_settings_scroll()
        self._build_settings_window()

    def _fix_settings_scroll(self) -> None:
        self._solidify_settings_frames(self.settings)
        try:
            self.settings_canvas.configure(
                scrollregion=self.settings_canvas.bbox("all")
            )
        except Exception:
            pass

    def _on_settings_wheel(self, event: Any) -> str | None:
        widget = getattr(event, "widget", None)
        inside = False
        while widget is not None:
            if widget is self.settings_shell or widget is self.settings_canvas:
                inside = True
                break
            widget = getattr(widget, "master", None)
        if not inside:
            return None
        try:
            bbox = self.settings_canvas.bbox("all")
            if (
                bbox
                and bbox[3] - bbox[1]
                <= self.settings_canvas.winfo_height()
            ):
                return "break"
            delta = int(-event.delta / 120) or (-1 if event.delta > 0 else 1)
            self.settings_canvas.yview_scroll(delta, "units")
        except Exception:
            pass
        return "break"

    def _solidify_settings_frames(
        self, widget: Any, parent_background: Any = ("#f4f5f7", "#171818")
    ) -> None:
        """Заменяет transparent у фреймов на цвет родителя — без следов при скролле."""
        try:
            children = widget.winfo_children()
        except Exception:
            return
        for child in children:
            background = parent_background
            if isinstance(child, ctk.CTkFrame):
                try:
                    current = child.cget("fg_color")
                    if str(current) in {
                        "transparent",
                        "('transparent', 'transparent')",
                    }:
                        child.configure(fg_color=parent_background)
                    else:
                        background = current
                except Exception:
                    pass
            self._solidify_settings_frames(child, background)

    def _section_title(self, text: str, description: str = "") -> None:
        ctk.CTkLabel(
            self.settings,
            text=text,
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(14, 1))
        if description:
            ctk.CTkLabel(
                self.settings,
                text=description,
                font=ctk.CTkFont(size=11),
                text_color=("#666666", "#9d9d9d"),
                anchor="w",
                justify="left",
                wraplength=400,
            ).pack(fill="x", padx=12, pady=(0, 5))

    def _build_settings(self) -> None:
        # Поисковые запросы со своими фильтрами
        self._section_title(
            "Поисковые запросы",
            "Вставьте запрос или ссылку Avito. У каждого запроса свои фильтры; "
            "настройки сохраняются автоматически.",
        )
        query_add_frame = ctk.CTkFrame(self.settings, fg_color="transparent")
        query_add_frame.pack(fill="x", padx=12, pady=(2, 4))
        self.new_query_entry = ctk.CTkEntry(
            query_add_frame,
            placeholder_text="Запрос или ссылка Avito",
        )
        self.new_query_entry.pack(side="left", fill="x", expand=True)
        self.new_query_entry.bind(
            "<Return>", lambda _event: self._add_query_from_entry()
        )
        for sequence in ("<Control-v>", "<Control-V>", "<Shift-Insert>"):
            self.new_query_entry.bind(
                sequence, self._paste_new_query_event
            )
            inner = getattr(self.new_query_entry, "_entry", None)
            if inner is not None:
                inner.bind(sequence, self._paste_new_query_event)
        self.new_query_entry.bind("<Button-3>", self._show_query_menu)
        self._query_menu = tk.Menu(self, tearoff=0)
        self._query_menu.add_command(
            label="Вставить", command=self._paste_new_query
        )
        self._query_menu.add_command(
            label="Копировать", command=self._copy_new_query
        )
        self._query_menu.add_command(
            label="Выделить всё", command=self._select_all_new_query
        )
        ctk.CTkButton(
            query_add_frame,
            text="Вставить",
            width=78,
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
            command=self._paste_new_query,
        ).pack(side="left", padx=(6, 0))
        ctk.CTkButton(
            query_add_frame,
            text="Добавить",
            width=92,
            command=self._add_query_from_entry,
        ).pack(side="left", padx=(4, 0))
        self.query_paste_hint = ctk.CTkLabel(
            self.settings,
            text=(
                "Ссылку Avito вставьте в верхнее поле и нажмите «Добавить». "
                "Если Ctrl+V не работает — кнопка «Вставить».\n"
                "Проверка запроса: кнопка «Проверить» в заголовке карточки."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=400,
        )
        self.query_paste_hint.pack(fill="x", padx=12, pady=(0, 2))
        ctk.CTkLabel(
            self.settings,
            text=(
                "Пустые поля не ограничивают поиск: город — вся Россия, "
                "цена и слова — без фильтра."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", padx=12, pady=(0, 4))
        self.queries_frame = ctk.CTkFrame(self.settings, fg_color="transparent")
        self.queries_frame.pack(fill="x", padx=12, pady=(2, 4))

    def _build_settings_window(self) -> None:
        self.settings_toplevel = ctk.CTkToplevel(self)
        self.settings_toplevel.title("Настройки Avito Radar")
        self.settings_toplevel.geometry("560x760")
        self.settings_toplevel.minsize(500, 560)
        self.settings_toplevel.transient(self)
        self.settings_toplevel.withdraw()
        self.settings_toplevel.protocol(
            "WM_DELETE_WINDOW", self.settings_toplevel.withdraw
        )
        self.settings_window_content = ctk.CTkScrollableFrame(
            self.settings_toplevel,
            fg_color=("#f4f5f7", "#171818"),
            corner_radius=0,
        )
        self.settings_window_content.pack(fill="both", expand=True, padx=8, pady=8)
        old_settings = self.settings
        self.settings = self.settings_window_content
        # Режим
        self._section_title("Режим работы")
        self.mode_row = ctk.CTkFrame(self.settings, fg_color="transparent")
        self.mode_row.pack(fill="x", padx=12, pady=(2, 4))
        self.mode_var = ctk.StringVar(value=MODE_ONCE)
        self.mode_menu = ctk.CTkOptionMenu(
            self.mode_row,
            values=MODES,
            variable=self.mode_var,
            command=self._on_mode_change,
            width=190,
            dynamic_resizing=False,
        )
        self.mode_menu.pack(side="left", anchor="n")
        self.mode_hint = ctk.CTkLabel(
            self.mode_row,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=("#666666", "#9d9d9d"),
            anchor="w",
            justify="left",
            wraplength=190,
        )
        self.mode_hint.pack(side="left", fill="x", expand=True, padx=(10, 0))

        self.interval_frame = ctk.CTkFrame(self.settings, fg_color="transparent")
        self.interval_label = ctk.CTkLabel(
            self.interval_frame, text="Интервал, минут", width=140, anchor="w"
        )
        self.interval_label.pack(side="left")
        self.interval_entry = ctk.CTkEntry(self.interval_frame, width=100)
        self.interval_entry.pack(side="left")
        defaults = self.config.get("defaults", {})
        self.interval_entry.insert(
            0, str(defaults.get("interval_minutes", 30) or 30)
        )
        self._on_mode_change(MODE_ONCE)


        # Метод парсинга
        self._section_title("Метод парсинга")
        transport_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        transport_row.pack(fill="x", padx=12, pady=(2, 2))
        self.transport_labels = {
            "Авто: HTTP → браузер": "auto",
            "HTTP: быстро": "http",
            "Браузер: безопасно": "browser",
            "Агрессивный HTTP": "aggressive",
        }
        current_transport = str(
            self.config.get("scraper", {}).get("transport", "auto")
        )
        current_transport_label = next(
            (
                label
                for label, value in self.transport_labels.items()
                if value == current_transport
            ),
            "Авто: HTTP → браузер",
        )
        self.transport_var = ctk.StringVar(value=current_transport_label)
        self.transport_menu = ctk.CTkOptionMenu(
            transport_row,
            values=list(self.transport_labels),
            variable=self.transport_var,
            command=self._on_transport_change,
            width=230,
            dynamic_resizing=False,
        )
        self.transport_menu.pack(side="left")
        strategy_row = ctk.CTkFrame(
            self.settings_window_content, fg_color="transparent"
        )
        self.strategy_row = strategy_row
        strategy_row.pack(fill="x", padx=12, pady=(2, 2))
        ctk.CTkLabel(strategy_row, text="Стратегия капчи", width=130, anchor="w").pack(side="left")
        self.captcha_strategy_labels = {
            "Как сейчас": "legacy",
            "Ручная + авто": "manual_then_auto",
            "Только авто": "auto_only",
        }
        current_strategy = str(
            self.config.get("scraper", {}).get("captcha_strategy", "legacy")
        )
        self.strategy_var = ctk.StringVar(
            value=next(
                (label for label, value in self.captcha_strategy_labels.items()
                 if value == current_strategy),
                "Как сейчас",
            )
        )
        strategy_menu = ctk.CTkOptionMenu(
            strategy_row,
            values=list(self.captcha_strategy_labels),
            variable=self.strategy_var,
            width=230,
            command=lambda label: self._on_captcha_strategy_change(
                self.captcha_strategy_labels.get(label, "legacy")
            ),
        )
        strategy_menu.pack(side="left")
        self.strategy_hint = ctk.CTkLabel(
            self.settings_window_content,
            text="Старое поведение: HTTP, при неудаче — браузер. Без новых правил.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        )
        self.strategy_hint.pack(fill="x", padx=12, pady=(0, 3))
        self.transport_hint = ctk.CTkLabel(
            self.settings_window_content,
            text="",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        )
        self.transport_hint.pack(fill="x", padx=12, pady=(0, 3))
        self._update_strategy_visibility()
        self.auto_captcha_var = ctk.BooleanVar(
            value=bool(self.config.get("scraper", {}).get("auto_captcha", True))
        )
        self.auto_captcha_checkbox = ctk.CTkCheckBox(
            self.settings_window_content,
            text="Автоматически решать капчу",
            variable=self.auto_captcha_var,
            command=self._on_auto_captcha_change,
            font=ctk.CTkFont(size=11),
            checkbox_width=18,
            checkbox_height=18,
        )
        self.auto_captcha_checkbox.pack(anchor="w", padx=12, pady=(2, 4))
        self._refresh_transport_hint()

        # Точность дат
        self._section_title("Точность дат")
        date_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        date_row.pack(fill="x", padx=12, pady=(2, 2))
        ctk.CTkLabel(date_row, text="Карточек за цикл", width=130, anchor="w").pack(side="left")
        self.date_limit_var = ctk.StringVar(
            value=(
                "Все"
                if int(self.config.get("app", {}).get("date_enrich_limit", 5)) < 0
                else str(int(self.config.get("app", {}).get("date_enrich_limit", 5)))
            )
        )
        self.date_limit_combo = ctk.CTkComboBox(
            date_row,
            values=["0", "3", "5", "10", "20", "50", "Все"],
            variable=self.date_limit_var,
            command=lambda _v: self._on_date_limit_change(),
            width=90,
        )
        self.date_limit_combo.pack(side="left")
        ctk.CTkLabel(
            self.settings_window_content,
            text=(
                "Сколько карточек за цикл открывать для точной даты публикации. "
                "Меньше — быстрее и ниже риск блокировки; больше — точнее, но медленнее. "
                "0 — не открывать карточки, дата может быть неизвестна."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        ).pack(fill="x", padx=12, pady=(0, 4))

        # Уведомления
        self._section_title("Уведомления в Telegram")
        notify_cfg = self.config.get("notifications", {})
        self.notify_new_var = ctk.BooleanVar(value=bool(notify_cfg.get("new", True)))
        self.notify_price_var = ctk.BooleanVar(
            value=bool(notify_cfg.get("price", True))
        )
        self.notify_old_var = ctk.BooleanVar(
            value=bool(notify_cfg.get("old", False))
        )
        for text, variable, hint in (
            (
                "Новые",
                self.notify_new_var,
                "Присылать объявления, которых раньше не было.",
            ),
            (
                "Старые",
                self.notify_old_var,
                "Присылать объявления, опубликованные раньше заданного срока.",
            ),
            (
                "Изменение цены",
                self.notify_price_var,
                "Присылать, если продавец изменил цену.",
            ),
        ):
            item = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
            item.pack(fill="x", padx=12, pady=(2, 1))
            ctk.CTkCheckBox(
                item,
                text=text,
                variable=variable,
                checkbox_width=18,
                checkbox_height=18,
                font=ctk.CTkFont(size=12, weight="bold"),
            ).pack(anchor="w")
            ctk.CTkLabel(
                item,
                text=hint,
                font=ctk.CTkFont(size=10),
                text_color=("#777777", "#8f8f8f"),
                anchor="w",
                justify="left",
                wraplength=390,
            ).pack(fill="x", padx=(26, 0))
        notify_sort_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        notify_sort_row.pack(fill="x", padx=12, pady=(2, 2))
        ctk.CTkLabel(
            notify_sort_row, text="Сортировка по умолчанию", width=200, anchor="w"
        ).pack(side="left")
        sort_labels = {
            "Дата": "date",
            "Дешевле": "price_asc",
            "Дороже": "price_desc",
            "По размеру скидки": "market_diff",
        }
        reverse_sort = {value: label for label, value in sort_labels.items()}
        self.notify_sort_combo = ctk.CTkComboBox(
            notify_sort_row,
            values=list(sort_labels),
            width=230,
        )
        saved_sort = str(notify_cfg.get("sort", "date"))
        if saved_sort == "price":
            saved_sort = "price_asc"
        self.notify_sort_combo.set(reverse_sort.get(saved_sort, "Дата"))
        self.notify_sort_combo.pack(side="left")
        notify_words_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        notify_words_row.pack(fill="x", padx=12, pady=(2, 2))
        ctk.CTkLabel(
            notify_words_row, text="Ключевые слова", width=200, anchor="w"
        ).pack(side="left")
        self.notify_keywords_entry = ctk.CTkEntry(
            notify_words_row, placeholder_text="через запятую"
        )
        self.notify_keywords_entry.insert(
            0, str(notify_cfg.get("keywords", "") or "")
        )
        self.notify_keywords_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            self.settings_window_content,
            text="Сохранить уведомления",
            width=170,
            command=self._on_notifications_save,
        ).pack(anchor="w", padx=12, pady=(3, 2))
        ctk.CTkLabel(
            self.settings_window_content,
            text=(
                "Фильтры применяются к дайджесту: новые, изменения цены, "
                "максимальная цена и обязательные слова в заголовке."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=395,
        ).pack(fill="x", padx=12, pady=(0, 4))

        # Получатели
        recipients_header = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        recipients_header.pack(fill="x", padx=12, pady=(14, 2))
        ctk.CTkLabel(
            recipients_header,
            text="Получатели",
            font=ctk.CTkFont(size=14, weight="bold"),
        ).pack(side="left")
        ctk.CTkButton(
            recipients_header,
            text="Обновить список",
            width=130,
            height=28,
            command=self._manual_refresh_recipients,
        ).pack(side="right")
        ctk.CTkLabel(
            self.settings_window_content,
            text="Отправьте /start боту, затем отметьте @username. Уведомления "
            "получат только выбранные.",
            font=ctk.CTkFont(size=11),
            text_color=("#666666", "#9d9d9d"),
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", padx=12, pady=(0, 4))
        self.telegram_status_label = ctk.CTkLabel(
            self.settings_window_content,
            text="Telegram: не подключён",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=("#9a5b00", "#e2b45c"),
            anchor="w",
            justify="left",
            wraplength=400,
        )
        self.telegram_status_label.pack(fill="x", padx=12, pady=(0, 4))
        self.recipients_frame = ctk.CTkFrame(
            self.settings_window_content,
            fg_color=("#ffffff", "#1f2020"),
            corner_radius=8,
            height=110,
        )
        self.recipients_frame.pack(fill="x", padx=12, pady=(2, 6))

        # Telegram token
        self._section_title(
            "Токен бота (необязательно)",
            "Хранится только в локальном config.toml. Токен нужен для /start и уведомлений.",
        )
        token_frame = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        token_frame.pack(fill="x", padx=12, pady=(2, 4))
        self.token_entry = ctk.CTkEntry(
            token_frame, placeholder_text="Вставьте токен от @BotFather", show="•"
        )
        self.token_entry.pack(side="left", fill="x", expand=True)
        self.token_entry.bind("<Control-v>", self._paste_token_event)
        self.token_entry.bind("<Control-V>", self._paste_token_event)
        self.token_entry.bind("<Shift-Insert>", self._paste_token_event)
        self.token_visible = False
        self.token_toggle_button = ctk.CTkButton(
            token_frame,
            text="Показать",
            width=74,
            command=self._toggle_token_visibility,
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
        )
        self.token_toggle_button.pack(side="left", padx=(5, 0))
        ctk.CTkButton(
            token_frame,
            text="Вставить",
            width=74,
            command=self._paste_token,
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
        ).pack(side="left", padx=(5, 0))
        api_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        api_row.pack(fill="x", padx=12, pady=(3, 0))
        ctk.CTkLabel(api_row, text="API Telegram", width=100, anchor="w").pack(side="left")
        self.api_base_entry = ctk.CTkEntry(
            api_row, placeholder_text="https://api.telegram.org"
        )
        self.api_base_entry.insert(
            0,
            str(
                self.config.get("telegram", {}).get(
                    "api_base", "https://api.telegram.org"
                )
            ),
        )
        self.api_base_entry.pack(side="left", fill="x", expand=True)
        proxy_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        proxy_row.pack(fill="x", padx=12, pady=(3, 0))
        ctk.CTkLabel(proxy_row, text="Прокси", width=100, anchor="w").pack(side="left")
        self.proxy_entry = ctk.CTkEntry(
            proxy_row, placeholder_text="http://... или socks5://..."
        )
        self.proxy_entry.insert(
            0, str(self.config.get("telegram", {}).get("proxy_url", ""))
        )
        self.proxy_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkLabel(
            self.settings_window_content,
            text=(
                "API Telegram: для Cloudflare Worker укажите базовый URL до /bot. "
                "Прокси — необязательный HTTP/SOCKS-адрес для запросов к Telegram."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", padx=12, pady=(2, 4))
        token_bottom = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        token_bottom.pack(fill="x", padx=12, pady=(0, 4))
        ctk.CTkButton(
            token_bottom,
            text="Сохранить Telegram",
            width=150,
            command=self._save_token,
        ).pack(side="left")
        ctk.CTkButton(
            token_bottom,
            text="Открыть config.toml",
            width=140,
            command=self._open_config,
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
        ).pack(side="left", padx=(6, 0))
        ctk.CTkLabel(
            self.settings_window_content,
            text="Если вставка не работает, впишите токен в config.toml и перезапустите приложение.",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            wraplength=400,
            justify="left",
            anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 4))
        token = get_telegram_token(self.config)
        if token:
            self.token_entry.insert(0, token)

        self.settings = old_settings
        ctk.CTkLabel(
            self.settings_window_content,
            text="Открытие объявлений",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).pack(fill="x", padx=12, pady=(14, 2))
        browser_row = ctk.CTkFrame(self.settings_window_content, fg_color="transparent")
        browser_row.pack(fill="x", padx=12, pady=(2, 4))
        ctk.CTkLabel(browser_row, text="Браузер", width=100, anchor="w").pack(side="left")
        self.browser_labels = {
            "Автоопределение": "auto",
            "Edge": "edge",
            "Chrome": "chrome",
            "Firefox": "firefox",
        }
        current_browser = str(self.config.get("app", {}).get("browser", "auto"))
        self.browser_var = ctk.StringVar(
            value=next(
                (label for label, value in self.browser_labels.items() if value == current_browser),
                "Автоопределение",
            )
        )
        self.browser_menu = ctk.CTkOptionMenu(
            browser_row,
            values=list(self.browser_labels),
            variable=self.browser_var,
            command=self._on_browser_change,
            width=190,
            dynamic_resizing=False,
        )
        self.browser_menu.pack(side="left")
        ctk.CTkButton(
            self.settings_window_content,
            text="Чёрный список продавцов",
            width=220,
            command=self._show_blacklist,
        ).pack(anchor="w", padx=12, pady=(12, 18))
        self._solidify_settings_frames(self.settings_window_content)

    def _open_settings_window(self) -> None:
        if not hasattr(self, "settings_toplevel"):
            return
        self.settings_toplevel.deiconify()
        self.settings_toplevel.lift()
        self.settings_toplevel.focus_force()
        self.after(20, lambda: apply_light_titlebar(self.settings_toplevel))

    def _windows_set_titlebar_color(self, _color_mode: str) -> None:
        """Не даём CustomTkinter снова включать тёмный заголовок."""
        self.after(10, lambda: apply_light_titlebar(self))

    def _build_action_bar(self, parent: Any) -> None:
        bar = ctk.CTkFrame(
            parent,
            fg_color=("#f4f5f7", "#171818"),
            corner_radius=12,
        )
        bar.grid(row=1, column=0, sticky="ew", pady=(7, 0))
        buttons = ctk.CTkFrame(bar, fg_color="transparent")
        buttons.pack(fill="x", padx=12, pady=10)
        self.start_button = ctk.CTkButton(
            buttons,
            text="Старт",
            height=42,
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._on_start,
        )
        self.start_button.pack(side="left", fill="x", expand=True)
        self.settings_button = ctk.CTkButton(
            buttons,
            text="Настройки",
            width=105,
            height=42,
            fg_color=("#e3e9f2", "#263442"),
            hover_color=("#d2dce9", "#31465a"),
            text_color=("#1e4d78", "#b9d8f5"),
            command=self._open_settings_window,
        )
        self.settings_button.pack(side="right", padx=(6, 0))
        self.log_show_button = ctk.CTkButton(
            buttons,
            text="Журнал",
            width=86,
            height=42,
            fg_color=("#e3e9f2", "#263442"),
            hover_color=("#d2dce9", "#31465a"),
            text_color=("#1e4d78", "#b9d8f5"),
            command=self._show_log_panel,
        )
        self.stop_button = ctk.CTkButton(
            buttons,
            text="Стоп",
            height=42,
            width=90,
            font=ctk.CTkFont(size=15, weight="bold"),
            fg_color=("#b33a3a", "#8f2f2f"),
            hover_color=("#9d3030", "#a93a3a"),
            command=self._on_stop,
            state="disabled",
        )
        self.browser_button = ctk.CTkButton(
            buttons,
            text="Показать браузер",
            height=42,
            width=150,
            font=ctk.CTkFont(size=13),
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
            command=self._toggle_browser,
            state="disabled",
        )
        if not self.browser_toggle_available:
            self.browser_button.configure(text="Окно недоступно", state="disabled")

    def _build_log_panel(self) -> None:
        panel = ctk.CTkFrame(self, corner_radius=12)
        self.log_panel = panel
        panel.grid(row=2, column=0, columnspan=2, sticky="ew", padx=14, pady=(6, 14))
        panel.grid_columnconfigure(0, weight=1)
        bar = ctk.CTkFrame(panel, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(8, 4))
        ctk.CTkLabel(
            bar, text="Журнал", font=ctk.CTkFont(size=14, weight="bold")
        ).pack(side="left")
        self.stats_label = ctk.CTkLabel(
            bar,
            text="",
            text_color=("#666666", "#a6a6a6"),
            font=ctk.CTkFont(size=11),
        )
        self.stats_label.pack(side="left", padx=(14, 0))
        ctk.CTkButton(
            bar, text="Экспорт XLSX", width=120, height=28, command=self._export_manual
        ).pack(side="right", padx=(6, 0))
        self.log_toggle = ctk.CTkButton(
            bar,
            text="Свернуть журнал ▾",
            width=145,
            height=28,
            fg_color=("#e3e9f2", "#263442"),
            hover_color=("#d2dce9", "#31465a"),
            text_color=("#1e4d78", "#b9d8f5"),
            command=self._toggle_log_panel,
        )
        self.log_toggle.pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            bar,
            text="Скрыть",
            width=80,
            height=28,
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
            command=self._hide_log_panel,
        ).pack(side="right", padx=(6, 0))
        ctk.CTkButton(
            bar,
            text="Открыть лог",
            width=115,
            height=28,
            command=self._open_log,
            fg_color=("#d7d9dc", "#2c2e2f"),
            hover_color=("#c5c8cc", "#3a3d3f"),
            text_color=("#222222", "#eeeeee"),
        ).pack(side="right")
        self.log_text = ctk.CTkTextbox(
            panel, height=170, font=ctk.CTkFont(family="Consolas", size=11)
        )
        self.log_text.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 10))
        self.log_text.configure(state="disabled")
        self._log_collapsed = False
        self._append_log("Avito Radar готов. Добавьте запросы и нажмите «Старт».", logging.INFO)
        self._toggle_log_panel()

    def _toggle_log_panel(self) -> None:
        if not hasattr(self, "log_text"):
            return
        self._log_collapsed = not bool(getattr(self, "_log_collapsed", False))
        if self._log_collapsed:
            self.log_text.grid_remove()
            self.log_toggle.configure(text="Развернуть журнал ▴")
        else:
            self.log_text.grid()
            self.log_toggle.configure(text="Свернуть журнал ▾")

    def _hide_log_panel(self) -> None:
        if not hasattr(self, "log_panel"):
            return
        self.log_panel.grid_remove()
        if not self.log_show_button.winfo_manager():
            self.log_show_button.pack(side="right", padx=(6, 0))

    def _show_log_panel(self) -> None:
        if not hasattr(self, "log_panel"):
            return
        self.log_panel.grid()
        if self.log_show_button.winfo_manager():
            self.log_show_button.pack_forget()

    # ---------------------------- query cards ----------------------------

    def _load_query_cards(self) -> None:
        self._empty_queries_label = None
        for row in self.storage.list_saved_queries():
            if not str(row.get("query") or "").strip():
                continue
            try:
                self._create_query_card(QueryConfig.from_row(row))
            except Exception:
                logger.exception("Не удалось восстановить сохранённый запрос")
        self._update_queries_empty_hint()
        self._solidify_settings_frames(self.settings)

    def _reload_query_cards(self) -> None:
        """Перечитывает карточки из базы без перезапуска приложения."""
        self._persist_queries()
        for card in list(self.query_cards):
            card.destroy()
        self.query_cards = []
        self._empty_queries_label = None
        self._load_query_cards()
        self._update_queries_empty_hint()
        self._refresh_query_options()

    def _update_queries_empty_hint(self) -> None:
        if self.query_cards:
            if self._empty_queries_label is not None:
                self._empty_queries_label.destroy()
                self._empty_queries_label = None
            return
        if self._empty_queries_label is None:
            self._empty_queries_label = ctk.CTkLabel(
                self.queries_frame,
                text="Запросы пока не добавлены. Добавьте запрос — у него будут свои фильтры.",
                text_color=("#777777", "#8f8f8f"),
                anchor="w",
                justify="left",
                wraplength=390,
            )
            self._empty_queries_label.pack(fill="x", padx=6, pady=10)

    def _create_query_card(self, query_config: QueryConfig) -> QueryCard:
        card = QueryCard(
            self.queries_frame,
            self,
            query_config,
            len(self.query_cards) + 1,
        )
        card.pack(fill="x", pady=4)
        self.query_cards.append(card)
        self._update_queries_empty_hint()
        self._solidify_settings_frames(self.settings)
        self._refresh_query_options()
        return card

    def _refresh_query_options(self) -> None:
        if not hasattr(self, "query_select_menu"):
            return
        entries: list[tuple[str, str, str]] = []
        for card in getattr(self, "query_cards", []):
            try:
                config = card.get_config(validate=False)
            except Exception:
                continue
            base = config.query or "Новый запрос"
            extra = config.city or ("по ссылке Avito" if config.source_url else "")
            entries.append((base, config.query_key(), extra))
        counts: dict[str, int] = {}
        for base, _key, _extra in entries:
            counts[base] = counts.get(base, 0) + 1
        options: dict[str, str] = {}
        for base, key, extra in entries:
            label = f"{base} · {extra}" if counts.get(base, 0) > 1 and extra else base
            options[label] = key
        values = list(options) or ["Все запросы"]
        active_keys: set[str] = set()
        for card in getattr(self, "query_cards", []):
            if not card.is_active_var.get():
                continue
            try:
                active_keys.add(card.get_config(validate=False).query_key())
            except Exception:
                pass
        active_labels = [
            label for label, key in options.items() if key in active_keys
        ]
        current = self.query_select_var.get()
        if len(active_labels) == 1:
            current = active_labels[0]
        elif current not in options:
            current = active_labels[0] if active_labels else values[0]
        self._query_labels = options
        self.query_select_menu.configure(values=values)
        self.query_select_menu.set(current)
        self.query_select_var.set(current)
        if hasattr(self, "table"):
            self.table.set_query_key(options.get(current, ""))
            self._update_table_title()
            if hasattr(self, "market_table"):
                self.market_table.set_query_key(options.get(current, ""))

    def _update_table_title(self) -> None:
        if not hasattr(self, "table"):
            return
        label = self.query_select_var.get().strip()
        label = re.sub(r"^\d+\.\s*", "", label)
        self.table.set_title(
            "Результаты" if not label or label == "Все запросы" else f"Результаты: {label}"
        )

    def _apply_query_filter(self) -> None:
        if not hasattr(self, "table"):
            return
        label = self.query_select_var.get()
        key = getattr(self, "_query_labels", {}).get(label, "")
        self.table.set_query_key(key)
        if hasattr(self, "market_table"):
            self.market_table.set_query_key(key)
        self._update_table_title()
        self._hide_details()

    def _toggle_advanced_filters(self) -> None:
        if not hasattr(self, "advanced_filters"):
            return
        visible = not bool(getattr(self, "_advanced_filters_visible", False))
        self._advanced_filters_visible = visible
        if visible:
            self.advanced_filters.grid()
            self.filters_toggle.configure(text="Фильтры ▴")
        else:
            self.advanced_filters.grid_remove()
            self.filters_toggle.configure(text="Фильтры ▾")

    def _global_paste_event(self, event: Any = None) -> str | None:
        if event is None:
            return None
        # keycode 86 — физическая клавиша V независимо от раскладки.
        keycode = int(getattr(event, "keycode", 0) or 0)
        keysym = str(getattr(event, "keysym", "") or "").casefold()
        if keycode == 86 or keysym in {"v", "м"}:
            return self._global_paste(event)
        return None

    def _global_paste(self, _event: Any = None) -> str | None:
        try:
            focused = self.focus_get()
        except Exception:
            return None
        candidates: list[Any] = [
            self.new_query_entry,
            getattr(self, "token_entry", None),
            getattr(self, "api_base_entry", None),
            getattr(self, "proxy_entry", None),
            getattr(self, "notify_max_price_entry", None),
            getattr(self, "notify_keywords_entry", None),
        ]
        for card in getattr(self, "query_cards", []):
            candidates.append(getattr(card, "query_entry", None))
        for entry in candidates:
            if entry is None:
                continue
            if focused is entry or focused is getattr(entry, "_entry", None):
                try:
                    text = str(self.clipboard_get() or "").strip()
                except Exception:
                    return "break"
                if text:
                    entry.insert("insert", text)
                    entry.icursor("end")
                return "break"
        return None

    def _paste_new_query(self) -> None:
        try:
            value = self.clipboard_get()
        except Exception:
            messagebox.showwarning(
                "Avito Radar",
                "Не удалось прочитать буфер обмена. Скопируйте ссылку ещё раз "
                "или вставьте её через Ctrl+V.",
            )
            return
        text = str(value or "").strip()
        if not text:
            messagebox.showwarning("Avito Radar", "Буфер обмена пуст.")
            return
        self.new_query_entry.focus_set()
        self.new_query_entry.insert("insert", text)
        self.new_query_entry.icursor("end")

    def _paste_new_query_event(self, _event: Any = None) -> str:
        self._paste_new_query()
        return "break"

    def _copy_new_query(self) -> None:
        value = self.new_query_entry.get()
        if not value:
            return
        self.clipboard_clear()
        self.clipboard_append(value)

    def _select_all_new_query(self) -> None:
        self.new_query_entry.focus_set()
        self.new_query_entry.select_range(0, "end")
        self.new_query_entry.icursor("end")

    def _show_query_menu(self, event: Any) -> None:
        try:
            self._query_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._query_menu.grab_release()

    def _add_query_from_entry(self) -> None:
        value = self.new_query_entry.get().strip()
        if not value:
            messagebox.showwarning(
                "Avito Radar", "Введите поисковый запрос или ссылку Avito."
            )
            return
        default_limit = int(self.config.get("app", {}).get("max_listings", 50))
        try:
            if is_avito_search_url(value):
                parsed = parse_avito_search_url(value)
                row = self.storage.save_saved_query(
                    None,
                    query=parsed["query"],
                    city=parsed["city"],
                    local_priority=parsed["local_priority"],
                    max_listings=default_limit,
                    price_min=parsed["price_min"],
                    price_max=parsed["price_max"],
                    source_url=parsed["source_url"],
                    avito_sort=parsed.get("avito_sort", 0),
                )
                logger.info(
                    "Добавлен запрос из ссылки Avito: «%s», город «%s»",
                    parsed["query"][:80],
                    parsed["city"] or "не определён",
                )
            else:
                row = self.storage.save_saved_query(
                    None, query=value, max_listings=default_limit
                )
                logger.info("Добавлен запрос с отдельными фильтрами: %s", value[:80])
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось добавить запрос:\n{exc}"
            )
            return
        self.new_query_entry.delete(0, "end")
        self._create_query_card(QueryConfig.from_row(row))

    def _delete_query_card(self, card: QueryCard) -> None:
        title = card.query_var.get().strip() or "этот запрос"
        if not messagebox.askyesno(
            "Avito Radar", f"Удалить запрос «{title}» и его фильтры?"
        ):
            return
        if card.db_id is not None:
            try:
                self.storage.delete_saved_query(card.db_id)
            except Exception as exc:
                messagebox.showerror("Avito Radar", f"Не удалось удалить запрос:\n{exc}")
                return
        self.query_cards = [item for item in self.query_cards if item is not card]
        card.destroy()
        for index, item in enumerate(self.query_cards, start=1):
            item.set_index(index)
        self._update_queries_empty_hint()
        self._refresh_query_options()
        logger.info("Удалён запрос: %s", title[:80])

    def _stop_telegram_query(self, card: QueryCard) -> None:
        if card.db_id is None:
            return
        jobs = [
            job for job in self.storage.list_bot_jobs()
            if int(job.get("query_id") or 0) == int(card.db_id)
        ]
        for job in jobs:
            self.storage.update_bot_job(int(job["id"]), status="stopped")
        if getattr(self, "bot_manager", None):
            self.bot_manager.wake()
            if (
                self.bot_manager.current_job_id
                and any(int(job["id"]) == self.bot_manager.current_job_id for job in jobs)
                and self.bot_manager.current_worker
            ):
                self.bot_manager.current_worker.request_stop()
        self.storage.set_collector_state({"status": "idle"})
        self._last_worker_status = "Остановлено"
        self.status_label.configure(
            text="Остановлено",
            fg_color=("#e7e7e7", "#2d2f31"),
            text_color=("#333333", "#e0e0e0"),
        )
        self._reload_query_cards()
        logger.info("Остановлен Telegram-запрос: %s", card.query_var.get()[:80])

    def _schedule_queries_save(self) -> None:
        if self._closing:
            return
        if self._query_save_after_id is not None:
            try:
                self.after_cancel(self._query_save_after_id)
            except Exception:
                pass
        self._query_save_after_id = self.after(700, self._persist_queries)

    def _persist_queries(self, *, show_errors: bool = False) -> None:
        pending = self._query_save_after_id
        self._query_save_after_id = None
        if pending is not None:
            try:
                self.after_cancel(pending)
            except Exception:
                pass
        for card in list(self.query_cards):
            try:
                config = card.get_config(validate=False)
            except FilterError as exc:
                if show_errors:
                    messagebox.showerror("Avito Radar", str(exc))
                logger.debug("Черновик запроса не сохранён: %s", exc)
                continue
            try:
                row = self.storage.save_saved_query(
                    config.id,
                    query=config.query,
                    city=config.city,
                    local_priority=config.local_priority,
                    max_listings=config.max_listings,
                    pages=config.pages,
                    avito_sort=config.avito_sort,
                    model_mode=config.model_mode,
                    smart_filter=config.smart_filter,
                    category=config.category,
                    memory=config.memory,
                    color=config.color,
                    sim=config.sim,
                    condition=config.condition,
                    is_active=config.is_active,
                    notify_new=config.notify_new,
                    notify_price=config.notify_price,
                    notify_old=config.notify_old,
                    price_min=config.price_min,
                    price_max=config.price_max,
                    delivery=config.delivery,
                    seller_type=config.seller_type,
                    include_words=config.include_words,
                    exclude_words=config.exclude_words,
                    source_url=config.source_url,
                )
                card.mark_saved(row)
            except Exception as exc:
                logger.exception("Ошибка сохранения запроса")
                if show_errors:
                    messagebox.showerror(
                        "Avito Radar", f"Не удалось сохранить запрос:\n{exc}"
                    )
        self._refresh_query_options()

    def _collect_query_configs(self) -> list[QueryConfig]:
        if not self.query_cards:
            raise FilterError("Добавьте хотя бы один поисковый запрос.")
        self._persist_queries()
        configs: list[QueryConfig] = []
        for card in self.query_cards:
            if getattr(card, "origin", "app") == "telegram":
                continue
            config = card.get_config(validate=True)
            configs.append(config)
        if not configs:
            raise FilterError(
                "Нет обычных запросов для ручного запуска. "
                "Запросы из Telegram запускаются командой в боте."
            )
        return configs

    # ---------------------------- recipients ----------------------------

    def _update_telegram_status(self, text: str, ok: bool | None = None) -> None:
        if not hasattr(self, "telegram_status_label"):
            return
        if ok is True:
            color = ("#1f6b38", "#8fe0a8")
        elif ok is False:
            color = ("#a32b2b", "#ff9e9e")
        else:
            color = ("#8a5b00", "#e2b45c")
        self.telegram_status_label.configure(text=text, text_color=color)

    def _manual_refresh_recipients(self) -> None:
        if not get_telegram_token(self.config):
            self._update_telegram_status(
                "Telegram: токен не задан. Сохраните токен — без него /start не принимается.",
                ok=False,
            )
            messagebox.showwarning(
                "Avito Radar",
                "Сначала сохраните токен бота.\n\n"
                "Без токена слушатель Telegram не запущен, поэтому сообщения /start "
                "не попадают в список получателей.",
            )
            return
        if not self.bot_listener or not self.bot_listener.is_alive():
            self._restart_bot_listener()
        self._refresh_recipients()
        count = len(self.storage.get_subscribers())
        logger.info("Обновление получателей: найдено %s", count)
        if count == 0:
            messagebox.showinfo(
                "Avito Radar",
                "Подписчиков пока нет.\n\n"
                "Отправьте вашему боту /start в Telegram, подождите пару секунд "
                "и нажмите «Обновить список» ещё раз.",
            )

    def _refresh_recipients(self) -> None:
        checked = {
            chat_id for chat_id, variable in self.recipient_vars.items() if variable.get()
        }
        for child in self.recipients_frame.winfo_children():
            child.destroy()
        self.recipient_vars = {}
        token = get_telegram_token(self.config)
        if not token:
            self._update_telegram_status("Telegram: токен не задан", ok=False)
        elif not self.bot_listener or not self.bot_listener.is_alive():
            self._update_telegram_status("Telegram: слушатель не запущен", ok=False)
        try:
            subscribers = self.storage.get_subscribers()
        except Exception as exc:
            logger.error("Не удалось прочитать подписчиков: %s", exc)
            return
        if not subscribers:
            ctk.CTkLabel(
                self.recipients_frame,
                text=(
                    "Пока никто не отправил /start боту."
                    if token
                    else "Сначала сохраните токен бота."
                ),
                text_color=("#777777", "#8f8f8f"),
                anchor="w",
            ).pack(fill="x", padx=10, pady=12)
            self._recipients_loaded = True
            return
        for subscriber in subscribers:
            chat_id = int(subscriber["chat_id"])
            label = Storage.subscriber_label(subscriber)
            if subscriber.get("first_name") and not subscriber.get("username"):
                label = f"{label} · {subscriber['first_name']}"
            variable = ctk.BooleanVar(
                value=(chat_id in checked)
                if self._recipients_loaded
                else True
            )
            checkbox = ctk.CTkCheckBox(
                self.recipients_frame,
                text=label,
                variable=variable,
                font=ctk.CTkFont(size=12),
            )
            checkbox.pack(anchor="w", padx=10, pady=4)
            self.recipient_vars[chat_id] = variable
            access_var = ctk.BooleanVar(
                value=bool(subscriber.get("can_create"))
                or str(subscriber.get("role") or "") == "owner"
            )
            access = ctk.CTkCheckBox(
                self.recipients_frame,
                text="Может создавать запросы",
                variable=access_var,
                font=ctk.CTkFont(size=10),
                state="disabled" if str(subscriber.get("role") or "") == "owner" else "normal",
                command=lambda cid=chat_id, var=access_var: (
                    self.storage.set_can_create(cid, bool(var.get())),
                    self._refresh_recipients(),
                ),
            )
            access.pack(anchor="w", padx=32, pady=(0, 3))
        owner = self.storage.get_bot_owner()
        owner_values = {
            Storage.subscriber_label(sub): int(sub["chat_id"])
            for sub in subscribers
        }
        owner_row = ctk.CTkFrame(self.recipients_frame, fg_color="transparent")
        owner_row.pack(fill="x", padx=10, pady=(4, 8))
        ctk.CTkLabel(owner_row, text="Владелец бота", width=110, anchor="w").pack(side="left")
        owner_var = ctk.StringVar(
            value=Storage.subscriber_label(owner) if owner else "Не назначен"
        )
        owner_menu = ctk.CTkOptionMenu(
            owner_row,
            values=["Не назначен", *owner_values],
            variable=owner_var,
            width=190,
            command=lambda value: (
                self.storage.set_bot_owner(owner_values[value])
                if value in owner_values
                else None,
                self._refresh_recipients(),
            ),
        )
        owner_menu.pack(side="left", padx=(6, 0))
        self._recipients_loaded = True

    def _selected_recipients(self) -> list[int]:
        return [
            chat_id for chat_id, variable in self.recipient_vars.items() if variable.get()
        ]

    # ------------------------------ modes ------------------------------

    def _refresh_transport_hint(self) -> None:
        if not hasattr(self, "transport_hint"):
            return
        transport = self.transport_labels.get(self.transport_var.get(), "auto")
        auto = bool(self.auto_captcha_var.get())
        if transport == "auto":
            if auto:
                text = (
                    "HTTP с автоматическим решением капчи; при неудаче — "
                    "Chromium для ручной проверки. Рекомендуется."
                )
            else:
                text = (
                    "HTTP без автоматического решения капчи; при неудаче — "
                    "Chromium для ручной проверки."
                )
        elif transport == "http":
            text = (
                "Только HTTP, без окна браузера. "
                + ("Капча решается автоматически." if auto else "При капче будет ошибка.")
            )
        elif transport == "browser":
            text = "Всегда Chromium и ручная проверка. Авто-капча не используется."
        else:
            text = (
                "HTTP без отката на браузер. "
                + ("Авто-капча включена." if auto else "Авто-капча выключена.")
            )
        self.transport_hint.configure(text=text)

    def _update_strategy_visibility(self) -> None:
        if not hasattr(self, "strategy_row"):
            return
        auto = self.transport_labels.get(self.transport_var.get(), "auto") == "auto"
        if auto:
            if not self.strategy_row.winfo_manager():
                self.strategy_row.pack(
                    fill="x", padx=12, pady=(2, 2), before=self.transport_hint
                )
            if not self.strategy_hint.winfo_manager():
                self.strategy_hint.pack(
                    fill="x", padx=12, pady=(0, 3), before=self.transport_hint
                )
        else:
            self.strategy_row.pack_forget()
            self.strategy_hint.pack_forget()

    def _on_captcha_strategy_change(self, value: str) -> None:
        try:
            save_scraper_setting(self.config_path, "captcha_strategy", value)
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Не удалось сохранить стратегию:\n{exc}")
            return
        self.config.setdefault("scraper", {})["captcha_strategy"] = value
        descriptions = {
            "legacy": "Старое поведение: HTTP, при неудаче — браузер. Без новых правил.",
            "manual_then_auto": "Сначала ручная проверка в браузере, затем автоматические попытки.",
            "auto_only": "Только автоматические попытки без открытия браузера.",
        }
        if hasattr(self, "strategy_hint"):
            self.strategy_hint.configure(text=descriptions.get(value, ""))
        self.status_label.configure(text="Стратегия капчи сохранена")

    def _on_transport_change(self, label: str) -> None:
        value = self.transport_labels.get(label, "auto")
        try:
            save_scraper_setting(self.config_path, "transport", value)
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось сохранить метод парсинга:\n{exc}"
            )
            return
        self.config.setdefault("scraper", {})["transport"] = value
        self._refresh_transport_hint()
        self._update_strategy_visibility()
        logger.info("Метод парсинга изменён на %s", value)
        self.status_label.configure(text=f"Метод парсинга: {label} (со следующего запуска)")

    def _on_browser_change(self, label: str) -> None:
        value = self.browser_labels.get(label, "auto")
        try:
            save_config_scalar(self.config_path, "app", "browser", value)
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось сохранить браузер:\n{exc}"
            )
            return
        self.config.setdefault("app", {})["browser"] = value
        set_preferred_browser(value)
        logger.info("Браузер для объявлений: %s", value)
        self.status_label.configure(text=f"Браузер: {label}")

    def _on_date_limit_change(self) -> None:
        try:
            raw_value = self.date_limit_var.get().strip()
            value = -1 if raw_value.lower() in {"все", "all"} else max(
                0, min(100, int(raw_value))
            )
        except (TypeError, ValueError):
            value = 5
        try:
            save_config_scalar(self.config_path, "app", "date_enrich_limit", value)
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось сохранить лимит дат:\n{exc}"
            )
            return
        self.config.setdefault("app", {})["date_enrich_limit"] = value
        label = "все" if value < 0 else str(value)
        logger.info("Лимит карточек для точной даты: %s", label)
        self.status_label.configure(
            text=f"Точных дат за цикл: {label} (со следующего запуска)"
        )

    def _on_notifications_save(self) -> None:
        keywords = self.notify_keywords_entry.get().strip()
        values = {
            "new": bool(self.notify_new_var.get()),
            "price": bool(self.notify_price_var.get()),
            "old": bool(self.notify_old_var.get()),
            "sort": {
                "Дешевле": "price_asc",
                "Дороже": "price_desc",
                "По размеру скидки": "market_diff",
            }.get(self.notify_sort_combo.get(), "date"),
            "keywords": keywords,
        }
        try:
            for key, value in values.items():
                save_config_scalar(self.config_path, "notifications", key, value)
            self.config = load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось сохранить уведомления:\n{exc}"
            )
            return
        logger.info("Настройки уведомлений сохранены")
        self.status_label.configure(
            text="Уведомления сохранены (со следующего запуска)"
        )

    def _on_auto_captcha_change(self) -> None:
        value = bool(self.auto_captcha_var.get())
        try:
            save_scraper_setting(self.config_path, "auto_captcha", value)
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось сохранить настройку капчи:\n{exc}"
            )
            return
        self.config.setdefault("scraper", {})["auto_captcha"] = value
        self._refresh_transport_hint()
        logger.info("Авто-капча: %s", "включена" if value else "выключена")

    def _on_mode_change(self, mode: str | None = None) -> None:
        mode = mode or self.mode_var.get()
        if self.mode_var.get() != mode:
            self.mode_var.set(mode)

        if mode == MODE_DURATION:
            hint = (
                "Циклы подряд в течение N минут, затем автоматический выход. "
                "N задаётся полем ниже."
            )
            self.interval_label.configure(text="Длительность, минут")
            show_interval = True
        elif mode == MODE_EVERY_N:
            hint = "Один цикл, затем перерыв N минут. Повторять до ручной остановки."
            self.interval_label.configure(text="Интервал, минут")
            show_interval = True
        elif mode == MODE_UNTIL_STOP:
            hint = "Один цикл, затем перерыв N минут. Работает до ручной остановки."
            self.interval_label.configure(text="Интервал, минут")
            show_interval = True
        else:
            hint = "Один цикл сбора и проверки, затем остановка."
            show_interval = False

        if show_interval:
            self.interval_entry.configure(state="normal")
            self.interval_frame.pack(
                fill="x", padx=12, pady=4, after=self.mode_row
            )
        else:
            self.interval_frame.pack_forget()
        self.mode_hint.configure(text=hint)

    def _read_interval(self) -> int:
        mode = self.mode_var.get()
        if mode == MODE_ONCE:
            return 0
        value = self.interval_entry.get().strip()
        if not re.fullmatch(r"\d{1,4}", value):
            raise ValueError("Интервал должен быть целым числом минут от 1 до 1440.")
        minutes = int(value)
        if not 1 <= minutes <= 1440:
            raise ValueError("Интервал должен быть от 1 до 1440 минут.")
        return minutes

    # ------------------------------ start/stop ------------------------------

    def _apply_table_filters(self) -> None:
        def parse_price(value: str) -> int | None:
            text = str(value or "").strip().replace(" ", "").replace("\u00a0", "")
            if not text:
                return None
            try:
                return max(0, int(text))
            except ValueError:
                return None

        discount_text = self.min_discount_var.get().replace("%", "").strip()
        try:
            discount = float(discount_text) / 100 if discount_text and discount_text != "Любая" else 0.0
        except ValueError:
            discount = 0.0
        self.table.set_view_filters(
            price_min=parse_price(self.price_from_var.get()),
            price_max=parse_price(self.price_to_var.get()),
            only_delivery=bool(self.only_delivery_var.get()),
            min_discount=discount,
            only_favorite=bool(self.only_favorite_var.get()),
        )

    def _hide_details(self) -> None:
        if not hasattr(self, "details_panel"):
            return
        if getattr(self, "_hiding_details", False):
            return
        self._hiding_details = True
        try:
            self.details_panel.clear()
            self.details_panel.grid_remove()
            if hasattr(self, "table"):
                self.table.tree.selection_set()
        finally:
            self._hiding_details = False

    def _copy_listing_link(self, url: str) -> None:
        try:
            self.clipboard_clear()
            self.clipboard_append(str(url))
            self.update_idletasks()
            self.status_label.configure(text="Ссылка скопирована")
            if self._status_restore_after_id is not None:
                self.after_cancel(self._status_restore_after_id)
            self._status_restore_after_id = self.after(
                2500, self._restore_worker_status
            )
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Не удалось скопировать ссылку:\n{exc}")

    def _restore_worker_status(self) -> None:
        self._status_restore_after_id = None
        if str(self.status_label.cget("text")) == "Ссылка скопирована":
            self.status_label.configure(text=self._last_worker_status)

    def _show_listing_details(self, listing: dict[str, Any] | None) -> None:
        if not hasattr(self, "details_panel"):
            return
        if not listing:
            self._hide_details()
            return
        self.details_panel.grid()
        listing_id = str(listing.get("id") or "")
        try:
            history = self.storage.get_price_history(listing_id) if listing_id else []
        except Exception:
            history = []
        try:
            favorite = self.storage.is_favorite(listing_id) if listing_id else False
        except Exception:
            favorite = False
        self.details_panel.set_listing(
            listing, favorite=favorite, history=history
        )

    def _toggle_favorite(self, listing: dict[str, Any], _desired: bool = True) -> None:
        listing_id = str(listing.get("id") or "")
        if not listing_id:
            return
        favorite = self.storage.is_favorite(listing_id)
        self.storage.set_favorite(listing_id, not favorite)
        for item in getattr(self, "last_cycle_listings", []):
            if str(item.get("id")) == listing_id:
                item["is_favorite"] = not favorite
        listing["is_favorite"] = not favorite
        self.table.refresh()
        self._show_listing_details(listing)
        self.status_label.configure(
            text="Убрано из избранного" if favorite else "Добавлено в избранное"
        )

    def _show_blacklist(self) -> None:
        window = ctk.CTkToplevel(self)
        window.title("Чёрный список продавцов")
        window.geometry("480x420")
        window.transient(self)
        ctk.CTkLabel(
            window,
            text="Заблокированные продавцы",
            font=ctk.CTkFont(size=15, weight="bold"),
        ).pack(anchor="w", padx=14, pady=(12, 6))
        body = ctk.CTkScrollableFrame(window, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        sellers = self.storage.list_blacklisted_sellers()
        if not sellers:
            ctk.CTkLabel(
                body, text="Список пуст.", text_color=("#777777", "#9aaebb")
            ).pack(anchor="w", padx=6, pady=6)
            return

        def rebuild() -> None:
            for child in body.winfo_children():
                child.destroy()
            for seller in self.storage.list_blacklisted_sellers():
                row = ctk.CTkFrame(body, fg_color="transparent")
                row.pack(fill="x", pady=2)
                ctk.CTkLabel(
                    row, text=seller, anchor="w", justify="left", wraplength=310
                ).pack(side="left", fill="x", expand=True)
                ctk.CTkButton(
                    row,
                    text="Разблокировать",
                    width=120,
                    command=lambda value=seller: (
                        self.storage.remove_blacklisted_seller(value),
                        rebuild(),
                    ),
                ).pack(side="right")

        rebuild()

    def _blacklist_seller(self, seller: str) -> None:
        self.storage.blacklist_seller(seller)
        self.status_label.configure(text=f"Продавец заблокирован: {seller[:40]}")
        if self.details_panel._listing:
            self._show_listing_details(self.details_panel._listing)

    def _clear_table_filters(self) -> None:
        self.quick_filter_var.set("")
        self.price_from_var.set("")
        self.price_to_var.set("")
        self.only_delivery_var.set(False)
        self.only_favorite_var.set(False)
        self.min_discount_var.set("Любая")
        self.table.set_quick_filter("")
        self.table.set_view_filters()

    def _preview_query_async(self, query_config: QueryConfig) -> None:
        if getattr(self, "_preview_running", False):
            messagebox.showinfo("Avito Radar", "Проверка уже выполняется.")
            return
        self._preview_running = True
        self.status_label.configure(text="Проверяю запрос: одна страница…")

        def run() -> None:
            scraper = None
            try:
                criteria = query_config.criteria()
                direct_url = None
                if query_config.source_url:
                    direct_url = build_effective_source_url(
                        query_config.source_url,
                        city=criteria.city,
                        price_min=criteria.price_min,
                        price_max=criteria.price_max,
                        local_priority=query_config.local_priority,
                        avito_sort=query_config.avito_sort,
                    )
                transport = str(
                    self.config.get("scraper", {}).get("transport", "auto")
                ).lower()
                scraper_class = (
                    HttpAvitoScraper
                    if transport in {"http", "auto", "aggressive"}
                    and HttpAvitoScraper is not None
                    else AvitoScraper
                )
                scraper = scraper_class(self.config)
                scraper.start()
                raw = scraper.search(
                    query_config.query,
                    city=criteria.city,
                    price_min=criteria.price_min,
                    price_max=criteria.price_max,
                    limit=50,
                    local_priority=query_config.local_priority,
                    direct_url=direct_url,
                    pages=1,
                    avito_sort=query_config.avito_sort,
                    category=query_config.category,
                )
                filter_criteria = (
                    replace(criteria, query="")
                    if query_config.source_url
                    else criteria
                )
                result = apply_filters(raw, filter_criteria)
                accepted = result.accepted[:5]
                rejected = result.rejected[:5]
                lines = [
                    f"Запрос: {query_config.query}",
                    f"Проверено: {result.total}",
                    f"Подходит: {len(result.accepted)}",
                    f"Отклонено: {len(result.rejected)}",
                    "",
                ]
                if accepted:
                    lines.append("Примеры подходящих:")
                    lines.extend(
                        f"• {item.get('title', '')[:70]}"
                        for item in accepted
                    )
                    lines.append("")
                if rejected:
                    lines.append("Примеры отклонённых:")
                    lines.extend(
                        f"• {item.get('title', '')[:60]} — {reason}"
                        for item, reason in rejected
                    )
                text = "\n".join(lines)
                self.after(0, lambda: self._show_preview_result(text))
            except Exception as exc:
                message = str(exc)
                self.after(
                    0,
                    lambda: messagebox.showerror(
                        "Avito Radar", f"Не удалось проверить запрос:\n{message}"
                    ),
                )
            finally:
                if scraper is not None:
                    try:
                        scraper.close()
                    except Exception:
                        pass
                self.after(0, self._finish_preview)

        threading.Thread(target=run, daemon=True, name="AvitoPreview").start()

    def _show_preview_result(self, text: str) -> None:
        messagebox.showinfo("Проверка запроса", text)

    def _finish_preview(self) -> None:
        self._preview_running = False
        self.status_label.configure(text="Проверка запроса завершена")

    def _on_start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        if getattr(self, "bot_manager", None) and self.bot_manager.current_job_id:
            messagebox.showinfo(
                "Avito Radar",
                "Сейчас выполняется запрос из Telegram. Дождитесь его завершения "
                "или остановите его командой /stop в боте.",
            )
            return
        if not self.query_cards:
            messagebox.showwarning(
                "Avito Radar", "Добавьте хотя бы один поисковый запрос."
            )
            return
        try:
            query_configs = self._collect_query_configs()
            interval = self._read_interval()
            mode = self.mode_var.get()
        except (FilterError, ValueError) as exc:
            messagebox.showerror("Avito Radar", str(exc))
            return
        recipients = self._selected_recipients()
        if not recipients:
            proceed = messagebox.askyesno(
                "Получатели не выбраны",
                "Уведомления в Telegram отправляться не будут.\n\n"
                "Продолжить сбор без получателей?",
            )
            if not proceed:
                return
        try:
            self.config = load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Ошибка config.toml:\n{exc}")
            return
        self.notifier = TelegramNotifier(self.config_path)
        self.last_rejected = []
        self.last_cycle_listings = []
        self.table.set_data([], [])
        self._hide_details()
        active_configs = [config for config in query_configs if config.is_active]
        if len(active_configs) == 1:
            active_key = active_configs[0].query_key()
            for label, key in getattr(self, "_query_labels", {}).items():
                if key == active_key:
                    self.query_select_var.set(label)
                    self.query_select_menu.set(label)
                    self.table.set_query_key(active_key)
                    self._update_table_title()
                    break
        self.filter_stats_label.configure(text="Идёт сбор…")
        self._set_running(True)
        logger.info(
            "Запуск из GUI: режим «%s», интервал/длительность %s мин, получателей %s",
            mode,
            interval,
            len(recipients),
        )
        self.worker = MonitorWorker(
            storage=self.storage,
            notifier=self.notifier,
            config=self.config,
            config_path=self.config_path,
            queries=query_configs,
            recipients=recipients,
            mode_label=mode,
            interval_minutes=interval,
            sort_key=self.table.sort_key,
            on_status=lambda text: self.ui_queue.put(("status", text)),
            on_error=lambda text, modal: self.ui_queue.put(
                ("error", {"message": text, "modal": modal})
            ),
            on_cycle=lambda listings, stats, cycle, rejected: self.ui_queue.put(
                (
                    "cycle",
                    {
                        "listings": listings,
                        "stats": stats,
                        "cycle": cycle,
                        "rejected": rejected,
                    },
                )
            ),
            on_finished=lambda reason: self.ui_queue.put(("finished", reason)),
            on_browser_visibility=lambda visible: self.ui_queue.put(
                ("browser_visibility", visible)
            ),
        )
        self.worker.start()
        self.storage.set_collector_state(
            {
                "status": "running",
                "kind": "manual",
                "query": ", ".join(config.query for config in query_configs)[:200],
                "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            }
        )

    def _toggle_browser(self) -> None:
        if not (self.worker and self.worker.is_alive()):
            logger.info("Браузер ещё не запущен")
            return
        scraper = getattr(self.worker, "_scraper", None)
        if scraper is None:
            logger.info("Браузер ещё запускается")
            return
        scraper.request_toggle()
        logger.info("Запрошено переключение окна Chromium")

    def _on_stop(self) -> None:
        if self.worker and self.worker.is_alive():
            logger.info("Пользователь нажал «Стоп»")
            self.worker.request_stop()
            self.stop_button.configure(state="disabled")
            self.browser_button.configure(state="disabled")
            self.status_label.configure(
                text="Останавливаю…",
                fg_color=("#f2e3c2", "#4a3b1d"),
                text_color=("#3a2c13", "#ffd98a"),
            )

    def _set_running(self, running: bool) -> None:
        if running:
            self.start_button.configure(state="disabled", text="Работает…")
            self.stop_button.configure(state="normal")
            self.browser_button.configure(
                state="normal" if self.browser_toggle_available else "disabled",
                text=(
                    "Показать браузер"
                    if self.browser_toggle_available
                    else "Окно недоступно"
                ),
            )
            if not self.stop_button.winfo_manager():
                self.stop_button.pack(side="left", padx=(8, 0))
            if (
                self.browser_toggle_available
                and not self.browser_button.winfo_manager()
            ):
                self.browser_button.pack(side="left", padx=(6, 0))
            self.status_label.configure(
                text="Работает",
                fg_color=("#d7f0dd", "#1e3d28"),
                text_color=("#174d2a", "#9be3ae"),
            )
        else:
            self.start_button.configure(state="normal", text="Старт")
            self.stop_button.configure(state="disabled")
            self.browser_button.configure(
                state="disabled",
                text=(
                    "Показать браузер"
                    if self.browser_toggle_available
                    else "Окно недоступно"
                ),
            )
            if self.stop_button.winfo_manager():
                self.stop_button.pack_forget()
            if self.browser_button.winfo_manager():
                self.browser_button.pack_forget()
            self.status_label.configure(
                text="Ожидание",
                fg_color=("#e7e7e7", "#2d2f31"),
                text_color=("#333333", "#e0e0e0"),
            )
            try:
                if "Идёт сбор" in str(self.filter_stats_label.cget("text")):
                    self.filter_stats_label.configure(text="Цикл завершён")
            except Exception:
                pass

    # ------------------------------ bot token ------------------------------

    def _toggle_token_visibility(self) -> None:
        self.token_visible = not self.token_visible
        self.token_entry.configure(show="" if self.token_visible else "•")
        self.token_toggle_button.configure(
            text="Скрыть" if self.token_visible else "Показать"
        )

    def _paste_token_event(self, _event: Any = None) -> str:
        self._paste_token()
        return "break"

    def _paste_token(self) -> None:
        try:
            value = self.clipboard_get()
        except Exception:
            messagebox.showwarning(
                "Avito Radar",
                "Буфер обмена пуст или недоступен. Скопируйте токен ещё раз "
                "или впишите его в config.toml.",
            )
            return
        value = (value or "").strip()
        if not value:
            messagebox.showwarning("Avito Radar", "В буфере обмена нет текста.")
            return
        self.token_entry.delete(0, "end")
        self.token_entry.insert(0, value)
        self.token_entry.focus_set()
        # Не показываем и не логируем сам токен.
        self._update_telegram_status(
            "Токен вставлен в поле. Нажмите «Сохранить токен».",
            ok=None,
        )
        logger.info("Токен вставлен из буфера обмена (длина %s)", len(value))

    def _open_config(self) -> None:
        try:
            os.startfile(str(self.config_path))  # type: ignore[attr-defined]
        except AttributeError:
            import webbrowser

            webbrowser.open(self.config_path.as_uri())
        except Exception as exc:
            messagebox.showerror(
                "Avito Radar", f"Не удалось открыть config.toml:\n{exc}"
            )

    def _save_token(self) -> None:
        token = self.token_entry.get().strip()
        if token and (":" not in token or len(token) < 20):
            messagebox.showerror(
                "Avito Radar",
                "Это не похоже на токен Telegram. Он должен иметь вид "
                "123456789:AA...\n\nСкопируйте токен из @BotFather целиком.",
            )
            return
        api_base = self.api_base_entry.get().strip().rstrip("/")
        if not api_base:
            api_base = "https://api.telegram.org"
        if not api_base.startswith(("http://", "https://")):
            messagebox.showerror(
                "Avito Radar",
                "API Telegram должен начинаться с http:// или https://.",
            )
            return
        proxy_url = self.proxy_entry.get().strip()
        if proxy_url and not proxy_url.startswith(
            ("http://", "https://", "socks5://", "socks5h://", "socks4://")
        ):
            messagebox.showerror(
                "Avito Radar",
                "Прокси должен начинаться с http://, https://, socks5:// или socks5h://.",
            )
            return
        try:
            save_telegram_token(self.config_path, token)
            save_config_scalar(self.config_path, "telegram", "api_base", api_base)
            save_config_scalar(self.config_path, "telegram", "proxy_url", proxy_url)
            self.config = load_config(self.config_path)
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Не удалось сохранить Telegram:\n{exc}")
            return
        logger.info(
            "Токен бота %s; слушатель Telegram перезапускается",
            "сохранён" if token else "очищен",
        )
        self._restart_bot_listener()
        messagebox.showinfo(
            "Avito Radar",
            (
                "Токен сохранён. Проверяю подключение к Telegram.\n"
                "Статус появится в блоке «Получатели» и в журнале."
            )
            if token
            else "Токен удалён из config.toml.",
        )

    def _start_bot_listener(self) -> None:
        if get_telegram_token(self.config):
            self._restart_bot_listener()
        else:
            self._update_telegram_status("Telegram: токен не задан", ok=False)
            logger.info(
                "Токен бота не задан: слушатель /start не запущен. "
                "Впишите токен и нажмите «Сохранить токен»."
            )

    def _restart_bot_listener(self) -> None:
        if self.bot_listener and self.bot_listener.is_alive():
            self.bot_listener.stop()
            self.bot_listener.join(timeout=0.8)
            if self.bot_listener.is_alive():
                self._update_telegram_status(
                    "Telegram: перезапускаю слушатель…", ok=None
                )
                self.after(500, self._restart_bot_listener)
                return
        self.bot_listener = None
        token = get_telegram_token(self.config)
        if not token:
            self._update_telegram_status("Telegram: токен не задан", ok=False)
            logger.info("Слушатель Telegram не запущен: токен пуст")
            return
        self._update_telegram_status("Telegram: подключаюсь…", ok=None)
        self.bot_listener = BotListener(
            self.storage,
            self.config_path,
            on_subscribers_changed=lambda: self.ui_queue.put(("subscribers", None)),
            on_jobs_changed=lambda: (
                self.ui_queue.put(("bot_jobs_changed", None)),
                self.bot_manager.wake(),
            ),
            on_error=lambda message: (
                self.ui_queue.put(("telegram_error", message)),
                self.ui_queue.put(("error", {"message": message, "modal": True})),
            ),
            on_ready=lambda username: self.ui_queue.put(
                ("telegram_ready", username)
            ),
        )
        self.bot_listener.start()
        logger.info("Слушатель Telegram запущен")

    # ------------------------------ actions ------------------------------

    def _open_log(self) -> None:
        path = app_path(self.config, "log_path", "logs/avito_radar.log")
        if not path.exists():
            messagebox.showinfo("Avito Radar", f"Файл лога ещё не создан:\n{path}")
            return
        try:
            os.startfile(str(path))  # type: ignore[attr-defined]
        except AttributeError:
            import webbrowser

            webbrowser.open(path.as_uri())
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Не удалось открыть лог:\n{exc}")

    def _export_manual(self) -> None:
        listings = self.last_cycle_listings
        if not listings and not self.last_rejected:
            messagebox.showinfo("Avito Radar", "Пока нет данных для экспорта.")
            return
        export_dir = app_path(self.config, "export_dir", "exports")
        export_dir.mkdir(parents=True, exist_ok=True)
        initial = f"avito_radar_{datetime.now().strftime('%Y%m%d_%H%M')}.xlsx"
        path = filedialog.asksaveasfilename(
            title="Экспорт в XLSX",
            initialdir=str(export_dir),
            initialfile=initial,
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
        )
        if not path:
            return
        history = {
            str(listing["id"]): self.storage.get_price_history(str(listing["id"]))
            for listing in listings
        }
        try:
            saved = export_xlsx(
                listings,
                path,
                sort_key=self.table.sort_key,
                price_history=history,
                rejected=self.last_rejected,
            )
        except ReporterError as exc:
            messagebox.showerror("Avito Radar", str(exc))
            return
        except Exception as exc:
            logger.exception("Ошибка экспорта XLSX")
            messagebox.showerror("Avito Radar", f"Ошибка экспорта XLSX:\n{exc}")
            return
        logger.info("Ручной экспорт XLSX: %s", saved)
        messagebox.showinfo("Avito Radar", f"Файл сохранён:\n{saved}")

    # ------------------------------ queue/log ------------------------------

    def _append_log(self, message: str, level: int = logging.INFO) -> None:
        try:
            self.log_text.configure(state="normal")
            self.log_text.insert("end", str(message).rstrip() + "\n")
            line_count = int(self.log_text.index("end-1c").split(".")[0])
            if line_count > 1200:
                self.log_text.delete("1.0", "201.0")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        except Exception:
            pass

    def _refresh_market_analysis(self) -> None:
        rows: list[dict[str, Any]] = []
        for card in self.query_cards:
            try:
                query_config = card.get_config(validate=False)
            except Exception:
                continue
            listings = self.storage.get_query_listings(query_config.query_key())
            if not listings:
                continue
            label = query_config.query
            if query_config.city:
                label = f"{label} · {query_config.city}"
            else:
                label = f"{label} · Вся Россия"
            market_rows = market.analyze(
                listings,
                query_label=label,
                city=query_config.city,
            )
            for market_row in market_rows:
                market_row["_query_key"] = query_config.query_key()
            rows.extend(market_rows)
        self._market_by_id = {
            str((row.get("listing") or {}).get("id")): row
            for row in rows
            if (row.get("listing") or {}).get("id") is not None
        }
        if hasattr(self, "market_table"):
            self.market_table.set_rows(rows)
            current_key = getattr(self, "_query_labels", {}).get(
                self.query_select_var.get(), ""
            )
            self.market_table.set_query_key(current_key)

    def _annotate_market(
        self, listings: list[dict[str, Any]], rejected: list[dict[str, Any]]
    ) -> None:
        try:
            favorites = self.storage.list_favorite_ids()
        except Exception:
            favorites = set()
        for listing in listings:
            listing["is_favorite"] = str(listing.get("id")) in favorites
            row = self._market_by_id.get(str(listing.get("id")))
            if row:
                listing["market_diff"] = row.get("vs_market")
                listing["market_median"] = row.get("market")
        for entry in rejected:
            listing = entry.get("listing") or {}
            row = self._market_by_id.get(str(listing.get("id")))
            if row:
                listing["market_diff"] = row.get("vs_market")
                listing["market_median"] = row.get("market")

    def _poll_queue(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self.ui_queue.get_nowait()
                if kind == "log":
                    text, level = payload
                    self._append_log(text, level)
                elif kind == "status":
                    status_text = str(payload)
                    self._last_worker_status = status_text
                    self.status_label.configure(text=status_text)
                    lowered = status_text.casefold()
                    if "проверк" in lowered or "капч" in lowered:
                        self.browser_button.configure(text="Открыть проверку Avito")
                elif kind == "browser_visibility":
                    if self.worker and self.worker.is_alive():
                        self.browser_button.configure(
                            text="Скрыть браузер" if payload else "Показать браузер"
                        )
                elif kind == "error":
                    message = payload["message"]
                    self.status_label.configure(
                        text="Ошибка",
                        fg_color=("#f2d7d7", "#4a2222"),
                        text_color=("#8f2020", "#ffb3b3"),
                    )
                    if payload.get("modal"):
                        self._show_modal_error_once(message)
                elif kind == "cycle":
                    listings = payload["listings"]
                    stats = payload["stats"]
                    cycle = payload.get("cycle") or {}
                    rejected = payload.get("rejected") or []
                    self.last_rejected = rejected
                    self.last_cycle_listings = listings
                    self._refresh_market_analysis()
                    self._annotate_market(listings, rejected)
                    self.table.set_data(listings, rejected)
                    self._hide_details()
                    self.stats_label.configure(
                        text=(
                            f"В базе: {stats.get('listings', 0)} · активно: "
                            f"{stats.get('active', 0)} · снято: {stats.get('missing', 0)} · "
                            f"без цены: {stats.get('no_price', 0)}"
                        )
                    )
                    if cycle:
                        self.filter_stats_label.configure(
                            text=(
                                "Цикл: найдено "
                                f"{cycle.get('found', 0)} · подходит "
                                f"{cycle.get('accepted', 0)} · отклонено "
                                f"{cycle.get('rejected', 0)} · новых "
                                f"{cycle.get('new', 0)} · изменение цены "
                                f"{cycle.get('price', 0)} · снято "
                                f"{cycle.get('missing', 0)}"
                            )
                        )

                elif kind == "finished":
                    self.storage.set_collector_state({"status": "idle"})
                    self._set_running(False)
                    self.status_label.configure(
                        text=str(payload),
                        fg_color=("#e7e7e7", "#2d2f31"),
                        text_color=("#333333", "#e0e0e0"),
                    )
                    self._refresh_recipients()
                elif kind == "bot_jobs_changed":
                    self._reload_query_cards()
                elif kind == "subscribers":
                    self._refresh_recipients()
                elif kind == "telegram_ready":
                    self._update_telegram_status(
                        f"Telegram: подключён, бот @{payload}. /start принимается.",
                        ok=True,
                    )
                    self._refresh_recipients()
                elif kind == "telegram_error":
                    self._update_telegram_status(
                        "Telegram: ошибка подключения — подробности в журнале",
                        ok=False,
                    )
        except queue.Empty:
            pass
        except Exception:
            logger.debug("Ошибка обработки UI-очереди", exc_info=True)
        if not self._closing:
            self._poll_after_id = self.after(120, self._poll_queue)

    def _show_modal_error_once(self, message: str) -> None:
        now = time.monotonic()
        if message == self._last_modal[0] and now - self._last_modal[1] < 30:
            return
        self._last_modal = (message, now)
        try:
            messagebox.showerror("Avito Radar", message)
        except Exception:
            pass

    # ------------------------------ shutdown ------------------------------

    def _save_ui_state(self) -> None:
        try:
            state = {
                "query_key": getattr(self, "_query_labels", {}).get(
                    self.query_select_var.get(), ""
                ),
                "sort_key": self.table.sort_key,
                "price_from": self.price_from_var.get(),
                "price_to": self.price_to_var.get(),
                "only_delivery": bool(self.only_delivery_var.get()),
                "only_favorite": bool(self.only_favorite_var.get()),
                "min_discount": self.min_discount_var.get(),
                "log_collapsed": bool(getattr(self, "_log_collapsed", True)),
                "mode": self.mode_var.get(),
                "interval": self.interval_entry.get(),
                "expanded": [
                    str(card.db_id)
                    for card in self.query_cards
                    if card._expanded and card.db_id is not None
                ],
            }
            self.storage.set_meta("ui_state", json.dumps(state, ensure_ascii=False))
        except Exception:
            logger.debug("Не удалось сохранить состояние интерфейса", exc_info=True)

    def _restore_ui_state(self) -> None:
        try:
            raw = self.storage.get_meta("ui_state", "")
            state = json.loads(raw) if raw else {}
        except Exception:
            state = {}
        if not state:
            return
        query_key = str(state.get("query_key") or "")
        if query_key:
            for label, key in getattr(self, "_query_labels", {}).items():
                if key == query_key:
                    self.query_select_var.set(label)
                    self.query_select_menu.set(label)
                    break
        try:
            self.table.set_sort_key(str(state.get("sort_key") or "market_diff"))
        except Exception:
            pass
        self.price_from_var.set(str(state.get("price_from") or ""))
        self.price_to_var.set(str(state.get("price_to") or ""))
        self.only_delivery_var.set(bool(state.get("only_delivery")))
        self.only_favorite_var.set(bool(state.get("only_favorite")))
        self.min_discount_var.set(str(state.get("min_discount") or "Любая"))
        self._apply_table_filters()
        self._apply_query_filter()
        if bool(state.get("log_collapsed", True)) != bool(self._log_collapsed):
            self._toggle_log_panel()
        mode = str(state.get("mode") or MODE_ONCE)
        if mode in MODES:
            self.mode_var.set(mode)
            self._on_mode_change(mode)
        interval = str(state.get("interval") or "")
        if interval:
            self.interval_entry.delete(0, "end")
            self.interval_entry.insert(0, interval)
        expanded = {str(value) for value in state.get("expanded", [])}
        for card in self.query_cards:
            if str(card.db_id) in expanded and not card._expanded:
                card._toggle()

    def _on_close(self) -> None:
        if self._closing:
            return
        if self.worker and self.worker.is_alive():
            proceed = messagebox.askyesno(
                "Avito Radar",
                "Мониторинг ещё работает. Остановить его и закрыть приложение?",
            )
            if not proceed:
                return
        if self._query_save_after_id is not None:
            try:
                self.after_cancel(self._query_save_after_id)
            except Exception:
                pass
            self._query_save_after_id = None
        if self._poll_after_id is not None:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None
        self._save_ui_state()
        self._persist_queries()
        self._closing = True
        if self.worker and self.worker.is_alive():
            self.worker.request_stop()
        if self.bot_listener and self.bot_listener.is_alive():
            self.bot_listener.stop()
        if getattr(self, "bot_manager", None):
            self.bot_manager.stop()
            self.bot_manager.join(timeout=5)
        logger.info("Закрытие Avito Radar")
        logging.getLogger().removeHandler(self._log_handler)
        self._wait_for_worker_and_close(time.monotonic() + 10)

    def _wait_for_worker_and_close(self, deadline: float) -> None:
        if self.worker and self.worker.is_alive() and time.monotonic() < deadline:
            self.after(150, lambda: self._wait_for_worker_and_close(deadline))
            return
        try:
            self.storage.close()
        except Exception:
            pass
        try:
            self.destroy()
        except Exception:
            pass
