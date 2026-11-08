"""Avito Radar — точка входа.

Обычный запуск:      python main.py
Автономное демо:     python main.py --demo --limit 5
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from filters import FilterCriteria, apply_filters
from storage import Storage
from utils import (
    APP_DIR,
    ConfigError,
    app_path,
    format_datetime,
    format_delivery,
    format_price,
    load_config,
    normalize_listing,
    now_moscow,
    setup_logging,
)

APP_NAME = "Avito Radar"
VERSION = "1.0.0"

DEMO_RAW = [
    {
        "id": "900000001",
        "title": "iPhone 13 128 ГБ, идеальное состояние",
        "price": "54 900 ₽",
        "published_at": "сегодня, 12:10",
        "city": "Москва",
        "delivery": "yes",
        "url": "https://www.avito.ru/moskva/telefony/iphone_13_900000001",
        "views": "123 просмотра",
    },
    {
        "id": "900000002",
        "title": "MacBook Air M1 8/256, гарантия",
        "price": "62 000 ₽",
        "published_at": "вчера, 18:40",
        "city": "Москва",
        "delivery": "yes",
        "url": "https://www.avito.ru/moskva/noutbuki/macbook_air_m1_900000002",
        "views": "45 просмотров",
    },
    {
        "id": "900000003",
        "title": "Монитор Samsung 27 дюймов",
        "price": "18 500 ₽",
        "published_at": "3 часа назад",
        "city": "Москва",
        "delivery": "",
        "url": "https://www.avito.ru/moskva/tovary_dlya_kompyutera/monitor_900000003",
        "views": "8 просмотров",
    },
    {
        "id": "900000004",
        "title": "Кофемолка ручная, новая",
        "price": "3 200 ₽",
        "published_at": "11 июля, 09:15",
        "city": "Москва",
        "delivery": "no",
        "url": "https://www.avito.ru/moskva/posuda_i_tovary_dlya_kuhni/kofemolka_900000004",
        "views": "2 просмотра",
    },
    {
        "id": "900000005",
        "title": "Дрель ударная, цена договорная",
        "price": "Цена не указана",
        "published_at": "2 дня назад",
        "city": "Москва",
        "delivery": "",
        "url": "https://www.avito.ru/moskva/remont_i_stroitelstvo/drel_900000005",
        "views": "",
    },
]


def run_demo(config_path: Path, limit: int = 5) -> int:
    """Офлайн-демонстрация полного пайплайна без сети и Telegram."""
    limit = max(1, min(int(limit), len(DEMO_RAW)))
    config = load_config(config_path)
    log_path = APP_DIR / "logs" / "demo.log"
    setup_logging(log_path)
    demo_db = APP_DIR / "data" / "demo_avito_radar.db"
    export_path = APP_DIR / "exports" / "demo_avito_radar.xlsx"

    print("Avito Radar — демо-режим")
    print("Сеть, Playwright и Telegram отключены. Используется локальная база demo.")
    print(f"База: {demo_db}")
    print()

    with Storage(demo_db) as storage:
        raw_items = [dict(item) for item in DEMO_RAW[:limit]]
        existing_iphone = storage.get_listing("900000001")
        if existing_iphone and existing_iphone.get("price") == 54_900:
            raw_items[0]["price"] = "52 900 ₽"

        now = now_moscow()
        listings: list[dict[str, Any]] = []
        for raw in raw_items:
            try:
                listings.append(normalize_listing(raw, now=now))
            except Exception as exc:
                print(f"Пропущена демо-карточка: {exc}")

        filtered = apply_filters(listings, FilterCriteria())
        print(f"Собрано карточек: {len(listings)}")
        print(
            f"После фильтров: {len(filtered.accepted)} "
            f"(отклонено: {len(filtered.rejected)})"
        )
        print()

        events: list[tuple[str, dict[str, Any], int | None]] = []
        ids: list[str] = []
        for listing in filtered.accepted:
            result = storage.upsert_listing(listing)
            ids.append(result.listing["id"])
            if result.created:
                events.append(("новое", result.listing, None))
            if result.price_changed:
                events.append(("изменение цены", result.listing, result.old_price))
        query_key = "демо|москва"
        storage.record_seen(query_key, ids)
        missing = storage.find_missing_seen_ids(query_key, ids)
        storage.mark_seen_missing(query_key, missing)
        for listing_id in missing:
            became_missing, listing = storage.refresh_listing_status(listing_id)
            if became_missing and listing:
                events.append(("возможно снято", listing, None))

        print("События:")
        if events:
            for kind, listing, old_price in events:
                if kind == "изменение цены":
                    print(
                        f"  [{kind}] {listing['title']}: "
                        f"{format_price(old_price)} → {format_price(listing['price'])}"
                    )
                else:
                    print(f"  [{kind}] {listing['title']} — {format_price(listing['price'])}")
        else:
            print("  (изменений нет — повторный запуск без изменений)")
        print()

        all_listings = storage.get_listings()
        from reporter import export_xlsx, split_and_sort

        priced, no_price = split_and_sort(all_listings, "price_asc")
        print("С ценой (сортировка по возрастанию цены):")
        for listing in priced:
            print(
                f"  {format_price(listing['price']):>12} | {listing['title'][:52]} | "
                f"{listing['city']} | доставка: {format_delivery(listing['delivery'])} | "
                f"{format_datetime(listing['published_at'])}"
            )
        print()
        print("Без цены (отдельная группа):")
        if no_price:
            for listing in no_price:
                print(
                    f"  {'—':>12} | {listing['title'][:52]} | "
                    f"{listing['city'] or listing.get('metro') or '—'}"
                )
        else:
            print("  нет объявлений без цены")
        print()

        history = {
            str(listing["id"]): storage.get_price_history(str(listing["id"]))
            for listing in all_listings
        }
        saved = export_xlsx(
            all_listings,
            export_path,
            sort_key="price_asc",
            price_history=history,
        )
        stats = storage.get_stats()
        print(
            f"Итого в demo-базе: {stats['listings']} (активно {stats['active']}, "
            f"без цены {stats['no_price']})"
        )
        print(f"XLSX: {saved}")
        print(f"Лог: {log_path}")
        print()
        print(
            "Повторный запуск demo покажет изменение цены iPhone. "
            "Удалите data/demo_avito_radar.db для сброса."
        )

    return 0


def run_gui(config_path: Path) -> int:
    try:
        from gui import AvitoRadarApp
    except ImportError as exc:
        print(
            "Не удалось загрузить GUI. Установите зависимости:\n"
            "  pip install -r requirements.txt\n"
            f"Ошибка: {exc}",
            file=sys.stderr,
        )
        return 1

    config: dict[str, Any] | None = None
    try:
        config = load_config(config_path)
        setup_logging(app_path(config, "log_path", "logs/avito_radar.log"))
    except ConfigError as exc:
        print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
        try:
            from tkinter import messagebox

            messagebox.showerror(APP_NAME, str(exc))
        except Exception:
            pass
        return 2

    try:
        app = AvitoRadarApp(config_path=config_path, config=config)
        app.mainloop()
    except Exception as exc:
        logging.getLogger("avito_radar.main").exception("Критическая ошибка GUI")
        print(f"Критическая ошибка: {exc}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Avito Radar — локальный монитор объявлений Avito с Telegram."
    )
    parser.add_argument(
        "--config",
        default=str(APP_DIR / "config.toml"),
        help="Путь к config.toml (по умолчанию рядом с main.py)",
    )
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Офлайн-демо без сети, браузера и Telegram",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Количество демо-карточек (по умолчанию 5)",
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {VERSION}")
    return parser


def main(argv: list[str] | None = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    config_path = Path(args.config)
    if args.demo:
        try:
            return run_demo(config_path, args.limit)
        except ConfigError as exc:
            print(f"Ошибка конфигурации: {exc}", file=sys.stderr)
            return 2
        except Exception as exc:
            print(f"Ошибка демо-режима: {exc}", file=sys.stderr)
            return 1
    return run_gui(config_path)


if __name__ == "__main__":
    raise SystemExit(main())
