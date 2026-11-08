"""Long-polling слушатель Telegram: /start -> подписчик в SQLite.

Поток занимается только сетью Telegram. Скрейпинг Avito остаётся строго
последовательным и выполняется отдельно, без параллельных страниц.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
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
    is_avito_search_url,
    load_config,
    parse_avito_search_url,
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
        on_jobs_changed: Callable[[], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_ready: Callable[[str], None] | None = None,
    ):
        super().__init__(name="AvitoRadarBotListener", daemon=True)
        self.storage = storage
        self.config_path = Path(config_path) if config_path else DEFAULT_CONFIG_PATH
        self.on_subscribers_changed = on_subscribers_changed
        self.on_jobs_changed = on_jobs_changed
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

    def _reply_keyboard(self) -> dict[str, Any]:
        return {
            "keyboard": [
                [{"text": "Новый запрос"}, {"text": "Мои запросы"}],
                [{"text": "Статус"}, {"text": "Сводки"}],
                [{"text": "Помощь"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def _main_menu_markup(self) -> dict[str, Any]:
        return {
            "inline_keyboard": [
                [{"text": "Новый запрос", "callback_data": "menu:new"}],
                [
                    {"text": "Мои запросы", "callback_data": "menu:jobs"},
                    {"text": "Статус", "callback_data": "menu:status"},
                ],
                [
                    {"text": "Сводки", "callback_data": "menu:digests"},
                    {"text": "Помощь", "callback_data": "menu:help"},
                ],
            ]
        }

    def _confirm(self, chat_id: int) -> None:
        try:
            notifier = TelegramNotifier(self.config_path)
            notifier.send_message(
                chat_id,
                "Avito Radar\n\n"
                "Вы подписаны на уведомления. Пользуйтесь кнопками внизу.",
                reply_markup=self._reply_keyboard(),
            )
        except NotifierError as exc:
            logger.warning("Не удалось отправить подтверждение %s: %s", chat_id, exc)
        except Exception:
            logger.exception("Ошибка подтверждения подписки")

    def _jobs_changed(self) -> None:
        if self.on_jobs_changed:
            try:
                self.on_jobs_changed()
            except Exception:
                logger.debug("Ошибка обновления запросов в GUI", exc_info=True)

    def _send_job_menu(self, chat_id: int, job_id: int) -> None:
        markup = {
            "inline_keyboard": [
                [{"text": "Один раз", "callback_data": f"job:once:{job_id}"}],
                [
                    {"text": "Каждые 15 минут", "callback_data": f"job:interval:{job_id}:15"},
                ],
                [
                    {"text": "Каждые 30 минут", "callback_data": f"job:interval:{job_id}:30"},
                ],
                [
                    {"text": "Каждые 60 минут", "callback_data": f"job:interval:{job_id}:60"},
                ],
                [{"text": "Свой интервал", "callback_data": f"job:custom:{job_id}"}],
                [{"text": "Отмена", "callback_data": "menu:cancel"}],
            ]
        }
        TelegramNotifier(self.config_path).send_message(
            chat_id,
            "Ссылка принята. Выберите режим работы задания.",
            reply_markup=markup,
        )

    def _create_job_from_url(self, chat_id: int, username: str, url: str) -> None:
        if not self.storage.can_create_requests(chat_id):
            TelegramNotifier(self.config_path).send_message(
                chat_id,
                "Создавать запросы может только владелец "
                "или пользователь из белого списка.",
            )
            return
        if not is_avito_search_url(url):
            TelegramNotifier(self.config_path).send_message(
                chat_id,
                "Пришлите ссылку на поиск Avito. Фильтры возьмутся из ссылки.",
            )
            return
        try:
            parsed = parse_avito_search_url(url)
            config = self._config()
            max_listings = int(config.get("app", {}).get("max_listings", 50))
            row = self.storage.save_saved_query(
                None,
                query=parsed["query"],
                city=parsed["city"],
                local_priority=parsed["local_priority"],
                max_listings=max_listings,
                price_min=parsed["price_min"],
                price_max=parsed["price_max"],
                source_url=parsed["source_url"],
                avito_sort=parsed.get("avito_sort", 0),
            )
            self.storage.set_query_origin(int(row["id"]), "telegram")
            job_id = self.storage.create_bot_job(
                int(row["id"]), chat_id, username, mode="pending"
            )
            self._send_job_menu(chat_id, job_id)
            self._jobs_changed()
        except Exception as exc:
            logger.warning("Не удалось создать запрос Telegram: %s", redact_secrets(exc))
            TelegramNotifier(self.config_path).send_message(
                chat_id, f"Не удалось принять ссылку: {redact_secrets(exc)}"
            )

    def _format_jobs(self, chat_id: int) -> str:
        jobs = self.storage.list_bot_jobs(chat_id=chat_id)
        if not jobs:
            return "У вас пока нет запросов."
        lines = ["Ваши запросы:"]
        status_labels = {
            "pending": "ожидает",
            "running": "выполняется",
            "waiting": "в очереди",
            "done": "выполнено",
            "stopped": "остановлено",
            "error": "ошибка",
        }
        for job in jobs:
            mode = {
                "pending": "режим не выбран",
                "once": "один раз",
                "interval": f"каждые {job.get('interval_minutes')} мин",
            }.get(str(job.get("mode")), str(job.get("mode")))
            query = self.storage.get_saved_query(int(job["query_id"])) or {}
            title = str(query.get("query") or "Без названия")[:45]
            status = status_labels.get(str(job.get("status")), str(job.get("status")))
            lines.append(f"{title} — {mode} — {status}")
        return "\n".join(lines)

    def _status_text(self) -> str:
        state = self.storage.get_collector_state()
        jobs = self.storage.list_bot_jobs(active_only=True)
        queue = [
            job for job in jobs
            if str(job.get("status")) in {"pending", "waiting"}
        ]
        if str(state.get("status")) == "running":
            kind = str(state.get("kind") or "")
            if kind == "manual":
                current = "выполняется ручной цикл приложения"
            else:
                current = "выполняется запрос"
            query = str(state.get("query") or "").strip()
            if query:
                current += f"\nЗапрос: {query}"
            started = str(state.get("started_at") or "")
            if started:
                try:
                    delta = datetime.now().astimezone() - datetime.fromisoformat(started)
                    minutes = max(0, int(delta.total_seconds() // 60))
                    current += f"\nДлительность: {minutes} мин"
                except (TypeError, ValueError):
                    pass
        else:
            current = "сейчас цикл не выполняется"
        lines = [current, f"В очереди: {len(queue)} запросов"]
        next_run = ""
        for job in jobs:
            if job.get("next_run_at"):
                next_run = str(job["next_run_at"])
                break
        if next_run:
            lines.append(f"Следующий запуск: {next_run}")
        return "\n".join(lines)

    def _send_digests_list(self, chat_id: int) -> None:
        rows: list[list[dict[str, str]]] = []
        for digest in self.storage.list_digests(50):
            payload = digest.get("payload") or {}
            recipients = [int(item) for item in payload.get("recipients", [])]
            if chat_id not in recipients:
                continue
            total = sum(
                len(payload.get(key) or [])
                for key in ("new", "price", "missing")
            )
            query = str(payload.get("query") or "Сводка")[:40]
            created = str(digest.get("created_at") or "")[:16].replace("T", " ")
            rows.append(
                [
                    {
                        "text": f" · {query} · {total} · {created}",
                        "callback_data": f"dg:{digest['id']}",
                    }
                ]
            )
            if len(rows) >= 10:
                break
        if not rows:
            self._send_main_menu(chat_id, "Сводок пока нет.")
            return
        rows.extend(self._main_menu_markup()["inline_keyboard"])
        TelegramNotifier(self.config_path).send_message(
            chat_id,
            "Последние сводки. Нажмите, чтобы открыть.",
            reply_markup={"inline_keyboard": rows},
        )

    def _send_status(self, chat_id: int, message_id: int | None = None) -> None:
        text = self._status_text()
        markup = {
            "inline_keyboard": [
                [{"text": "Обновить", "callback_data": "status:refresh"}],
                [{"text": "В меню", "callback_data": "menu:help"}],
            ]
        }
        notifier = TelegramNotifier(self.config_path)
        if message_id:
            try:
                notifier.edit_message_text(
                    chat_id, message_id, text, reply_markup=markup
                )
                return
            except NotifierError:
                pass
        notifier.send_message(chat_id, text, reply_markup=markup)

    def _jobs_menu_markup(self, chat_id: int) -> dict[str, Any]:
        rows: list[list[dict[str, str]]] = []
        jobs = self.storage.list_bot_jobs(chat_id=chat_id)
        for job in jobs:
            job_id = int(job["id"])
            rows.append(
                [
                    {"text": "Стоп", "callback_data": f"jobctl:stop:{job_id}"},
                    {"text": "Интервал", "callback_data": f"jobctl:interval:{job_id}"},
                ]
            )
            rows.append(
                [
                    {"text": "Заново", "callback_data": f"jobctl:restart:{job_id}"},
                    {"text": "Ссылка", "callback_data": f"jobctl:link:{job_id}"},
                ]
            )
        return {"inline_keyboard": rows}

    def _send_jobs_menu(self, chat_id: int) -> None:
        TelegramNotifier(self.config_path).send_message(
            chat_id,
            self._format_jobs(chat_id),
            reply_markup=self._jobs_menu_markup(chat_id),
        )

    def _handle_job_control(
        self, chat_id: int, callback_id: str, parts: list[str], notifier: TelegramNotifier
    ) -> None:
        if len(parts) < 3:
            return
        try:
            job_id = int(parts[2])
        except ValueError:
            return
        job = self.storage.get_bot_job(job_id)
        if not job or int(job["chat_id"]) != int(chat_id):
            notifier.answer_callback_query(callback_id, "Запрос не найден")
            return
        action = parts[1]
        if action == "stop":
            self.storage.update_bot_job(job_id, status="stopped")
            notifier.answer_callback_query(callback_id, "Остановлено")
            self._jobs_changed()
            self._send_jobs_menu(chat_id)
            return
        if action == "restart":
            self.storage.update_bot_job(
                job_id, status="pending", first_run_done=0
            )
            notifier.answer_callback_query(callback_id, "Запрос в очереди")
            self._jobs_changed()
            self._send_jobs_menu(chat_id)
            return
        if action == "interval":
            self.storage.set_bot_state(
                chat_id, "waiting_job_interval", {"job_id": job_id}
            )
            notifier.answer_callback_query(callback_id)
            notifier.send_message(
                chat_id,
                "Введите новый интервал в минутах от 1 до 1440.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Отмена", "callback_data": "menu:cancel"}]
                    ]
                },
            )
            return
        if action == "link":
            query = self.storage.get_saved_query(int(job["query_id"]))
            url = str((query or {}).get("source_url") or "")
            notifier.answer_callback_query(callback_id)
            notifier.send_message(chat_id, url or "У задания нет исходной ссылки.")
            return

    def _handle_job_callback(
        self, chat_id: int, callback_id: str, parts: list[str], notifier: TelegramNotifier
    ) -> None:
        if len(parts) < 3:
            return
        try:
            job_id = int(parts[2])
        except ValueError:
            return
        job = self.storage.get_bot_job(job_id)
        if not job or int(job["chat_id"]) != int(chat_id):
            notifier.answer_callback_query(callback_id, "Запрос не найден")
            return
        mode = parts[1]
        if mode == "custom":
            self.storage.set_bot_state(
                chat_id, "waiting_custom_interval", {"job_id": job_id}
            )
            notifier.answer_callback_query(callback_id)
            notifier.send_message(
                chat_id,
                "Введите интервал в минутах от 1 до 1440.\n"
                "Например: 120",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Отмена", "callback_data": "menu:cancel"}]
                    ]
                },
            )
            return
        if mode == "once":
            self.storage.update_bot_job(job_id, mode="once", status="pending")
            notifier.answer_callback_query(callback_id, "Режим: один раз")
            self._send_main_menu(
                chat_id, f"Запрос #{job_id} создано: один раз."
            )
        elif mode == "interval":
            try:
                minutes = max(1, int(parts[3]))
            except (IndexError, ValueError):
                minutes = 15
            minutes = min(1440, minutes)
            self.storage.update_bot_job(
                job_id, mode="interval", interval_minutes=minutes, status="pending"
            )
            notifier.answer_callback_query(callback_id, f"Интервал: {minutes} мин")
            self._send_main_menu(
                chat_id, f"Запрос #{job_id} создано: каждые {minutes} мин."
            )
        self._jobs_changed()

    def _send_main_menu(self, chat_id: int, text: str) -> None:
        TelegramNotifier(self.config_path).send_message(chat_id, text)

    def _help_text(self) -> str:
        return (
            "Avito Radar\n\n"
            "Новый запрос — пришлите ссылку Avito и выберите интервал.\n"
            "Мои запросы — список и управление.\n"
            "Статус — что выполняется сейчас.\n"
            "Сводки — последние уведомления.\n\n"
            "Команды: /add URL, /list, /status, /digests, /stop <номер>, /help."
        )

    def _handle_menu_callback(
        self, chat_id: int, callback_id: str, action: str, notifier: TelegramNotifier
    ) -> None:
        notifier.answer_callback_query(callback_id)
        if action == "new":
            if not self.storage.can_create_requests(chat_id):
                notifier.send_message(
                    chat_id,
                    "Создавать запросы может только владелец "
                    "или пользователь из белого списка.",
                )
                return
            self.storage.set_bot_state(chat_id, "waiting_link")
            notifier.send_message(
                chat_id,
                "Пришлите ссылку на поиск Avito.\n"
                "Фильтры и город возьмутся из ссылки.",
                reply_markup={
                    "inline_keyboard": [
                        [{"text": "Отмена", "callback_data": "menu:cancel"}]
                    ]
                },
            )
            return
        if action == "jobs":
            self._send_jobs_menu(chat_id)
            return
        if action == "status":
            self._send_status(chat_id)
            return
        if action == "digests":
            self._send_digests_list(chat_id)
            return
        if action == "help":
            self._send_main_menu(chat_id, self._help_text())
            return
        if action == "cancel":
            state = self.storage.get_bot_state(chat_id) or {}
            if state.get("state") == "waiting_custom_interval":
                try:
                    payload = json.loads(state.get("payload") or "{}")
                    job_id = int(payload.get("job_id"))
                    self.storage.update_bot_job(job_id, status="stopped")
                except (TypeError, ValueError, json.JSONDecodeError):
                    pass
            self.storage.clear_bot_state(chat_id)
            self._send_main_menu(chat_id, "Действие отменено.")
            return

    def _handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        chat_id = chat.get("id")
        if chat_id is None:
            return
        text = str(message.get("text") or "").strip()
        username = chat.get("username") or ""
        first_name = chat.get("first_name") or ""
        notifier = TelegramNotifier(self.config_path)
        if text in {"Новый запрос", "Мои запросы", "Статус", "Сводки", "Помощь"}:
            if text == "Новый запрос":
                if not self.storage.can_create_requests(chat_id):
                    notifier.send_message(
                        chat_id,
                        "Создавать запросы может только владелец "
                        "или пользователь из белого списка.",
                    )
                    return
                self.storage.set_bot_state(chat_id, "waiting_link")
                notifier.send_message(
                    chat_id,
                    "Пришлите ссылку на поиск Avito. Фильтры возьмутся из ссылки.",
                )
            elif text == "Мои запросы":
                self._send_jobs_menu(chat_id)
            elif text == "Статус":
                self._send_status(chat_id)
            elif text == "Сводки":
                self._send_digests_list(chat_id)
            else:
                self._send_main_menu(chat_id, self._help_text())
            return
        if text and is_avito_search_url(text):
            self.storage.clear_bot_state(chat_id)
            self._create_job_from_url(chat_id, username, text)
            return
        state = self.storage.get_bot_state(chat_id)
        if state and state.get("state") == "waiting_job_interval":
            if text == "/cancel":
                self.storage.clear_bot_state(chat_id)
                self._send_jobs_menu(chat_id)
                return
            if not text.isdigit() or not 1 <= int(text) <= 1440:
                TelegramNotifier(self.config_path).send_message(
                    chat_id,
                    "Нужно целое число от 1 до 1440 минут.",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "Отмена", "callback_data": "menu:cancel"}]
                        ]
                    },
                )
                return
            try:
                payload = json.loads(state.get("payload") or "{}")
                job_id = int(payload.get("job_id"))
            except (TypeError, ValueError, json.JSONDecodeError):
                self.storage.clear_bot_state(chat_id)
                self._send_jobs_menu(chat_id)
                return
            self.storage.update_bot_job(
                job_id, interval_minutes=int(text), mode="interval"
            )
            self.storage.clear_bot_state(chat_id)
            self._jobs_changed()
            self._send_jobs_menu(chat_id)
            return
        if state and state.get("state") == "waiting_custom_interval":
            if text == "/cancel":
                self.storage.clear_bot_state(chat_id)
                self._send_main_menu(chat_id, "Ввод интервала отменён.")
                return
            if not text.isdigit() or not 1 <= int(text) <= 1440:
                TelegramNotifier(self.config_path).send_message(
                    chat_id,
                    "Нужно целое число от 1 до 1440 минут. Например: 120",
                    reply_markup={
                        "inline_keyboard": [
                            [{"text": "Отмена", "callback_data": "menu:cancel"}]
                        ]
                    },
                )
                return
            try:
                payload = json.loads(state.get("payload") or "{}")
                job_id = int(payload.get("job_id"))
            except (TypeError, ValueError, json.JSONDecodeError):
                self.storage.clear_bot_state(chat_id)
                self._send_main_menu(chat_id, "Не удалось определить запрос.")
                return
            minutes = int(text)
            self.storage.update_bot_job(
                job_id, mode="interval", interval_minutes=minutes, status="pending"
            )
            self.storage.clear_bot_state(chat_id)
            self._jobs_changed()
            self._send_main_menu(
                chat_id, f"Запрос #{job_id} создано: каждые {minutes} мин."
            )
            return
        if state and state.get("state") == "waiting_link" and not text.startswith("/"):
            TelegramNotifier(self.config_path).send_message(
                chat_id, "Жду ссылку на поиск Avito. Например: https://www.avito.ru/..."
            )
            return
        if not text.startswith("/"):
            TelegramNotifier(self.config_path).send_message(
                chat_id,
                "Пришлите ссылку на поиск Avito или используйте /help.",
                reply_markup=self._main_menu_markup(),
            )
            return
        command = text.split(maxsplit=1)[0].split("@", 1)[0].lower()

        if command == "/add":
            _, _, value = text.partition(" ")
            value = value.strip()
            if value:
                self._create_job_from_url(chat_id, username, value)
            else:
                TelegramNotifier(self.config_path).send_message(
                    chat_id, "Использование: /add https://www.avito.ru/..."
                )
            return
        if command == "/list":
            self._send_jobs_menu(chat_id)
            return
        if command == "/status":
            self._send_status(chat_id)
            return
        if command == "/digests":
            self._send_digests_list(chat_id)
            return
        if command == "/stop":
            _, _, value = text.partition(" ")
            job_id = None
            if value.strip().lstrip("#").isdigit():
                job_id = int(value.strip().lstrip("#"))
            else:
                state = self.storage.get_collector_state()
                if state.get("kind") == "telegram" and state.get("job_id"):
                    job_id = int(state["job_id"])
                else:
                    running = [
                        job for job in self.storage.list_bot_jobs(chat_id=chat_id)
                        if str(job.get("status")) == "running"
                    ]
                    if running:
                        job_id = int(running[0]["id"])
            if job_id is None:
                TelegramNotifier(self.config_path).send_message(
                    chat_id, "Сейчас нет выполняющегося запроса."
                )
                return
            job = self.storage.get_bot_job(job_id)
            if not job or int(job["chat_id"]) != int(chat_id):
                TelegramNotifier(self.config_path).send_message(chat_id, "Запрос не найден.")
                return
            self.storage.update_bot_job(job_id, status="stopped")
            self._jobs_changed()
            TelegramNotifier(self.config_path).send_message(
                chat_id, "Запрос остановлен."
            )
            return

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
                self._send_main_menu(chat_id, self._help_text())
            except NotifierError as exc:
                logger.warning("Не удалось отправить /help: %s", exc)
        else:
            try:
                self._send_main_menu(
                    chat_id,
                    "Доступные команды: /start, /add <ссылка>, /list, /stop <номер>, /status, /digests, /help.",
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
        parts = data.split(":")
        if parts and parts[0] == "menu":
            action = parts[1] if len(parts) > 1 else ""
            self._handle_menu_callback(chat_id, callback_id, action, notifier)
            return
        if parts and parts[0] == "status":
            notifier.answer_callback_query(callback_id)
            self._send_status(chat_id, int(message.get("message_id") or 0) or None)
            return
        if parts and parts[0] == "jobctl":
            self._handle_job_control(chat_id, callback_id, parts, notifier)
            return
        if parts and parts[0] == "job":
            self._handle_job_callback(chat_id, callback_id, parts, notifier)
            return
        if not subscriber or not subscriber.get("is_active"):
            notifier.answer_callback_query(callback_id, "Нет доступа")
            return
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

        city = str(payload.get("city") or "")
        default_local = bool(city.strip() and city.strip().casefold() != "вся россия")

        if action == "dg":
            total_pages = digest_total_pages(payload)
            text = digest_page_text(
                payload, 0, sort_key=sort_key, local_first=default_local
            )
            markup = digest_page_keyboard(
                digest_id, 0, total_pages, sort_key,
                local_first=default_local, city=city,
            )
            try:
                notifier.edit_message_text(
                    chat_id, int(message.get("message_id") or 0),
                    text, reply_markup=markup, parse_mode="HTML",
                )
            except NotifierError:
                notifier.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            return

        if action == "s":
            if len(parts) > 2 and parts[2] in valid_sorts:
                sort_key = parts[2]
                self.storage.set_meta(f"digest_sort:{chat_id}", sort_key)
            local_first = parts[3] == "1" if len(parts) > 3 else default_local
            total_pages = digest_total_pages(payload)
            text = digest_page_text(
                payload, 0, sort_key=sort_key, local_first=local_first
            )
            markup = digest_page_keyboard(
                digest_id, 0, total_pages, sort_key,
                local_first=local_first, city=city,
            )
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
            local_first = parts[3] == "1" if len(parts) > 3 else default_local
            total_pages = digest_total_pages(payload)
            page = min(page, total_pages - 1)
            text = digest_page_text(
                payload, page, sort_key=sort_key, local_first=local_first
            )
            markup = digest_page_keyboard(
                digest_id, page, total_pages, sort_key,
                local_first=local_first, city=city,
            )
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

        if action == "l":
            local_first = len(parts) > 2 and parts[2] == "1"
            total_pages = digest_total_pages(payload)
            text = digest_page_text(
                payload, 0, sort_key=sort_key, local_first=local_first
            )
            markup = digest_page_keyboard(
                digest_id, 0, total_pages, sort_key,
                local_first=local_first, city=city,
            )
            try:
                notifier.edit_message_text(
                    chat_id, int(message.get("message_id") or 0),
                    text, reply_markup=markup, parse_mode="HTML",
                )
            except NotifierError:
                notifier.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")
            return

        if action == "c":
            try:
                notifier.edit_message_text(
                    chat_id,
                    int(message.get("message_id") or 0),
                    "Сводка закрыта. Открыть снова: /digests",
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
