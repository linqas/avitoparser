"""Headless-запуск Avito Radar для планировщика и автоматизации.

Примеры:
    python cli.py list
    python cli.py run --once
    python cli.py export exports/manual.xlsx
    python cli.py add "https://www.avito.ru/..."
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from filters import QueryConfig
from gui import MODE_ONCE, MonitorWorker
from notifier import TelegramNotifier
from reporter import export_xlsx
from storage import Storage
from utils import (
    APP_DIR,
    app_path,
    is_avito_search_url,
    load_config,
    parse_avito_search_url,
    setup_logging,
)

logger = logging.getLogger("avito_radar.cli")


def _config(args: argparse.Namespace) -> tuple[dict[str, Any], Path]:
    config_path = Path(args.config).resolve() if args.config else APP_DIR / "config.toml"
    return load_config(config_path), config_path


def _storage(config: dict[str, Any]) -> Storage:
    return Storage(app_path(config, "database_path", "data/avito_radar.db"))


def _queries(storage: Storage, only: str = "") -> list[QueryConfig]:
    result: list[QueryConfig] = []
    for row in storage.list_saved_queries():
        config = QueryConfig.from_row(row)
        if not config.is_active:
            continue
        if only and only.casefold() not in config.query.casefold():
            continue
        result.append(config)
    return result


def command_list(args: argparse.Namespace) -> int:
    config, _ = _config(args)
    storage = _storage(config)
    rows = storage.list_saved_queries()
    if not rows:
        print("Запросов нет.")
        return 0
    print(f"{'ID':>3}  {'Акт':<3} {'Режим':<7} {'Сорт':<5} Запрос")
    for row in rows:
        query = str(row.get("query") or "")
        print(
            f"{row['id']:>3}  "
            f"{'да' if row.get('is_active', 1) else 'нет':<3} "
            f"{str(row.get('model_mode') or 'strict'):<7} "
            f"{int(row.get('avito_sort') or 0):<5} "
            f"{query[:80]}"
        )
    return 0


def command_add(args: argparse.Namespace) -> int:
    config, _ = _config(args)
    storage = _storage(config)
    value = args.value.strip()
    if not value:
        print("Пустое значение.", file=sys.stderr)
        return 2
    if is_avito_search_url(value):
        parsed = parse_avito_search_url(value)
        row = storage.save_saved_query(
            None,
            query=parsed["query"],
            city=parsed["city"],
            local_priority=parsed["local_priority"],
            max_listings=args.max_listings,
            price_min=parsed["price_min"],
            price_max=parsed["price_max"],
            source_url=parsed["source_url"],
            avito_sort=args.avito_sort,
            model_mode=args.model_mode,
        )
    else:
        row = storage.save_saved_query(
            None,
            query=value,
            city=args.city,
            max_listings=args.max_listings,
            avito_sort=args.avito_sort,
            model_mode=args.model_mode,
        )
    print(f"Добавлен запрос #{row['id']}: {row['query']}")
    return 0


def command_export(args: argparse.Namespace) -> int:
    config, _ = _config(args)
    storage = _storage(config)
    listings = storage.get_listings("active")
    if not listings:
        print("Активных объявлений нет.", file=sys.stderr)
        return 1
    output = Path(args.path) if args.path else APP_DIR / "exports" / "cli.xlsx"
    result = export_xlsx(listings, output, sort_key="market_diff")
    print(f"XLSX: {result}")
    return 0


def command_run(args: argparse.Namespace) -> int:
    config, config_path = _config(args)
    logger_root = setup_logging(
        app_path(config, "log_path", "logs/avito_radar.log")
    )
    logger_root.info("CLI: запуск одного цикла")
    storage = _storage(config)
    queries = _queries(storage, args.query)
    if not queries:
        print("Нет активных запросов. Добавьте запрос через GUI или cli.py add.", file=sys.stderr)
        return 1
    recipients: list[int] = []
    if not args.no_telegram:
        for subscriber in storage.get_subscribers(active_only=True):
            try:
                recipients.append(int(subscriber["chat_id"]))
            except (KeyError, TypeError, ValueError):
                continue
    if not recipients:
        print("Получатели Telegram не выбраны; сбор без уведомлений.", file=sys.stderr)

    def on_status(text: str) -> None:
        print(text, flush=True)

    def on_error(text: str, modal: bool = False) -> None:
        print(f"ОШИБКА: {text}", file=sys.stderr, flush=True)

    worker = MonitorWorker(
        storage=storage,
        notifier=TelegramNotifier(config_path),
        config=config,
        config_path=config_path,
        queries=queries,
        recipients=recipients,
        mode_label=MODE_ONCE,
        interval_minutes=0,
        sort_key="market_diff",
        on_status=on_status,
        on_error=on_error,
        on_cycle=lambda *_args: None,
        on_finished=lambda reason: print(f"Завершено: {reason}", flush=True),
    )
    worker.run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Avito Radar CLI: headless-цикл, список запросов, экспорт."
    )
    parser.add_argument(
        "--config", default=str(APP_DIR / "config.toml"), help="путь к config.toml"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="показать сохранённые запросы")
    p_list.set_defaults(func=command_list)

    p_add = sub.add_parser("add", help="добавить запрос или ссылку Avito")
    p_add.add_argument("value", help="поисковая фраза или ссылка Avito")
    p_add.add_argument("--city", default="", help="город для текстового запроса")
    p_add.add_argument("--max-listings", type=int, default=50)
    p_add.add_argument(
        "--avito-sort", type=int, default=0, choices=[0, 1, 2, 104, 172297],
        help="0 по умолчанию, 104 дата, 1 дешевле, 2 дороже, 172297 по размеру скидки",
    )
    p_add.add_argument(
        "--model-mode", default="strict", choices=["strict", "broad"]
    )
    p_add.set_defaults(func=command_add)

    p_run = sub.add_parser("run", help="выполнить один цикл")
    p_run.add_argument("--once", action="store_true", help="один цикл (по умолчанию)")
    p_run.add_argument("--query", default="", help="фильтр по тексту запроса")
    p_run.add_argument("--no-telegram", action="store_true", help="без отправки в Telegram")
    p_run.set_defaults(func=command_run)

    p_export = sub.add_parser("export", help="экспортировать активные объявления в XLSX")
    p_export.add_argument("path", nargs="?", default="", help="путь к .xlsx")
    p_export.set_defaults(func=command_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Остановлено пользователем.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ОШИБКА: {exc}", file=sys.stderr)
        logger.exception("CLI")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
