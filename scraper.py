"""Сбор публичных карточек Avito через Playwright Chromium.

Важно:
* браузер работает в обычном режиме (не headless), но окно по умолчанию
  скрыто за пределами экрана и показывается только для ручной проверки Avito;
* один браузер, одна страница, последовательные запросы — без параллельного
  скрейпинга;
* между действиями со страницей выдерживается пауза 5–10 секунд;
* имена и телефоны продавцов не извлекаются и не сохраняются.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from utils import (
    APP_DIR,
    app_path,
    city_to_slug,
    is_captcha_text,
    is_no_results_text,
    load_config,
    logger_for,
    normalize_listing,
    now_moscow,
    parse_published_at,
    setup_logging,
)

logger = logger_for("scraper")

CARD_SELECTOR = '[data-marker="item"]'
ITEMS_RESPONSE_MARKER = "/web/1/js/items"


def with_page(url: str, page: int) -> str:
    """Возвращает URL поиска с нужным номером страницы Avito."""
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "p"]
    if page > 1:
        query.append(("p", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))
CAPTCHA_SELECTORS = (
    ".geetest_holder",
    ".geetest_panel",
    ".geetest_box",
    'iframe[src*="geetest"]',
    '[class*="captcha"]',
    '[id*="captcha"]',
)
DETAIL_DATE_SELECTORS = (
    '[data-marker="item-view/item-date"]',
    '[data-marker*="item-date"]',
    'time[datetime]',
    '[class*="item-date"]',
    '[class*="date-text"]',
)
DETAIL_DATE_SELECTOR = ", ".join(DETAIL_DATE_SELECTORS)


class ScraperError(RuntimeError):
    """Ошибка сбора, после которой можно продолжить следующий цикл."""


class ScraperUnavailableError(ScraperError):
    """Нет Playwright/браузера или не удалось запустить Chromium."""


class AvitoBlockedError(ScraperError):
    """Avito показал капчу или ограничил доступ."""


class ScraperCancelled(ScraperError):
    """Пользователь нажал «Стоп»."""


def build_search_url(
    base_url: str,
    query: str,
    city: str = "",
    price_min: int | None = None,
    price_max: int | None = None,
    local_priority: bool = False,
    avito_sort: int = 0,
    category: str = "",
) -> str:
    base = (base_url or "https://www.avito.ru").rstrip("/")
    path_parts = []
    if (city or "").strip():
        path_parts.append(city_to_slug(city))
    if (category or "").strip():
        path_parts.append(category.strip("/"))
    path = "/" + "/".join(path_parts) if path_parts else ""
    params: dict[str, Any] = {"q": (query or "").strip()}
    if price_min is not None:
        params["pmin"] = int(price_min)
    if price_max is not None:
        params["pmax"] = int(price_max)
    if local_priority and (city or "").strip():
        # Штатный переключатель Avito «Сначала в выбранном городе».
        params["localPriority"] = 1
    if int(avito_sort or 0) == 172297:
        params["s"] = "172297_desc"
    elif int(avito_sort or 0) in {1, 2, 104}:
        params["s"] = int(avito_sort)
    return f"{base}{path}?{urlencode(params)}"


# JS возвращает только перечисленные публичные поля. Внутренний текст карточки
# используется лишь для классификации доставки/продавца и наружу не попадает.
_EXTRACT_JS = r"""
() => {
  const clean = (value) => (value || '').replace(/\s+/g, ' ').trim();
  const pickText = (root, selectors) => {
    for (const selector of selectors) {
      const node = root.querySelector(selector);
      if (node) {
        const text = clean(node.innerText || node.textContent || '');
        if (text) return text;
      }
    }
    return '';
  };
  const cards = Array.from(document.querySelectorAll('[data-marker="item"]'));
  return cards.map((card) => {
    const link = card.querySelector('[data-marker="item-title"]')
      || card.querySelector('a[itemprop="url"]')
      || card.querySelector('a[href]');
    const href = link ? (link.href || link.getAttribute('href') || '') : '';
    let id = card.getAttribute('data-item-id')
      || card.getAttribute('data-id')
      || card.getAttribute('data-avito-id')
      || '';
    if (!id && href) {
      const match = href.match(/_(\d{6,})(?:[?#]|$)/)
        || href.match(/\/(\d{6,})(?:[?#]|\/|$)/);
      if (match) id = match[1];
    }
    const titleNode = card.querySelector('[data-marker="item-title"]')
      || card.querySelector('h3 a')
      || link;
    const title = clean(titleNode ? (titleNode.innerText || titleNode.textContent) : '')
      || clean(link ? link.getAttribute('title') : '');
    const priceNode = card.querySelector('[data-marker="item-price"]')
      || card.querySelector('meta[itemprop="price"]')
      || card.querySelector('[class*="price"]');
    let price = '';
    if (priceNode) {
      price = clean(priceNode.getAttribute('content')
        || priceNode.innerText
        || priceNode.textContent);
    }
    const dateNode = card.querySelector(
      'time[datetime], [datetime], [data-marker*="date"]'
    );
    let date = '';
    if (dateNode) {
      date = clean(dateNode.getAttribute('datetime')
        || dateNode.innerText
        || dateNode.textContent);
    }
    if (!date) {
      date = pickText(card, [
        '[data-marker="item-date"]',
        '.date-text',
        '[class*="date-text"]',
        '[class*="item-date"]',
        '[class*="date"]'
      ]);
    }
    const city = pickText(card, [
      '[data-marker="item-location"]',
      '[class*="geo-address"]',
      '[class*="geo-root"]',
      '[class*="location"]'
    ]);
    let seller = pickText(card, [
      '[data-marker="item-seller"]',
      '[data-marker*="seller"]',
      '[class*="seller-info"]',
      '[class*="seller"]'
    ]);
    if (!seller) {
      const sellerMatch = clean(card.innerText || '').match(
        /(частное лицо|компания|агентство|магазин|застройщик)/i
      );
      if (sellerMatch) seller = sellerMatch[1];
    }

    const cardText = clean(card.innerText || '').toLowerCase();
    let delivery = 'unknown';
    if (cardText.includes('авито доставка') || cardText.includes('доставка')) {
      delivery = 'yes';
    } else if (cardText.includes('самовывоз') || cardText.includes('без доставки')) {
      delivery = 'no';
    }
    let views = '';
    const viewsMatch = (card.innerText || '').match(
      /(\d[\d\s\u00a0]*)\s*(?:просмотр|просмотра|просмотров)/i
    );
    if (viewsMatch) views = viewsMatch[0];
    return {
      id: clean(id),
      title: title,
      price: price,
      published_at: date,
      city: city,
      seller: seller,
      delivery: delivery,
      url: href,
      views: views
    };
  });
}
"""


def configure_local_playwright_browser() -> Path | None:
    """Настраивает Chromium и временную папку внутри каталога проекта.

    Это обходит запрет записи в C:\\Users\\...\\AppData\\Local:
    браузер лежит в <проект>\\.playwright-browsers, а Playwright получает
    записываемую временную папку <проект>\\.tmp.
    """
    runtime_tmp = APP_DIR / ".tmp"
    try:
        runtime_tmp.mkdir(parents=True, exist_ok=True)
        # Playwright создаёт здесь профиль и artifacts; системный %TEMP%
        # в некоторых конфигурациях Windows недоступен для записи.
        os.environ["TMP"] = str(runtime_tmp)
        os.environ["TEMP"] = str(runtime_tmp)
        os.environ["TMPDIR"] = str(runtime_tmp)
        logger.info("Временная папка Chromium: %s", runtime_tmp)
    except OSError as exc:
        logger.warning("Не удалось подготовить локальную временную папку: %s", exc)

    local_browsers = APP_DIR / ".playwright-browsers"
    if local_browsers.is_dir() and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local_browsers)
        logger.info("Playwright использует локальный Chromium: %s", local_browsers)
        return local_browsers
    return None


class AvitoScraper:
    def __init__(
        self,
        config: dict[str, Any] | None = None,
        *,
        config_path: str | Path | None = None,
        stop_event: Any | None = None,
        on_browser_visibility: Callable[[bool], None] | None = None,
    ):
        self.config = config or load_config(config_path)
        scraper_cfg = self.config.get("scraper", {})
        app_cfg = self.config.get("app", {})
        self.base_url = str(scraper_cfg.get("base_url", "https://www.avito.ru"))
        self.timeout_ms = int(scraper_cfg.get("navigation_timeout_ms", 60_000))
        self.pause_min = max(5.0, float(scraper_cfg.get("action_pause_min", 5.0)))
        self.pause_max = max(
            self.pause_min + 0.1, float(scraper_cfg.get("action_pause_max", 10.0))
        )
        self.scroll_attempts = max(0, int(scraper_cfg.get("scroll_attempts", 3)))
        # 0 означает «ждать проверку без ограничения по времени».
        self.captcha_wait_seconds = max(
            0, int(scraper_cfg.get("captcha_wait_seconds", 0))
        )
        self.hidden_window = bool(scraper_cfg.get("hidden_window", True))
        self.show_on_captcha = bool(scraper_cfg.get("show_on_captcha", True))
        # Автоклик по проверке IP отключён: проверку проходит пользователь.
        self.auto_ip_check = False
        self.ip_check_wait_seconds = max(
            5, int(scraper_cfg.get("ip_check_wait_seconds", 30))
        )
        self.enrich_missing_dates = bool(
            scraper_cfg.get("enrich_missing_dates", True)
        )
        date_pause_min = max(
            0.1, float(scraper_cfg.get("date_lookup_pause_min", 1.0))
        )
        self.date_pause_min = date_pause_min
        self.date_pause_max = max(
            date_pause_min + 0.1,
            float(scraper_cfg.get("date_lookup_pause_max", 2.0)),
        )
        self.window_width = 1280
        self.window_height = 900
        self.default_limit = max(1, int(app_cfg.get("max_listings", 50)))
        # Headless всегда выключен: Avito активнее блокирует его, а при капче
        # нечего показать пользователю. Окно скрывается позиционированием.
        self.headless = False
        self.stop_event = stop_event
        self._playwright = None
        self._browser = None
        self._context = None
        self.page = None
        self._started = False
        self._toggle_requested = threading.Event()
        self.on_browser_visibility = on_browser_visibility
        self._manual_visible = False

    # --------------------------- browser lifecycle ---------------------------

    def start(self) -> None:
        if self._started:
            return
        configure_local_playwright_browser()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise ScraperUnavailableError(
                "Playwright не установлен. Выполните: pip install -r requirements.txt"
            ) from exc

        try:
            self._playwright = sync_playwright().start()
            profile_dir = APP_DIR / ".browser-profile"
            profile_dir.mkdir(parents=True, exist_ok=True)
            # Постоянный профиль сохраняет Cookie и результат ручной проверки
            # Avito между запусками. Это не обход капчи, а обычный браузерный
            # профиль, который пользователь может использовать сам.
            window_args = (
                [
                    f"--window-position=-32000,-32000",
                    f"--window-size={self.window_width},{self.window_height}",
                    # Скрытое окно не должно замедляться из-за occlusion throttling.
                    "--disable-backgrounding-occluded-windows",
                    "--disable-renderer-backgrounding",
                    "--disable-background-timer-throttling",
                    "--disable-features=CalculateNativeWinOcclusion",
                ]
                if self.hidden_window
                else ["--start-maximized"]
            )
            self._context = self._playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=False,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-proxy-server",
                    "--lang=ru-RU",
                    *window_args,
                ],
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1440, "height": 940},
                screen={"width": 1920, "height": 1080},
                java_script_enabled=True,
            )
            self._context.set_default_timeout(self.timeout_ms)
            self._context.set_default_navigation_timeout(self.timeout_ms)
            self.page = (
                self._context.pages[0]
                if self._context.pages
                else self._context.new_page()
            )
            self._started = True
            if self.hidden_window:
                logger.info(
                    "Chromium запущен скрыто за пределами экрана; при проверке "
                    "Avito окно будет показано"
                )
            else:
                logger.info("Chromium запущен в видимом режиме (headless=False)")
            logger.info("Профиль браузера: %s", profile_dir)
        except Exception as exc:
            self._safe_close()
            message = str(exc)
            if "Executable doesn't exist" in message or "playwright install" in message:
                raise ScraperUnavailableError(
                    "Браузер Chromium не установлен. Запустите install_browser.bat — "
                    "он установит Chromium в папку проекта."
                ) from exc
            raise ScraperUnavailableError(f"Не удалось запустить Chromium: {message}") from exc

    def _safe_close(self) -> None:
        for attr in ("_context", "_browser"):
            try:
                target = getattr(self, attr)
                if target:
                    target.close()
            except Exception:
                pass
            setattr(self, attr, None)
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._playwright = None
        self.page = None
        self._started = False

    def close(self) -> None:
        if self._started or self._playwright:
            logger.info("Закрываю Chromium")
        self._safe_close()

    def __enter__(self) -> "AvitoScraper":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ------------------------------- helpers -------------------------------

    def _cancelled(self) -> bool:
        return self.stop_event is not None and self.stop_event.is_set()

    def _check_cancel(self) -> None:
        if self._cancelled():
            raise ScraperCancelled("Сбор остановлен пользователем")

    def _window_bounds(self) -> dict[str, Any] | None:
        if self.page is None or self._context is None:
            return None
        session = None
        try:
            session = self._context.new_cdp_session(self.page)
            info = session.send("Browser.getWindowForTarget")
            result = session.send(
                "Browser.getWindowBounds", {"windowId": info["windowId"]}
            )
            return result.get("bounds")
        except Exception as exc:
            logger.debug("Не удалось прочитать положение окна Chromium: %s", exc)
            return None
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass

    def _set_window_bounds(self, left: int, top: int) -> bool:
        if self.page is None or self._context is None:
            return False
        session = None
        try:
            session = self._context.new_cdp_session(self.page)
            info = session.send("Browser.getWindowForTarget")
            window_id = info["windowId"]
            session.send(
                "Browser.setWindowBounds",
                {"windowId": window_id, "bounds": {"windowState": "normal"}},
            )
            session.send(
                "Browser.setWindowBounds",
                {
                    "windowId": window_id,
                    "bounds": {
                        "left": int(left),
                        "top": int(top),
                        "width": self.window_width,
                        "height": self.window_height,
                        "windowState": "normal",
                    },
                },
            )
            return True
        except Exception as exc:
            logger.warning("Не удалось изменить положение окна Chromium: %s", exc)
            return False
        finally:
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    pass

    def _notify_visibility(self, visible: bool) -> None:
        if self.on_browser_visibility is None:
            return
        try:
            self.on_browser_visibility(bool(visible))
        except Exception as exc:
            logger.debug("Не удалось обновить состояние кнопки браузера: %s", exc)

    def _is_window_visible(self) -> bool:
        if not self.hidden_window:
            return True
        bounds = self._window_bounds()
        if not bounds:
            return self._manual_visible
        return (
            int(bounds.get("left", 0)) > -10000
            and str(bounds.get("windowState", "normal")) != "minimized"
        )

    def request_toggle(self) -> None:
        """Потокобезопасно просит показать/скрыть окно из GUI."""
        self._toggle_requested.set()

    def _process_toggle_request(self) -> None:
        if not self._toggle_requested.is_set():
            return
        self._toggle_requested.clear()
        self.toggle_browser()

    def show_browser(self, manual: bool = False) -> None:
        """Показывает скрытый Chromium, когда нужна ручная проверка Avito."""
        if not self.hidden_window:
            return
        if manual:
            self._manual_visible = True
        if not self._set_window_bounds(80, 80):
            return
        try:
            if self.page is not None:
                self.page.bring_to_front()
        except Exception as exc:
            logger.debug("Не удалось вывести Chromium на передний план: %s", exc)
        self._notify_visibility(True)
        logger.warning(
            "Chromium показан на экране — пройдите проверку Avito, затем "
            "приложение продолжит работу автоматически."
        )

    def hide_browser(self, manual: bool = False) -> None:
        """Убирает Chromium за пределы экрана после проверки."""
        if not self.hidden_window:
            return
        if manual:
            self._manual_visible = False
        if not self._set_window_bounds(-32000, -32000):
            return
        self._notify_visibility(False)
        logger.info("Chromium снова скрыт за пределами экрана")

    def toggle_browser(self) -> None:
        """Показывает или скрывает окно по повторному нажатию кнопки."""
        if not self.hidden_window:
            return
        if self._is_window_visible():
            self.hide_browser(manual=True)
        else:
            self._manual_visible = True
            self.show_browser(manual=True)

    def _pause(self, reason: str = "") -> None:
        """Пауза 5–10 секунд; «Стоп» прерывает её не более чем за 0.2 секунды."""
        self._check_cancel()
        seconds = random.uniform(self.pause_min, self.pause_max)
        logger.debug("Пауза %.1f с (%s)", seconds, reason or "между действиями")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_cancel()
            self._process_toggle_request()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _pause_date(self, reason: str = "") -> None:
        """Короткая пауза для последовательного открытия карточек с датами."""
        self._check_cancel()
        seconds = random.uniform(self.date_pause_min, self.date_pause_max)
        logger.debug("Пауза %.1f с (%s)", seconds, reason or "уточнение даты")
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self._check_cancel()
            self._process_toggle_request()
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    def _body_text(self) -> tuple[str, str]:
        assert self.page is not None
        try:
            title = self.page.title()
        except Exception:
            title = ""
        try:
            body = self.page.locator("body").inner_text(timeout=5_000)
        except Exception:
            body = ""
        return title, body[:15_000]

    def _captcha_visible(self) -> bool:
        """Ищет видимый виджет GeeTest/капчи, даже если текст не распознан."""
        if self.page is None:
            return False
        for selector in CAPTCHA_SELECTORS:
            try:
                locator = self.page.locator(selector)
                for index in range(min(locator.count(), 4)):
                    if locator.nth(index).is_visible():
                        return True
            except Exception:
                continue
        return False

    def _try_auto_ip_check(self, title: str, body: str) -> bool:
        """Один раз нажимает «Пройти проверку» на странице IP-проверки."""
        if not self.auto_ip_check or self.page is None:
            return False
        text = f"{title}\n{body}".casefold()
        if "пройти проверку" not in text:
            return False
        logger.warning("Проверка IP обнаружена, ищу кнопку «Пройти проверку»")
        selectors = (
            "button:has-text('Пройти проверку')",
            "a:has-text('Пройти проверку')",
            "[role=button]:has-text('Пройти проверку')",
        )
        button = None
        for selector in selectors:
            try:
                locator = self.page.locator(selector)
                for index in range(min(locator.count(), 4)):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        button = candidate
                        break
            except Exception:
                continue
            if button is not None:
                break
        clicked_by_js = False
        if button is None:
            try:
                clicked_by_js = bool(
                    self.page.evaluate(
                        """
                        () => {
                          const needle = 'пройти проверку';
                          const all = Array.from(document.querySelectorAll(
                            'button, a, [role="button"], input[type="button"], input[type="submit"]'
                          ));
                          const target = all.find((node) => {
                            const text = (node.innerText || node.value || '').toLowerCase();
                            return text.includes(needle) && node.offsetParent !== null;
                          });
                          if (!target) return false;
                          target.click();
                          return true;
                        }
                        """
                    )
                )
            except Exception as exc:
                logger.debug("JS-поиск кнопки проверки IP: %s", exc)
        if button is None and not clicked_by_js:
            logger.warning(
                "Кнопка «Пройти проверку» не найдена на странице (%s)",
                (title or body)[:120].replace("\n", " "),
            )
            return False
        try:
            logger.warning(
                "Avito показал проверку IP — нажимаю «Пройти проверку» автоматически"
            )
            if button is not None:
                button.click(timeout=5_000)
            self.page.wait_for_timeout(1_500)
            deadline = time.monotonic() + self.ip_check_wait_seconds
            while time.monotonic() < deadline:
                self._check_cancel()
                if self.page is None or self.page.is_closed():
                    return True
                title, body = self._body_text()
                if not is_captcha_text(title, body) and not self._captcha_visible():
                    try:
                        if self.page.locator(CARD_SELECTOR).count() > 0:
                            logger.info("Проверка IP пройдена, продолжаю сбор")
                            return True
                    except Exception:
                        pass
                self.page.wait_for_timeout(1_500)
            logger.warning(
                "Автоматическая проверка IP не завершилась за %s секунд — "
                "показываю браузер для ручного прохождения",
                self.ip_check_wait_seconds,
            )
            return True
        except ScraperCancelled:
            raise
        except Exception as exc:
            logger.warning("Не удалось нажать «Пройти проверку»: %s", exc)
            return False

    def _raise_if_blocked(self, response_status: int | None = None) -> None:
        assert self.page is not None
        status_block = response_status in {403, 429, 503}
        try:
            title, body = self._body_text()
        except Exception as exc:
            logger.debug("Не удалось прочитать страницу для проверки блокировки: %s", exc)
            if status_block:
                self._save_debug_screenshot("blocked")
                raise AvitoBlockedError(
                    f"Avito ограничил доступ (HTTP {response_status})"
                )
            return

        if is_captcha_text(title, body) or self._captcha_visible():
            self._save_debug_screenshot("captcha")
            logger.warning(
                "Avito показал проверку/капчу: %s",
                (title or body)[:160].replace("\n", " "),
            )
            if self._try_auto_ip_check(title, body):
                try:
                    title, body = self._body_text()
                    if not is_captcha_text(title, body) and not self._captcha_visible():
                        return
                except Exception as exc:
                    logger.debug("Проверка после автоклика IP: %s", exc)
            auto_shown = False
            if self.show_on_captcha and not self._is_window_visible():
                # Окно было скрыто — показываем автоматически и после проверки
                # скрываем, если пользователь не взял управление кнопкой.
                self._manual_visible = False
                self.show_browser(manual=False)
                auto_shown = True
            try:
                self._wait_for_manual_avito_check()
            finally:
                if auto_shown and not self._manual_visible:
                    self.hide_browser(manual=False)
            return

        if status_block:
            self._save_debug_screenshot("blocked")
            raise AvitoBlockedError(
                f"Avito ограничил доступ (HTTP {response_status})"
            )

    def _wait_for_manual_avito_check(self) -> None:
        """Ждёт ручную проверку Avito, не закрывая браузер."""
        assert self.page is not None
        timeout = self.captcha_wait_seconds
        unlimited = timeout <= 0
        if unlimited:
            logger.warning(
                "Avito требует проверку браузера. Пройдите её в показанном окне "
                "Chromium. Ожидание без ограничения по времени; для выхода "
                "нажмите «Стоп»."
            )
        else:
            logger.warning(
                "Avito требует проверку браузера. Пройдите её в показанном окне "
                "Chromium в течение %s секунд. Не закрывайте окно.",
                timeout,
            )
        deadline = None if unlimited else time.monotonic() + timeout
        last_report = 0.0
        while True:
            self._check_cancel()
            self._process_toggle_request()
            if self.page is None or self.page.is_closed():
                raise AvitoBlockedError(
                    "Окно Chromium было закрыто во время проверки Avito"
                )
            if deadline is not None and time.monotonic() >= deadline:
                break
            time.sleep(1.5)
            try:
                title, body = self._body_text()
                captcha_present = is_captcha_text(title, body) or self._captcha_visible()
                cards = 0
                if not captcha_present:
                    cards = self.page.locator(CARD_SELECTOR).count()
                    if cards > 0 or is_no_results_text(body):
                        logger.info(
                            "Проверка Avito пройдена, продолжаю сбор (карточек: %s)",
                            cards,
                        )
                        return
            except ScraperCancelled:
                raise
            except Exception as exc:
                logger.debug("Ожидание ручной проверки Avito: %s", exc)

            if unlimited:
                now = time.monotonic()
                if now - last_report >= 15:
                    logger.warning(
                        "Ожидаю прохождение проверки Avito… Браузер остаётся "
                        "открытым. Для выхода нажмите «Стоп»."
                    )
                    last_report = now
            else:
                remaining = int((deadline or 0) - time.monotonic())
                if remaining > 0 and (
                    last_report - remaining >= 15 or last_report == 0
                ):
                    logger.warning(
                        "Ожидаю прохождение проверки Avito… осталось примерно %s сек.",
                        remaining,
                    )
                    last_report = remaining

        self._save_debug_screenshot("captcha_timeout")
        raise AvitoBlockedError(
            f"Проверка Avito не пройдена за {timeout} секунд — цикл пропущен"
        )

    @staticmethod
    def _json_iva_payloads(item: dict[str, Any], step: str) -> list[dict[str, Any]]:
        steps = (item.get("iva") or {}).get(step) or []
        return [
            entry.get("payload") or {}
            for entry in steps
            if isinstance(entry, dict)
        ]

    def _json_items_to_cards(
        self, payload: dict[str, Any] | None, limit: int
    ) -> list[dict[str, Any]]:
        """Преобразует item-ответ Avito в тот же формат, что DOM-карточки."""
        if not isinstance(payload, dict):
            return []
        catalog = payload.get("catalog") if isinstance(payload.get("catalog"), dict) else {}
        items = catalog.get("items") if isinstance(catalog.get("items"), list) else []
        cards: list[dict[str, Any]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            item_id = item.get("id")
            url_path = str(item.get("urlPath") or "")
            if item_id is None or not url_path:
                continue
            title = str(item.get("title") or "").strip()
            price_value = (item.get("priceDetailed") or {}).get("value")
            price = price_value if isinstance(price_value, (int, float)) else ""
            published = ""
            for meta in self._json_iva_payloads(item, "DateInfoStep"):
                candidate = meta.get("relative") or meta.get("absolute")
                if candidate:
                    published = str(candidate)
                    break
            badges = [
                str(badge.get("title") or "")
                for meta in self._json_iva_payloads(item, "BadgeBarStep")
                for badge in (meta.get("badges") or [])
                if isinstance(badge, dict) and badge.get("title")
            ]
            badge_text = " ".join(badges).casefold()
            if "доставка" in badge_text:
                delivery = "yes"
            elif "самовывоз" in badge_text:
                delivery = "no"
            else:
                delivery = "unknown"
            geo = item.get("geo") or {}
            city = str(geo.get("formattedAddress") or "")
            cards.append(
                {
                    "id": str(item_id),
                    "title": title,
                    "price": str(price),
                    "published_at": published,
                    "city": city,
                    "delivery": delivery,
                    "url": self.base_url + "/" + url_path.lstrip("/"),
                }
            )
            if len(cards) >= limit:
                break
        return cards

    def _read_detail_published_at(self) -> str | None:
        """Пытается прочитать точную дату с открытой карточки объявления."""
        assert self.page is not None
        try:
            locator = self.page.locator(DETAIL_DATE_SELECTOR)
            for index in range(min(locator.count(), 4)):
                node = locator.nth(index)
                candidates = [
                    node.get_attribute("datetime"),
                    node.inner_text(timeout=500),
                ]
                for candidate in candidates:
                    parsed = parse_published_at(candidate, now=now_moscow())
                    if parsed:
                        return parsed
        except Exception as exc:
            logger.debug("Не удалось прочитать дату по селекторам: %s", exc)
        try:
            _title, body = self._body_text()
            match = re.search(
                r"(?:размещено|опубликовано|дата публикации)[^\n]{0,100}",
                body,
                flags=re.IGNORECASE,
            )
            if match:
                return parse_published_at(match.group(0), now=now_moscow())
        except Exception as exc:
            logger.debug("Не удалось прочитать дату из текста карточки: %s", exc)
        return None

    def enrich_listing_dates(self, listings: list[dict[str, Any]]) -> None:
        """Открывает карточки без даты и уточняет дату публикации."""
        if not self.enrich_missing_dates:
            return
        targets = [
            listing
            for listing in listings
            if listing.get("url")
            and (
                not listing.get("published_at")
                or "T00:00:00" in str(listing.get("published_at"))
            )
        ]
        if not targets:
            return
        assert self.page is not None
        logger.info("Уточняю дату публикации в %s карточках", len(targets))
        for listing in targets:
            self._check_cancel()
            self._pause_date("перед открытием карточки для даты")
            try:
                self.page.goto(
                    str(listing["url"]),
                    wait_until="domcontentloaded",
                    timeout=self.timeout_ms,
                )
                self._raise_if_blocked()
                try:
                    self.page.wait_for_selector(
                        DETAIL_DATE_SELECTOR, timeout=3_500
                    )
                except Exception:
                    pass
                published = self._read_detail_published_at()
                if published:
                    listing["published_at"] = published
                    logger.info(
                        "Дата публикации уточнена: %s (%s)",
                        listing.get("title", "")[:55],
                        published,
                    )
                else:
                    logger.info(
                        "Дата публикации не найдена в карточке: %s",
                        listing.get("title", "")[:70],
                    )
            except ScraperCancelled:
                raise
            except AvitoBlockedError as exc:
                logger.warning(
                    "Не удалось уточнить даты: Avito заблокировал доступ (%s)",
                    exc,
                )
                break
            except Exception as exc:
                logger.warning(
                    "Не удалось открыть карточку для даты %s: %s",
                    listing.get("url"),
                    exc,
                )

    def _save_debug_screenshot(self, prefix: str) -> None:
        if self.page is None:
            return
        try:
            folder = APP_DIR / "logs" / "screenshots"
            folder.mkdir(parents=True, exist_ok=True)
            filename = folder / f"{prefix}_{now_moscow().strftime('%Y%m%d_%H%M%S')}.png"
            self.page.screenshot(path=str(filename), full_page=False)
            logger.info("Скриншот для диагностики: %s", filename)
        except Exception as exc:
            logger.debug("Не удалось сохранить скриншот: %s", exc)

    # ------------------------------- scraping ------------------------------

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
        """Открывает страницы поиска и возвращает объявления с каждой."""
        if not (query or "").strip() and not direct_url:
            raise ScraperError("Пустой поисковый запрос")
        pages = max(1, min(30, int(pages or 1)))
        if pages > 1:
            base_url = direct_url or build_search_url(
                self.base_url,
                query,
                city,
                price_min,
                price_max,
                local_priority=local_priority,
                avito_sort=avito_sort,
                category=category,
            )
            aggregate: list[dict[str, Any]] = []
            seen: set[str] = set()
            for page in range(1, pages + 1):
                self._check_cancel()
                page_items = self.search(
                    query,
                    city=city,
                    price_min=price_min,
                    price_max=price_max,
                    limit=limit,
                    local_priority=local_priority,
                    direct_url=with_page(base_url, page),
                    pages=1,
                    avito_sort=avito_sort,
                    category=category,
                )
                logger.info(
                    "Страница %s/%s: получено %s объявлений",
                    page,
                    pages,
                    len(page_items),
                )
                for listing in page_items:
                    if listing["id"] in seen:
                        continue
                    seen.add(listing["id"])
                    aggregate.append(listing)
                if not page_items:
                    break
            return aggregate
        self.start()
        self._check_cancel()
        # Пауза перед каждым новым переходом, в том числе между запросами.
        self._pause("перед открытием поиска")
        limit = max(1, int(limit or self.default_limit))
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
        assert self.page is not None
        logger.info("Открываю Avito: запрос «%s», город «%s»", query.strip(), city or "не задан")
        logger.debug("URL поиска: %s", url)

        items_payload: dict[str, Any] | None = None

        def capture_items(response: Any) -> None:
            nonlocal items_payload
            if ITEMS_RESPONSE_MARKER not in response.url:
                return
            try:
                data = response.json()
            except Exception:
                return
            if not isinstance(data, dict):
                return
            catalog = data.get("catalog")
            if isinstance(catalog, dict) and isinstance(catalog.get("items"), list):
                items_payload = data

        self.page.on("response", capture_items)
        response_status: int | None = None
        try:
            response = self.page.goto(
                url, wait_until="domcontentloaded", timeout=self.timeout_ms
            )
            if response is not None:
                response_status = response.status
        except ScraperCancelled:
            raise
        except Exception as exc:
            try:
                self._save_debug_screenshot("page_error")
            finally:
                raise ScraperError(f"Не удалось открыть страницу поиска: {exc}") from exc

        try:
            # Сначала даём странице 5–10 секунд: автоматическая проверка Avito
            # иногда завершается сама и не должна считаться блокировкой.
            self._pause("после открытия страницы")
            self._raise_if_blocked(response_status)

            if items_payload is None:
                self.page.wait_for_timeout(700)
            json_cards = self._json_items_to_cards(items_payload, limit)
            try:
                self.page.remove_listener("response", capture_items)
            except Exception:
                pass

            if json_cards:
                raw_cards = json_cards
                logger.info(
                    "Из JSON-выдачи Avito получено карточек: %s", len(raw_cards)
                )
            else:
                if items_payload is None:
                    logger.info(
                        "JSON-выдача Avito не перехвачена; использую DOM-разбор"
                    )
                else:
                    logger.info(
                        "JSON-выдача Avito пустая или без items; использую DOM-разбор"
                    )
                try:
                    self.page.wait_for_selector(CARD_SELECTOR, timeout=8_000)
                except Exception:
                    # Нет карточек — это может быть «ничего не найдено» или смена
                    # вёрстки. Ниже отличаем одно от другого.
                    pass

                count = 0
                try:
                    count = self.page.locator(CARD_SELECTOR).count()
                except Exception:
                    count = 0

                for _ in range(self.scroll_attempts):
                    if count >= limit:
                        break
                    self._check_cancel()
                    try:
                        self.page.mouse.wheel(0, random.randint(800, 1400))
                        self._pause("после прокрутки")
                        count = self.page.locator(CARD_SELECTOR).count()
                    except ScraperCancelled:
                        raise
                    except Exception as exc:
                        logger.warning("Прокрутка прервалась: %s", exc)
                        break

                self._raise_if_blocked()
                raw_cards = self.page.evaluate(_EXTRACT_JS) or []
                logger.info("На странице найдено карточек: %s", len(raw_cards))

            title, body = self._body_text()
            if not raw_cards:
                if is_no_results_text(body):
                    logger.info("По запросу «%s» ничего не найдено", query.strip())
                    return []
                self._save_debug_screenshot("no_cards")
                logger.warning(
                    "Карточки не найдены, и это не похоже на пустую выдачу. "
                    "Возможно, Avito изменил вёрстку. Скриншот сохранён в logs/screenshots."
                )
                return []

            normalized: list[dict[str, Any]] = []
            seen: set[str] = set()
            for card in raw_cards:
                if not isinstance(card, dict):
                    continue
                try:
                    listing = normalize_listing(card, base_url=self.base_url)
                except Exception as exc:
                    logger.warning("Пропущена карточка без корректного id: %s", exc)
                    continue
                if listing["id"] in seen:
                    continue
                seen.add(listing["id"])
                normalized.append(listing)
                if len(normalized) >= limit:
                    break
            logger.info("Извлечено объявлений: %s (лимит %s)", len(normalized), limit)
            return normalized
        except AvitoBlockedError:
            raise
        except ScraperCancelled:
            raise
        except Exception as exc:
            try:
                self._save_debug_screenshot("scrape_error")
            finally:
                raise ScraperError(f"Ошибка во время сбора: {exc}") from exc


def _run_cli() -> int:
    parser = argparse.ArgumentParser(
        description="Тестовый запуск сборщика Avito Radar (Chromium по настройкам окна)."
    )
    parser.add_argument("--query", required=True, help="Поисковый запрос")
    parser.add_argument("--city", default="", help="Город, например Москва")
    parser.add_argument("--limit", type=int, default=5, help="Сколько карточек вывести")
    parser.add_argument("--min-price", type=int, default=None, dest="price_min")
    parser.add_argument("--max-price", type=int, default=None, dest="price_max")
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Печатать словари; сейчас это и так основной вывод",
    )
    parser.add_argument("--config", default=str(APP_DIR / "config.toml"))
    args = parser.parse_args()

    config = load_config(args.config)
    setup_logging(app_path(config, "log_path", "logs/avito_radar.log"))
    try:
        with AvitoScraper(config, config_path=args.config) as scraper:
            listings = scraper.search(
                args.query,
                city=args.city,
                price_min=args.price_min,
                price_max=args.price_max,
                limit=max(1, args.limit),
            )
    except ScraperError as exc:
        logger.error("%s", exc)
        print(f"\nОшибка: {exc}", file=sys.stderr)
        return 2

    print(f"\nПолучено объявлений: {len(listings)}")
    for index, listing in enumerate(listings, start=1):
        print(f"\n--- Карточка {index} ---")
        print(json.dumps(listing, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli())
