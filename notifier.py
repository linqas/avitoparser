"""Telegram-уведомления Avito Radar.

Токен читается из config.toml при каждой отправке, поэтому его можно заменить
в GUI без перезапуска приложения. В логи и исключения токен не попадает.
"""

from __future__ import annotations

import html
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests

from utils import (
    DEFAULT_CONFIG_PATH,
    format_datetime,
    format_delivery,
    format_location,
    format_price,
    format_price_short,
    get_telegram_token,
    load_config,
    redact_secrets,
    truncate_message,
)

logger = logging.getLogger("avito_radar.notifier")


class NotifierError(RuntimeError):
    pass


class TelegramConfigError(NotifierError):
    pass


class TelegramAuthError(NotifierError):
    pass


class TelegramRecipientError(NotifierError):
    pass


@dataclass(slots=True)
class SendReport:
    sent: int = 0
    failed: list[tuple[int, str]] = field(default_factory=list)

    @property
    def attempted(self) -> int:
        return self.sent + len(self.failed)


def _join_lines(lines: Iterable[str]) -> str:
    return "\n".join(line for line in lines if line)


def _digest_title(listing: dict[str, Any]) -> str:
    title = html.escape(str(listing.get("title") or "Без названия"), quote=False)
    url = str(listing.get("url") or "")
    if url.startswith(("http://", "https://")):
        return f'<a href="{html.escape(url, quote=True)}">{title}</a>'
    return title


def _digest_kind_item(item: dict[str, Any]) -> str:
    kind = str(item.get("kind") or "new")
    listing = item.get("listing") or {}
    title = _digest_title(listing)
    price = html.escape(format_price_short(listing.get("price")), quote=False)
    location = html.escape(format_location(listing), quote=False)
    if kind == "price":
        old_price = format_price_short(item.get("old_price"))
        return f"💰 {title} — {old_price} → {price} — {location}"
    if kind == "missing":
        return f"⚠️ {title} — снято с публикации — {location}"
    published = format_datetime(listing.get("published_at"))
    market_diff = listing.get("market_diff")
    market = (
        f" · от рынка {market_diff:+.0%}"
        if isinstance(market_diff, (int, float))
        else ""
    )
    return f"🆕 {title} — {price} — {location} — {published}{market}"


def digest_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        item
        for key in ("new", "price", "missing")
        for item in (payload.get(key) or [])
        if isinstance(item, dict)
    ]


