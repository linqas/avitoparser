"""Быстрый HTTP-сканер Avito на основе открытого avito-parser.

Он не управляет Chromium: получает внутренний JSON Avito через curl_cffi.
Если HTTP-путь недоступен, прозрачно переключается на обычный Playwright.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

from avito_http.avito_client import CalmClient, page_from_payload
from avito_http.link_resolver import resolve
from scraper import (
    AvitoBlockedError,
    AvitoScraper,
    ScraperCancelled,
    ScraperError,
    build_search_url,
)
from utils import (
    APP_DIR,
    normalize_listing,
    now_moscow,
    parse_published_at,
)

logger = logging.getLogger("avito_radar.http_scraper")


class HttpAvitoScraper:
    """HTTP-клиент с откатом на Playwright при проблемах."""

    def __init__(
        self,
        config: dict[str, Any],
        *,
        stop_event: Any = None,
        on_browser_visibility: Any = None,
    ) -> None:
        self.config = config
        self.stop_event = stop_event
        self.on_browser_visibility = on_browser_visibility
        scraper_config = config.get("scraper", {})
        self.base_url = str(scraper_config.get("base_url", "https://www.avito.ru"))
        self.delay = max(0.5, float(scraper_config.get("http_delay_seconds", 2.0)))
        os.environ["AVITO_AUTO_CAPTCHA"] = (
            "1" if bool(scraper_config.get("auto_captcha", True)) else "0"
        )
        logger.info(
            "HTTP: авто-капча=%s, fallback=%s",
            "вкл" if os.environ["AVITO_AUTO_CAPTCHA"] == "1" else "выкл",
            "вкл" if str(scraper_config.get("transport", "auto")).lower() == "auto" else "выкл",
        )
        self.cookie_path = APP_DIR / "data" / "http_cookies.json"
        self.client = CalmClient(
            delay=self.delay,
            log=self._event,
            cookie_path=str(self.cookie_path),
        )
        self._locations_cache: dict[str, Any] = {}
        self._resolved: dict[str, Any] = {}
        self._fallback: AvitoScraper | None = None
        transport = str(scraper_config.get("transport", "auto")).lower()
        self.allow_fallback = transport == "auto"
        os.environ["AVITO_FAST_FALLBACK"] = "1" if self.allow_fallback else "0"
        self._closed = False

    def _event(self, kind: str, **fields: Any) -> None:
        if kind in {"SESSION-RESET", "IP-BLOCK", "TRANSPORT"}:
            detail = fields.get("reason") or fields.get("error") or ""
            logger.warning("HTTP Avito: %s %s", kind, detail)
        elif kind.startswith("CAPTCHA"):
            logger.info("HTTP Avito: %s %s", kind, fields)
        else:
            logger.debug("HTTP Avito: %s %s", kind, fields)

    def _check_cancel(self) -> None:
        if self.stop_event is not None and self.stop_event.is_set():
            raise ScraperCancelled("Сбор остановлен")

    def _fallback_scraper(self) -> AvitoScraper:
        if self._fallback is None:
            logger.info("HTTP-клиент недоступен; переключаюсь на Playwright")
            self._fallback = AvitoScraper(
                self.config,
                stop_event=self.stop_event,
                on_browser_visibility=self.on_browser_visibility,
            )
            self._fallback.start()
        return self._fallback

    def start(self) -> None:
        # HTTP-клиент не требует запуска браузера.
        return

    def _persist_browser_cookies(self) -> None:
        context = getattr(self._fallback, "_context", None) if self._fallback else None
        if context is None:
            return
        try:
            cookies = context.cookies()
            self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
            self.cookie_path.write_text(
                json.dumps(cookies, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("Cookies после ручной проверки сохранены для HTTP")
        except Exception as exc:
            logger.debug("Не удалось сохранить cookies браузера: %s", exc)

    def close(self) -> None:
        self._closed = True
        self._persist_browser_cookies()
        self.client.save_cookies()
        if self._fallback is not None:
            self._fallback.close()
        session = getattr(self.client, "session", None)
        if session is not None:
            try:
                session.close()
            except Exception:
                pass

    def _process_toggle_request(self) -> None:
        if self._fallback is not None:
            self._fallback._process_toggle_request()

    def _resolve(self, url: str, city: str) -> Any:
        if url not in self._resolved:
            self._check_cancel()
            self._resolved[url] = resolve(
                self.client,
                url,
                locations_cache=self._locations_cache,
                city=city or None,
            )
        return self._resolved[url]

    @staticmethod
    def _date_text(item: dict[str, Any]) -> str:
        for step in (item.get("iva") or {}).get("DateInfoStep") or []:
            if not isinstance(step, dict):
                continue
            payload = step.get("payload") or {}
            value = payload.get("relative") or payload.get("absolute")
            if value:
                return str(value)
        return ""

    @staticmethod
    def _badges(item: dict[str, Any]) -> list[str]:
        result: list[str] = []
        for step in (item.get("iva") or {}).get("BadgeBarStep") or []:
            if not isinstance(step, dict):
                continue
            payload = step.get("payload") or {}
            for badge in payload.get("badges") or []:
                if isinstance(badge, dict) and badge.get("title"):
                    result.append(str(badge["title"]))
        return result

    def _to_listing(self, item: dict[str, Any]) -> dict[str, Any] | None:
        item_id = item.get("id")
        url_path = str(item.get("urlPath") or "")
        if item_id is None or not url_path:
            return None
        price = (item.get("priceDetailed") or {}).get("value")
        badges = self._badges(item)
        badge_text = " ".join(badges).casefold()
        if "доставка" in badge_text:
            delivery = "yes"
        elif "самовывоз" in badge_text:
            delivery = "no"
        else:
            delivery = "unknown"
        date_text = self._date_text(item)
        published_at = parse_published_at(date_text, now=now_moscow()) if date_text else None
        geo = item.get("geo") or {}
        seller_values: list[str] = []
        for step in (item.get("iva") or {}).get("SecondLineStep") or []:
            if isinstance(step, dict):
                value = (step.get("payload") or {}).get("value")
                if value:
                    seller_values.append(str(value))
        seller_info = item.get("seller")
        if isinstance(seller_info, dict):
            for key in ("name", "sellerName", "companyName", "title"):
                if seller_info.get(key):
                    seller_values.append(str(seller_info[key]))
        for key in ("sellerName", "seller_name", "companyName"):
            if item.get(key):
                seller_values.append(str(item[key]))
        if item.get("isCompany") and not seller_values:
            seller_values.append("Компания")
        raw = {
            "id": str(item_id),
            "title": str(item.get("title") or ""),
            "price": price if price is not None else "",
            "published_at": published_at or "",
            "city": str(geo.get("formattedAddress") or ""),
            "delivery": delivery,
            "seller": " ".join(seller_values),
            "url": self.base_url.rstrip("/") + "/" + url_path.lstrip("/"),
        }
        try:
            return normalize_listing(raw, now=now_moscow())
        except Exception as exc:
            logger.debug("HTTP Avito: пропущена карточка %s: %s", item_id, exc)
            return None

    def search(
        self,
        query: str,
        city: str = "",
        price_min: int | None = None,
        price_max: int | None = None,
        limit: int | None = None,
        local_priority: bool = False,
        direct_url: str | None = None,
        pages: int = 1,
        avito_sort: int = 0,
        category: str = "",
    ) -> list[dict[str, Any]]:
        limit = max(1, int(limit or 50))
        pages = max(1, min(30, int(pages or 1)))
        try:
            url = direct_url or build_search_url(
                self.base_url,
                query,
                city,
                price_min,
                price_max,
                local_priority=local_priority,
                avito_sort=avito_sort,
                category=category,
            )
            resolved = self._resolve(url, city)
            aggregate: list[dict[str, Any]] = []
            seen: set[str] = set()
            for number in range(1, pages + 1):
                self._check_cancel()
                payload = self.client.page_payload(
                    catalog_url=resolved.catalog_url,
                    params=resolved.params,
                    number=number,
                )
                page = page_from_payload(payload, number)
                logger.info(
                    "HTTP Avito: страница %s, карточек %s из %s",
                    number,
                    len(page.items),
                    page.total_count,
                )
                for item in page.items:
                    listing = self._to_listing(item)
                    if not listing or listing["id"] in seen:
                        continue
                    seen.add(listing["id"])
                    aggregate.append(listing)
                    if len(aggregate) >= limit * pages:
                        break
                if not page.items:
                    break
            return aggregate
        except ScraperCancelled:
            raise
        except Exception as exc:
            logger.warning("HTTP-сбор не удался: %s", exc)
            if not self.allow_fallback:
                raise ScraperError(f"HTTP-сбор не удался: {exc}") from exc
            fallback = self._fallback_scraper()
            result = fallback.search(
                query,
                city=city,
                price_min=price_min,
                price_max=price_max,
                limit=limit,
                local_priority=local_priority,
                direct_url=direct_url,
                pages=pages,
                avito_sort=avito_sort,
                category=category,
            )
            self._persist_browser_cookies()
            return result

    def enrich_listing_dates(self, listings: list[dict[str, Any]]) -> None:
        # HTTP-выдача уже содержит DateInfoStep; для оставшихся дат
        # используется Playwright-fallback, чтобы не усложнять HTTP-цикл.
        if self._fallback is not None:
            self._fallback.enrich_listing_dates(listings)
