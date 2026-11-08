"""Long-polling слушатель Telegram: /start -> подписчик в SQLite.

Поток занимается только сетью Telegram. Скрейпинг Avito остаётся строго
последовательным и выполняется отдельно, без параллельных страниц.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

import requests

from notifier import (
    NotifierError,
    TelegramAuthError,
    TelegramNotifier,
    digest_page_keyboard,
    digest_page_text,
    digest_total_pages,
)
from reporter import export_xlsx
from storage import Storage
from utils import (
    DEFAULT_CONFIG_PATH,
    get_telegram_token,
    load_config,
    redact_secrets,
)

logger = logging.getLogger("avito_radar.bot")


class BotListener(threading.Thread):
    def __init__(
        self,
        storage: Storage,
        config_path: str | Path | None = None,
        *,
        on_subscribers_changed: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_ready: Callable[[str], None] | None = None,
    ):
        super().__init__(name="AvitoRadarBotListener", daemon=True)
        self.storage = storage
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.on_subscribers_changed = on_subscribers_changed
        self.on_error = on_error
        self.on_ready = on_ready
        self._stop_event = threading.Event()
        self._session = requests.Session()
        self._session.trust_env = False  # без прокси и HTTP(S)_PROXY
        self._offset = self._load_offset()

    def _load_offset(self) -> int:
        try:
            return int(self.storage.get_meta("telegram_offset", "0") or "0")
        except (TypeError, ValueError):
            return 0

    def _save_offset(self, offset: int) -> None:
        self._offset = max(0, offset)
        try:
            self.storage.set_meta("telegram_offset", str(self._offset))
        except Exception:
            logger.debug("Не удалось сохранить offset Telegram", exc_info=True)

    def stop(self) -> None:
        self._stop_event.set()

    def _notify_error(self, message: str) -> None:
        logger.error("%s", message)
        if self.on_error:
            try:
                self.on_error(message)
            except Exception:
                logger.debug("Ошибка в GUI-колбэке bot_listener", exc_info=True)

    def _config(self) -> dict[str, Any]:
        return load_config(self.config_path)

    def _telegram_settings(self) -> tuple[str, str, int, int]:
        config = self._config()
        telegram = config.get("telegram", {})
        token = get_telegram_token(config)
        api_base = str(telegram.get("api_base", "https://api.telegram.org")).rstrip("/")
        poll_timeout = max(5, int(telegram.get("poll_timeout_seconds", 25)))
        request_timeout = max(10, int(telegram.get("request_timeout_seconds", 15)))
        proxy = str(telegram.get("proxy_url", "") or "").strip()
        self._session.proxies = (
            {"http": proxy, "https": proxy} if proxy else {}
        )
        return token, api_base, poll_timeout, request_timeout

    def _confirm(self, chat_id: int) -> None:
        try:
            notifier = TelegramNotifier(self.config_path)
            notifier.send_message(
                chat_id,
                "✅ Avito Radar\n\n"
                "Вы подписаны на уведомления. Теперь отметьте @username в окне "
                "приложения на вкладке получателей и нажмите «Старт».\n\n"
                "Отписаться можно, заблокировав бота.",
            )
        except NotifierError as exc:
            logger.warning("Не удалось отправить подтверждение %s: %s", chat_id, exc)
        except Exception:
            logger.exception("Ошибка подтверждения подписки")

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = str(message.get("text") or "").strip()
        if not text.startswith("/"):
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()
        username = chat.get("username") or ""
        first_name = chat.get("first_name") or ""

        if command == "/start":
            subscriber = self.storage.upsert_subscriber(chat_id, username, first_name)
            logger.info(
                "Новый подписчик: %s (chat_id=%s)",
                Storage.subscriber_label(subscriber),
                chat_id,
            )
            if self.on_subscribers_changed:
                try:
                    self.on_subscribers_changed()
                except Exception:
                    logger.debug("Ошибка обновления получателей в GUI", exc_info=True)
            self._confirm(chat_id)
        elif command in {"/help", "/помощь"}:
            try:
                TelegramNotifier(self.config_path).send_message(
                    chat_id,
                    "Avito Radar присылает одну сводку на поисковый запрос. "
                    "В ней есть кнопки «Показать» для списка по 10 объявлений "
                    "и «Скачать XLSX».\n\n"
                    "Команда /start — подписаться на уведомления.",
                )
            except NotifierError as exc:
                logger.warning("Не удалось отправить /help: %s", exc)
        else:
            try:
                TelegramNotifier(self.config_path).send_message(
                    chat_id, "Доступные команды: /start — подписаться на уведомления."
                )
            except NotifierError as exc:
                logger.warning("Не удалось ответить на команду: %s", exc)

    def _handle_callback_query(self, callback: dict[str, Any]) -> None:
        callback_id = str(callback.get("id") or "")
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id = int(chat_id)
        subscriber = self.storage.get_subscriber(chat_id)
        notifier = TelegramNotifier(self.config_path)
        if not subscriber or not subscriber.get("is_active"):
            notifier.answer_callback_query(callback_id, "Нет доступа")
            return

        parts = data.split(":")
        if parts and parts[0] == "n":
            notifier.answer_callback_query(callback_id)
            return
        notifier.answer_callback_query(callback_id)
        if len(parts) < 2:
            return
        action, digest_id = parts[0], parts[1]
        payload = self.storage.get_digest(digest_id)
        recipients = [int(item) for item in (payload or {}).get("recipients", [])]
        if not payload or chat_id not in recipients:
            notifier.send_message(chat_id, "Сводка устарела. Запустите новый цикл.")
            return

        default_sort = str(
            self._config().get("notifications", {}).get("sort", "date")
        )
        valid_sorts = {"date", "price", "price_asc", "price_desc", "market_diff"}
        if default_sort not in valid_sorts:
            default_sort = "date"
        sort_key = self.storage.get_meta(
            f"digest_sort:{chat_id}", default_sort
        )
        if sort_key not in valid_sorts:
            sort_key = default_sort

        if action == "s":
            if len(parts) > 2 and parts[2] in valid_sorts:
                sort_key = parts[2]
                self.storage.set_meta(f"digest_sort:{chat_id}", sort_key)
            total_pages = digest_total_pages(payload)
            text = digest_page_text(payload, 0, sort_key=sort_key)
            markup = digest_page_keyboard(digest_id, 0, total_pages, sort_key)
            try:
                notifier.edit_message_text(
                    chat_id,
                    int(message.get("message_id") or 0),
                    text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            except NotifierError:
                notifier.send_message(
                    chat_id, text, reply_markup=markup, parse_mode="HTML"
                )
            return

        if action == "d":
            try:
                page = max(0, int(parts[2])) if len(parts) > 2 else 0
            except ValueError:
                page = 0
            total_pages = digest_total_pages(payload)
            page = min(page, total_pages - 1)
            text = digest_page_text(payload, page, sort_key=sort_key)
            markup = digest_page_keyboard(digest_id, page, total_pages, sort_key)
            try:
                notifier.edit_message_text(
                    chat_id,
                    int(message.get("message_id") or 0),
                    text,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
            except NotifierError:
                notifier.send_message(
                    chat_id, text, reply_markup=markup, parse_mode="HTML"
                )
            return

        if action == "c":
            try:
                notifier.edit_message_text(
                    chat_id,
                    int(message.get("message_id") or 0),
                    "Сводка закрыта.",
                )
            except NotifierError:
                pass
            return

        if action == "x":
            items = [
                item.get("listing") or {}
                for key in ("new", "price", "missing")
                for item in (payload.get(key) or [])
                if isinstance(item, dict)
            ]
            if not items:
                notifier.send_message(chat_id, "В сводке нет объявлений.")
                return
            fd, path = tempfile.mkstemp(
                prefix=f"avito_digest_{digest_id}_", suffix=".xlsx"
            )
            try:
                import os

                os.close(fd)
                export_xlsx(items, path, sort_key="date")
                notifier.send_document(
                    chat_id,
                    path,
                    caption=f"Avito Radar: {payload.get('query') or 'сводка'}",
                )
            except Exception as exc:
                logger.warning("Не удалось отправить XLSX сводки: %s", exc)
                notifier.send_message(
                    chat_id, f"Не удалось подготовить XLSX: {exc}"
                )
            finally:
                try:
                    Path(path).unlink(missing_ok=True)
                except OSError:
                    pass

    def run(self) -> None:
        logger.info("Слушатель Telegram запущен")
        token, api_base, poll_timeout, request_timeout = self._telegram_settings()
        if not token:
            self._notify_error(
                "Токен бота не задан. Уведомления и подписка через /start недоступны."
            )
            logger.info("Слушатель Telegram завершён: нет токена")
            return

        validation_attempt = 0
        bot_username = ""
        while not self._stop_event.is_set():
            token, api_base, poll_timeout, request_timeout = self._telegram_settings()
            if not token:
                self._notify_error("Токен бота не задан.")
                return
            try:
                bot_username = TelegramNotifier(self.config_path).validate_token()
                break
            except TelegramAuthError as exc:
                self._notify_error(f"Telegram: {exc}")
                logger.info("Слушатель Telegram завершён из-за неверного токена")
                return
            except NotifierError as exc:
                validation_attempt += 1
                safe_error = redact_secrets(exc)
                logger.warning(
                    "Не удалось подключиться к Telegram (попытка %s): %s. "
                    "Повтор через 30 секунд.",
                    validation_attempt,
                    safe_error,
                )
                if validation_attempt == 1:
                    self._notify_error(
                        "Нет связи с Telegram API. Проверьте интернет/VPN; "
                        "приложение будет периодически повторять подключение."
                    )
                self._stop_event.wait(30)
        if self._stop_event.is_set():
            return

        logger.info("Бот Telegram подключён: @%s", bot_username)
        if self.on_ready:
            try:
                self.on_ready(bot_username)
            except Exception:
                logger.debug("Ошибка GUI-колбэка on_ready", exc_info=True)

        failures = 0
        while not self._stop_event.is_set():
            token, api_base, poll_timeout, request_timeout = self._telegram_settings()
            if not token:
                self._notify_error("Токен бота удалён из config.toml")
                break
            url = f"{api_base}/bot{token}/getUpdates"
            try:
                response = self._session.get(
                    url,
                    params={
                        "offset": self._offset,
                        "timeout": poll_timeout,
                        "allowed_updates": '["message","callback_query"]',
                    },
                    timeout=poll_timeout + request_timeout,
                )
            except requests.RequestException as exc:
                failures += 1
                if failures == 1 or failures % 10 == 0:
                    safe_error = redact_secrets(exc)
                    logger.warning("Нет связи с Telegram API: %s", safe_error)
                    if self.on_error:
                        try:
                            self.on_error(f"Нет связи с Telegram: {safe_error}")
                        except Exception:
                            pass
                self._stop_event.wait(min(60, 5 * failures))
                continue

            failures = 0
            if response.status_code == 401:
                self._notify_error(
                    "Неверный токен бота. Проверьте [telegram].bot_token в config.toml."
                )
                break
            if response.status_code == 409:
                logger.warning("Telegram 409: этот бот уже опрашивается другим процессом")
                self._stop_event.wait(15)
                continue
            try:
                payload = response.json()
            except ValueError:
                logger.warning("Telegram вернул некорректный JSON (HTTP %s)", response.status_code)
                self._stop_event.wait(10)
                continue
            if not isinstance(payload, dict) or not payload.get("ok"):
                description = ""
                if isinstance(payload, dict):
                    description = str(payload.get("description") or "")
                self._notify_error(f"Ошибка Telegram API: {description or response.status_code}")
                self._stop_event.wait(15)
                continue

            for update in payload.get("result", []):
                update_id = update.get("update_id")
                if update_id is not None:
                    self._save_offset(int(update_id) + 1)
                message = update.get("message")
                if isinstance(message, dict):
                    try:
                        self._handle_message(message)
                    except Exception:
                        logger.exception("Ошибка обработки сообщения Telegram")
                callback = update.get("callback_query")
                if isinstance(callback, dict):
                    try:
                        self._handle_callback_query(callback)
                    except Exception:
                        logger.exception("Ошибка обработки callback Telegram")

        logger.info("Слушатель Telegram остановлен")