def digest_total_pages(payload: dict[str, Any], per_page: int = 10) -> int:
    return max(1, (len(digest_items(payload)) + per_page - 1) // per_page)


def digest_summary_text(payload: dict[str, Any]) -> str:
    new_count = len(payload.get("new") or [])
    price_count = len(payload.get("price") or [])
    missing_count = len(payload.get("missing") or [])
    query = html.escape(str(payload.get("query") or "—"), quote=False)
    city = html.escape(str(payload.get("city") or "Вся Россия"), quote=False)
    return _join_lines(
        [
            "🔔 <b>Avito Radar</b>",
            "",
            f"🆕 Новых: <b>{new_count}</b>",
            f"💰 Изменения цены: <b>{price_count}</b>",
            f"⚠️ Снято: <b>{missing_count}</b>",
            "",
            f"Поиск: «{query}»",
            f"Город: {city}",
            "",
            "Откройте список кнопкой ниже. Данные доступны 7 дней.",
        ]
    )


def sorted_digest_items(
    payload: dict[str, Any], sort_key: str = "date", *, local_first: bool = False
) -> list[dict[str, Any]]:
    items = digest_items(payload)
    if sort_key in {"price", "price_asc"}:
        items.sort(
            key=lambda item: (
                (item.get("listing") or {}).get("price") is None,
                (item.get("listing") or {}).get("price") or 0,
            )
        )
    elif sort_key == "price_desc":
        items.sort(
            key=lambda item: (
                (item.get("listing") or {}).get("price") is None,
                -float((item.get("listing") or {}).get("price") or 0),
            )
        )
    elif sort_key == "market_diff":
        items.sort(
            key=lambda item: (
                not isinstance(
                    (item.get("listing") or {}).get("market_diff"), (int, float)
                ),
                float((item.get("listing") or {}).get("market_diff") or 0),
            ),
            reverse=True,
        )
    else:
        items.sort(
            key=lambda item: str(
                (item.get("listing") or {}).get("published_at") or ""
            ),
            reverse=True,
        )
    city = str(payload.get("city") or "").strip().casefold()
    if local_first and city and city != "вся россия":
        items.sort(
            key=lambda item: 0
            if city in str((item.get("listing") or {}).get("city") or "").casefold()
            else 1
        )
    return items


def digest_page_text(
    payload: dict[str, Any],
    page: int = 0,
    per_page: int = 10,
    sort_key: str = "date",
    *,
    local_first: bool = False,
) -> str:
    items = sorted_digest_items(payload, sort_key, local_first=local_first)
    total_pages = digest_total_pages(payload, per_page)
    page = max(0, min(int(page), total_pages - 1))
    start = page * per_page
    selected = items[start : start + per_page]
    query = html.escape(str(payload.get("query") or "—"), quote=False)
    lines = [f"📋 <b>{query}</b> · страница {page + 1}/{total_pages}", ""]
    current_kind = ""
    for item in selected:
        kind = str(item.get("kind") or "new")
        if kind != current_kind:
            label = {
                "new": "🆕 Новые",
                "price": "💰 Изменение цены",
                "missing": "⚠️ Снятые",
            }.get(kind, "Объявления")
            lines.extend([label, ""])
            current_kind = kind
        lines.append(_digest_kind_item(item))
        lines.append("")
    if not selected:
        lines.append("Нет объявлений.")
    return _join_lines(lines)


def digest_summary_keyboard(digest_id: str, total: int) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": f"Показать ({total})", "callback_data": f"d:{digest_id}:0"},
                {"text": "Скачать XLSX", "callback_data": f"x:{digest_id}"},
            ]
        ]
    }


def digest_page_keyboard(
    digest_id: str,
    page: int,
    total_pages: int,
    sort_key: str = "date",
    *,
    local_first: bool = False,
    city: str = "",
) -> dict[str, Any]:
    flag = "1" if local_first else "0"
    nav: list[dict[str, str]] = []
    if page > 0:
        nav.append(
            {"text": "◀ Назад", "callback_data": f"d:{digest_id}:{page - 1}:{flag}"}
        )
    nav.append({"text": f"{page + 1}/{total_pages}", "callback_data": f"n:{digest_id}"})
    if page + 1 < total_pages:
        nav.append(
            {"text": "Ещё ▶", "callback_data": f"d:{digest_id}:{page + 1}:{flag}"}
        )
    labels = {
        "date": "Дата",
        "price_asc": "Дешевле",
        "price_desc": "Дороже",
        "market_diff": "По размеру скидки",
    }
    sort_row = [
        {
            "text": ("✓ " if value == sort_key else "") + label,
            "callback_data": f"s:{digest_id}:{value}:{flag}",
        }
        for value, label in labels.items()
    ]
    rows = [sort_row]
    if city.strip() and city.strip().casefold() != "вся россия":
        rows.append(
            [
                {
                    "text": ("✓ " if local_first else "") + "Мой город",
                    "callback_data": f"l:{digest_id}:{1 if not local_first else 0}",
                }
            ]
        )
    rows.extend(
        [
            nav,
            [
                {"text": "Скачать XLSX", "callback_data": f"x:{digest_id}"},
                {"text": "Закрыть", "callback_data": f"c:{digest_id}"},
            ],
        ]
    )
    return {"inline_keyboard": rows}


def listing_caption(listing: dict[str, Any]) -> str:
    lines = [
        str(listing.get("title") or "Без названия"),
        f"Цена: {format_price(listing.get('price'))}",
        f"Город: {format_location(listing)}",
        f"Авито Доставка: {format_delivery(listing.get('delivery') or 'unknown')}",
        f"Опубликовано: {format_datetime(listing.get('published_at'))}",
    ]
    if listing.get("views") is not None:
        lines.append(f"Просмотры: {listing['views']}")
    if listing.get("url"):
        lines.append(f"Ссылка: {listing['url']}")
    return _join_lines(lines)


class TelegramNotifier:
    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        config: dict[str, Any] | None = None,
    ):
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self._static_config = config
        self._session = requests.Session()
        # Проект работает напрямую, без прокси и переменных HTTP(S)_PROXY.
        self._session.trust_env = False

    def _config(self) -> dict[str, Any]:
        if self._static_config is not None:
            return self._static_config
        return load_config(self.config_path)

    def _settings(self) -> tuple[str, str, float]:
        config = self._config()
        token = get_telegram_token(config)
        telegram = config.get("telegram", {})
        api_base = str(telegram.get("api_base", "https://api.telegram.org")).rstrip("/")
        timeout = float(telegram.get("request_timeout_seconds", 15))
        proxy = str(telegram.get("proxy_url", "") or "").strip()
        self._session.proxies = (
            {"http": proxy, "https": proxy} if proxy else {}
        )
        return token, api_base, timeout

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        """Повторяет сетевой запрос и переподключается после обрыва связи."""
        last_error: requests.RequestException | None = None
        for attempt in range(3):
            try:
                return self._session.request(method, url, **kwargs)
            except requests.RequestException as exc:
                last_error = exc
                logger.warning(
                    "Telegram: сетевая ошибка, попытка %s/3: %s",
                    attempt + 1,
                    redact_secrets(exc),
                )
                if attempt < 2:
                    time.sleep(2 * (attempt + 1))
                    try:
                        self._session.close()
                    except Exception:
                        pass
                    self._session = requests.Session()
                    self._session.trust_env = False
        raise NotifierError(
            f"Нет связи с Telegram API: {redact_secrets(last_error)}"
        ) from last_error

    def validate_token(self) -> str:
        """Возвращает username бота или выбрасывает русскоязычную ошибку."""
        token, api_base, timeout = self._settings()
        if not token:
            raise TelegramConfigError(
                "Токен бота не задан. Впишите его в config.toml или в поле "
                "«Токен бота» и нажмите «Сохранить токен»."
            )
        url = f"{api_base}/bot{token}/getMe"
        try:
            response = self._request("GET", url, timeout=timeout)
        except requests.RequestException as exc:
            raise NotifierError(
                f"Нет связи с Telegram API: {redact_secrets(exc)}"
            ) from exc
        payload = self._payload(response)
        if not payload.get("ok"):
            raise TelegramAuthError(_describe_api_error(payload, invalid_auth=True))
        return str(payload.get("result", {}).get("username") or "bot")

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> int:
        token, api_base, timeout = self._settings()
        if not token:
            raise TelegramConfigError(
                "Токен бота не задан в [telegram].bot_token файла config.toml"
            )
        try:
            chat_id = int(chat_id)
        except (TypeError, ValueError) as exc:
            raise TelegramRecipientError(f"Некорректный chat_id: {chat_id!r}") from exc
        if not (text or "").strip():
            raise NotifierError("Пустой текст уведомления")
        url = f"{api_base}/bot{token}/sendMessage"
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": truncate_message(text),
            "disable_web_page_preview": True,
        }
        if disable_notification:
            payload["disable_notification"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            response = self._request(
                "POST",
                url,
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise NotifierError(
                f"Не удалось отправить сообщение в Telegram: {redact_secrets(exc)}"
            ) from exc
        payload = self._payload(response)
        if not payload.get("ok"):
            description = str(payload.get("description") or "").lower()
            if response.status_code in {401, 404}:
                raise TelegramAuthError(
                    "Telegram отклонил токен бота. Проверьте [telegram].bot_token."
                )
            if "chat not found" in description or "blocked by the user" in description:
                raise TelegramRecipientError(
                    f"Получатель {chat_id} недоступен. Возможно, бот заблокирован."
                )
            raise NotifierError(_describe_api_error(payload))
        result = payload.get("result", {})
        message_id = result.get("message_id") if isinstance(result, dict) else None
        return int(message_id or 0)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        parse_mode: str | None = None,
    ) -> bool:
        token, api_base, timeout = self._settings()
        payload: dict[str, Any] = {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "text": truncate_message(text),
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            response = self._request(
                "POST",
                f"{api_base}/bot{token}/editMessageText",
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            raise NotifierError(
                f"Не удалось обновить сообщение Telegram: {redact_secrets(exc)}"
            ) from exc
        data = self._payload(response)
        if not data.get("ok"):
            raise NotifierError(_describe_api_error(data))
        return True

    def answer_callback_query(self, callback_id: str, text: str = "") -> bool:
        token, api_base, timeout = self._settings()
        payload: dict[str, Any] = {"callback_query_id": callback_id}
        if text:
            payload["text"] = text
        try:
            response = self._request(
                "POST",
                f"{api_base}/bot{token}/answerCallbackQuery",
                json=payload,
                timeout=timeout,
            )
        except requests.RequestException as exc:
            logger.debug("Не удалось ответить на callback: %s", redact_secrets(exc))
            return False
        data = self._payload(response)
        if not data.get("ok"):
            logger.debug("Telegram callback error: %s", data.get("description"))
            return False
        return True

    def send_document(
        self, chat_id: int, path: str | Path, caption: str = ""
    ) -> bool:
        token, api_base, timeout = self._settings()
        file_path = Path(path)
        content = file_path.read_bytes()
        response = self._request(
            "POST",
            f"{api_base}/bot{token}/sendDocument",
            data={
                "chat_id": int(chat_id),
                "caption": truncate_message(caption),
            },
            files={
                "document": (
                    file_path.name,
                    content,
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
            timeout=timeout,
        )
        data = self._payload(response)
        if not data.get("ok"):
            raise NotifierError(_describe_api_error(data))
        return True

    @staticmethod
    def _payload(response: requests.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError:
            return {"ok": False, "description": f"HTTP {response.status_code}: некорректный ответ"}
        if not isinstance(payload, dict):
            return {"ok": False, "description": "Некорректный ответ Telegram API"}
        return payload

    def send_many(
        self, chat_ids: Iterable[int], text: str, *, context: str = ""
    ) -> SendReport:
        report = SendReport()
        unique_ids: list[int] = []
        seen: set[int] = set()
        for raw in chat_ids:
            try:
                chat_id = int(raw)
            except (TypeError, ValueError):
                logger.warning("Пропущен некорректный chat_id: %r", raw)
                continue
            if chat_id not in seen:
                seen.add(chat_id)
                unique_ids.append(chat_id)
        if not unique_ids:
            logger.warning(
                "Получатели не выбраны — уведомление%s не отправлено",
                f" ({context})" if context else "",
            )
            return report

        for chat_id in unique_ids:
            try:
                self.send_message(chat_id, text)
                report.sent += 1
            except TelegramAuthError as exc:
                logger.error("Ошибка Telegram: %s", exc)
                report.failed.append((chat_id, str(exc)))
                # Остальные отправки с тем же токеном бессмысленны.
                break
            except NotifierError as exc:
                logger.error("Не доставлено получателю %s: %s", chat_id, exc)
                report.failed.append((chat_id, str(exc)))
            except Exception as exc:  # непредвиденная ошибка не должна рушить цикл
                logger.exception("Неожиданная ошибка отправки в Telegram")
                report.failed.append((chat_id, str(exc)))
        logger.info(
            "Telegram%s: отправлено %s, ошибок %s",
            f" ({context})" if context else "",
            report.sent,
            len(report.failed),
        )
        return report

    # --------------------------- message builders ---------------------------

    @staticmethod
    def new_listing_text(listing: dict[str, Any]) -> str:
        return _join_lines(["🆕 Новое объявление", "", listing_caption(listing)])

    @staticmethod
    def price_changed_text(listing: dict[str, Any], old_price: int | None) -> str:
        return _join_lines(
            [
                "💸 Изменение цены",
                "",
                str(listing.get("title") or "Без названия"),
                f"Было: {format_price(old_price)}",
                f"Стало: {format_price(listing.get('price'))}",
                f"Город: {format_location(listing)}",
                f"Авито Доставка: {format_delivery(listing.get('delivery') or 'unknown')}",
                f"Ссылка: {listing.get('url') or '—'}",
            ]
        )

    @staticmethod
    def missing_listing_text(listing: dict[str, Any]) -> str:
        return _join_lines(
            [
                "⚠️ Возможно снято с публикации",
                "",
                str(listing.get("title") or "Без названия"),
                f"Последняя цена: {format_price(listing.get('price'))}",
                f"Город: {format_location(listing)}",
                f"Ссылка: {listing.get('url') or '—'}",
                "",
                "Объявление не найдено в текущей выдаче Avito.",
            ]
        )

    @staticmethod
    def blocked_text(query: str, city: str = "") -> str:
        return _join_lines(
            [
                "🚫 Запрос заблокирован Avito",
                "",
                f"Поиск: «{query}»",
                f"Город: {city or 'не задан'}",
                "",
                "Avito показал капчу или ограничил доступ. Этот цикл пропущен, "
                "попробуйте позже и без частых запусков.",
            ]
        )

    @staticmethod
    def error_text(message: str, query: str = "") -> str:
        return _join_lines(
            [
                "⚠️ Ошибка сбора Avito Radar",
                f"Поиск: «{query}»" if query else "",
                f"Причина: {message}",
                "",
                "Уже собранные данные сохранены. Следующий цикл можно запустить вручную.",
            ]
        )

    def notify_digest(
        self,
        chat_ids: Iterable[int],
        digest_id: str,
        payload: dict[str, Any],
    ) -> SendReport:
        report = SendReport()
        unique_ids: list[int] = []
        seen: set[int] = set()
        for raw in chat_ids:
            try:
                chat_id = int(raw)
            except (TypeError, ValueError):
                continue
            if chat_id not in seen:
                seen.add(chat_id)
                unique_ids.append(chat_id)
        if not unique_ids:
            return report
        text = digest_summary_text(payload)
        markup = digest_summary_keyboard(digest_id, len(digest_items(payload)))
        for chat_id in unique_ids:
            try:
                self.send_message(
                    chat_id,
                    text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                report.sent += 1
            except TelegramAuthError as exc:
                report.failed.append((chat_id, str(exc)))
                break
            except NotifierError as exc:
                logger.warning("Ошибка отправки сводки %s: %s", chat_id, exc)
                report.failed.append((chat_id, str(exc)))
        return report

    def notify_new(self, chat_ids: Iterable[int], listing: dict[str, Any]) -> SendReport:
        return self.send_many(chat_ids, self.new_listing_text(listing), context="новое")

    def notify_price_change(
        self, chat_ids: Iterable[int], listing: dict[str, Any], old_price: int | None
    ) -> SendReport:
        return self.send_many(
            chat_ids, self.price_changed_text(listing, old_price), context="цена"
        )

    def notify_missing(self, chat_ids: Iterable[int], listing: dict[str, Any]) -> SendReport:
        return self.send_many(chat_ids, self.missing_listing_text(listing), context="снято")

    def notify_blocked(
        self, chat_ids: Iterable[int], query: str, city: str = ""
    ) -> SendReport:
        return self.send_many(chat_ids, self.blocked_text(query, city), context="блокировка")

    def notify_error(
        self, chat_ids: Iterable[int], message: str, query: str = ""
    ) -> SendReport:
        return self.send_many(chat_ids, self.error_text(message, query), context="ошибка")


def _describe_api_error(payload: dict[str, Any], *, invalid_auth: bool = False) -> str:
    description = redact_secrets(
        payload.get("description") or "неизвестная ошибка Telegram"
    )
    if invalid_auth:
        return "Неверный токен бота. Проверьте [telegram].bot_token в config.toml."
    if "chat not found" in description.lower():
        return "Чат не найден. Получатель должен сначала отправить /start боту."
    return f"Telegram API: {description}"
