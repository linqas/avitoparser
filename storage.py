"""SQLite-хранилище Avito Radar.

Таблицы:
  listings       — последнее известное состояние объявления;
  price_history  — история цен;
  seen_ids       — какие объявления встречались в каком поисковом запросе;
  subscribers    — chat_id пользователей, отправивших /start боту.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from utils import city_from_url, clean_metro, clean_text, fix_city_name, iso_now

logger = logging.getLogger("avito_radar.storage")

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id             TEXT PRIMARY KEY,
    title          TEXT NOT NULL,
    price          INTEGER,
    published_at   TEXT,
    city           TEXT NOT NULL DEFAULT '',
    metro          TEXT NOT NULL DEFAULT '',
    delivery       TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (delivery IN ('yes', 'no', 'unknown')),
    url            TEXT NOT NULL DEFAULT '',
    seller         TEXT NOT NULL DEFAULT '',
    seller_type    TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (seller_type IN ('private', 'company', 'unknown')),
    views          INTEGER,
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'missing'))
);

CREATE TABLE IF NOT EXISTS price_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    price       INTEGER,
    observed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seen_ids (
    query_key     TEXT NOT NULL,
    listing_id    TEXT NOT NULL REFERENCES listings(id) ON DELETE CASCADE,
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'active'
                  CHECK (status IN ('active', 'missing')),
    PRIMARY KEY (query_key, listing_id)
);

CREATE TABLE IF NOT EXISTS subscribers (
    chat_id       INTEGER PRIMARY KEY,
    username      TEXT NOT NULL DEFAULT '',
    first_name    TEXT NOT NULL DEFAULT '',
    subscribed_at TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS saved_queries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    query         TEXT NOT NULL,
    city          TEXT NOT NULL DEFAULT '',
    local_priority INTEGER NOT NULL DEFAULT 0,
    max_listings  INTEGER NOT NULL DEFAULT 50,
    pages         INTEGER NOT NULL DEFAULT 1,
    price_min     INTEGER,
    price_max     INTEGER,
    delivery      TEXT NOT NULL DEFAULT 'any'
                  CHECK (delivery IN ('any', 'require', 'exclude')),
    seller_type   TEXT NOT NULL DEFAULT 'any',
    include_words TEXT NOT NULL DEFAULT '',
    exclude_words TEXT NOT NULL DEFAULT '',
    source_url    TEXT NOT NULL DEFAULT '',
    primed        INTEGER NOT NULL DEFAULT 1,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    avito_sort    INTEGER NOT NULL DEFAULT 0,
    model_mode    TEXT NOT NULL DEFAULT 'strict',
    smart_filter  INTEGER NOT NULL DEFAULT 0,
    category      TEXT NOT NULL DEFAULT '',
    memory        TEXT NOT NULL DEFAULT '',
    color         TEXT NOT NULL DEFAULT '',
    sim           TEXT NOT NULL DEFAULT '',
    condition     TEXT NOT NULL DEFAULT '',
    is_active     INTEGER NOT NULL DEFAULT 1,
    notify_new    INTEGER NOT NULL DEFAULT 1,
    notify_price  INTEGER NOT NULL DEFAULT 1,
    notify_old    INTEGER NOT NULL DEFAULT 0,
    origin        TEXT NOT NULL DEFAULT 'app',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS bot_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    query_id         INTEGER NOT NULL REFERENCES saved_queries(id) ON DELETE CASCADE,
    chat_id          INTEGER NOT NULL,
    username         TEXT NOT NULL DEFAULT '',
    mode             TEXT NOT NULL DEFAULT 'pending'
                     CHECK (mode IN ('pending', 'once', 'interval')),
    interval_minutes INTEGER NOT NULL DEFAULT 0,
    status           TEXT NOT NULL DEFAULT 'pending',
    first_run_done   INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    last_run_at      TEXT NOT NULL DEFAULT '',
    next_run_at      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_bot_jobs_chat ON bot_jobs(chat_id, status);

CREATE INDEX IF NOT EXISTS idx_listings_status ON listings(status);
CREATE INDEX IF NOT EXISTS idx_listings_price ON listings(price);
CREATE INDEX IF NOT EXISTS idx_price_history_listing
    ON price_history(listing_id, observed_at);
CREATE INDEX IF NOT EXISTS idx_seen_ids_listing ON seen_ids(listing_id);

CREATE TABLE IF NOT EXISTS digests (
    id         TEXT PRIMARY KEY,
    payload    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_digests_created ON digests(created_at);

CREATE TABLE IF NOT EXISTS favorites (
    listing_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS seller_blacklist (
    seller     TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta(key, value) VALUES ('schema_version', '1')
ON CONFLICT(key) DO NOTHING;
"""


@dataclass(slots=True)
class UpsertResult:
    created: bool
    price_changed: bool
    old_price: int | None
    new_price: int | None
    restored: bool
    listing: dict[str, Any]


class Storage:
    """Потокобезопасное хранилище (один SQLite-коннект под RLock)."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._closed = False
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30
        )
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON")
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.executescript(SCHEMA)
            self._ensure_column("listings", "metro", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column("listings", "seller", "TEXT NOT NULL DEFAULT ''")
            self._ensure_column(
                "saved_queries", "local_priority", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                "saved_queries", "max_listings", "INTEGER NOT NULL DEFAULT 50"
            )
            self._ensure_column(
                "saved_queries", "pages", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                "saved_queries", "primed", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                "saved_queries", "source_url", "TEXT NOT NULL DEFAULT ''"
            )
            self._ensure_column(
                "saved_queries", "avito_sort", "INTEGER NOT NULL DEFAULT 0"
            )
            self._ensure_column(
                "saved_queries", "model_mode", "TEXT NOT NULL DEFAULT 'strict'"
            )
            self._ensure_column(
                "saved_queries", "smart_filter", "INTEGER NOT NULL DEFAULT 0"
            )
            for column in ("category", "memory", "color", "sim", "condition"):
                self._ensure_column(
                    "saved_queries", column, "TEXT NOT NULL DEFAULT ''"
                )
            self._ensure_column(
                "saved_queries", "is_active", "INTEGER NOT NULL DEFAULT 1"
            )
            self._ensure_column(
                "saved_queries", "origin", "TEXT NOT NULL DEFAULT 'app'"
            )
            for column, default in (
                ("notify_new", 1),
                ("notify_price", 1),
                ("notify_old", 0),
            ):
                self._ensure_column(
                    "saved_queries",
                    column,
                    f"INTEGER NOT NULL DEFAULT {default}",
                )
        self.repair_listing_locations()
        logger.info("База данных готова: %s", self.db_path)

    def repair_listing_locations(self) -> int:
        """Заполняет/исправляет город из URL и переносит старую локацию в метро."""
        repaired = 0
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT id, url, city, metro FROM listings WHERE url <> ''"
            ).fetchall()
            for row in rows:
                old_city = clean_text(row["city"])
                old_metro = clean_text(row["metro"])
                fixed_old = fix_city_name(old_city)
                if fixed_old and re.search(r"[А-Яа-яЁё]", fixed_old):
                    canonical = fixed_old
                else:
                    canonical = fix_city_name(city_from_url(str(row["url"] or "")))
                if not canonical:
                    continue
                metro = old_metro
                if (
                    not metro
                    and old_city
                    and old_city.casefold().replace("ё", "е")
                    != canonical.casefold().replace("ё", "е")
                ):
                    metro = clean_metro(old_city, canonical)
                if old_city != canonical or metro != old_metro:
                    self._conn.execute(
                        "UPDATE listings SET city = ?, metro = ? WHERE id = ?",
                        (canonical, metro, row["id"]),
                    )
                    repaired += 1
        if repaired:
            logger.info("Исправлены города/метро у объявлений: %s", repaired)
        return repaired

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        columns = {
            str(row["name"])
            for row in self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if column not in columns:
            self._conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {column} {declaration}"
            )
            logger.info("Миграция БД: добавлена колонка %s.%s", table, column)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._conn.close()
                self._closed = True

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    # ----------------------------- listings -----------------------------

    @staticmethod
    def _row_to_listing(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def get_listing(self, listing_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM listings WHERE id = ?", (str(listing_id),)
            ).fetchone()
            return self._row_to_listing(row)

    def get_listings(self, status: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            if status:
                rows = self._conn.execute(
                    "SELECT * FROM listings WHERE status = ? ORDER BY last_seen_at DESC",
                    (status,),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM listings ORDER BY last_seen_at DESC"
                ).fetchall()
            return [dict(row) for row in rows]

    def upsert_listing(
        self, listing: dict[str, Any], observed_at: str | None = None
    ) -> UpsertResult:
        """Сохраняет объявление и возвращает информацию для уведомлений.

        Пустая цена с карточки (None) считается «неизвестной» и не затирает
        уже известную цену. Изменение фиксируется, только когда новая цена
        действительно была прочитана.
        """
        observed_at = observed_at or iso_now()
        listing_id = str(listing["id"])
        incoming_price = listing.get("price")
        if incoming_price is not None:
            incoming_price = int(incoming_price)

        with self._lock, self._conn:
            old_row = self._conn.execute(
                "SELECT * FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            old = dict(old_row) if old_row else None

            if old is None:
                effective_price = incoming_price
                self._conn.execute(
                    """
                    INSERT INTO listings (
                        id, title, price, published_at, city, metro, delivery, url,
                        seller, seller_type, views, first_seen_at, last_seen_at, status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                    """,
                    (
                        listing_id,
                        listing.get("title") or "Без названия",
                        effective_price,
                        listing.get("published_at"),
                        listing.get("city") or "",
                        listing.get("metro") or "",
                        listing.get("delivery") or "unknown",
                        listing.get("url") or "",
                        listing.get("seller") or "",
                        listing.get("seller_type") or "unknown",
                        listing.get("views"),
                        observed_at,
                        observed_at,
                    ),
                )
                self._conn.execute(
                    "INSERT INTO price_history(listing_id, price, observed_at) VALUES (?, ?, ?)",
                    (listing_id, effective_price, observed_at),
                )
                created = True
                price_changed = False
                restored = False
            else:
                old_price = old.get("price")
                effective_price = incoming_price if incoming_price is not None else old_price
                price_changed = (
                    incoming_price is not None and incoming_price != old_price
                )
                restored = old.get("status") == "missing"
                self._conn.execute(
                    """
                    UPDATE listings SET
                        title = ?,
                        price = ?,
                        published_at = COALESCE(?, published_at),
                        city = ?,
                        metro = ?,
                        delivery = ?,
                        url = ?,
                        seller = COALESCE(NULLIF(?, ''), seller),
                        seller_type = ?,
                        views = COALESCE(?, views),
                        last_seen_at = ?,
                        status = 'active'
                    WHERE id = ?
                    """,
                    (
                        listing.get("title") or old.get("title") or "Без названия",
                        effective_price,
                        listing.get("published_at"),
                        listing.get("city") or old.get("city") or "",
                        listing.get("metro") or old.get("metro") or "",
                        listing.get("delivery") or old.get("delivery") or "unknown",
                        listing.get("url") or old.get("url") or "",
                        listing.get("seller") or "",
                        listing.get("seller_type") or old.get("seller_type") or "unknown",
                        listing.get("views"),
                        observed_at,
                        listing_id,
                    ),
                )
                if price_changed:
                    self._conn.execute(
                        "INSERT INTO price_history(listing_id, price, observed_at) VALUES (?, ?, ?)",
                        (listing_id, incoming_price, observed_at),
                    )
                created = False

            row = self._conn.execute(
                "SELECT * FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            result_listing = dict(row)

        return UpsertResult(
            created=created,
            price_changed=price_changed,
            old_price=old.get("price") if old else None,
            new_price=incoming_price,
            restored=restored,
            listing=result_listing,
        )

    def set_listing_status(self, listing_id: str, status: str) -> None:
        if status not in {"active", "missing"}:
            raise ValueError("Недопустимый статус объявления")
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE listings SET status = ? WHERE id = ?", (status, str(listing_id))
            )

    def refresh_listing_status(self, listing_id: str) -> tuple[bool, dict[str, Any] | None]:
        """Пересчитывает глобальный статус по seen_ids.

        Возвращает (произошёл_переход_в_missing, объявление).
        """
        listing_id = str(listing_id)
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM listings WHERE id = ?", (listing_id,)
            ).fetchone()
            if row is None:
                return False, None
            active_seen = self._conn.execute(
                "SELECT 1 FROM seen_ids WHERE listing_id = ? AND status = 'active' LIMIT 1",
                (listing_id,),
            ).fetchone()
            new_status = "active" if active_seen else "missing"
            changed = row["status"] != new_status
            if changed:
                self._conn.execute(
                    "UPDATE listings SET status = ? WHERE id = ?",
                    (new_status, listing_id),
                )
            updated = dict(
                self._conn.execute(
                    "SELECT * FROM listings WHERE id = ?", (listing_id,)
                ).fetchone()
            )
        return changed and new_status == "missing", updated

    def get_price_history(self, listing_id: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM price_history WHERE listing_id = ? ORDER BY observed_at, id",
                (str(listing_id),),
            ).fetchall()
            return [dict(row) for row in rows]

    # ----------------------------- seen_ids -----------------------------

    def record_seen(
        self,
        query_key: str,
        listing_ids: Iterable[str],
        observed_at: str | None = None,
    ) -> None:
        observed_at = observed_at or iso_now()
        ids = [str(item) for item in listing_ids]
        with self._lock, self._conn:
            self._conn.executemany(
                """
                INSERT INTO seen_ids(
                    query_key, listing_id, first_seen_at, last_seen_at, status
                ) VALUES (?, ?, ?, ?, 'active')
                ON CONFLICT(query_key, listing_id) DO UPDATE SET
                    last_seen_at = excluded.last_seen_at,
                    status = 'active'
                """,
                [(query_key, listing_id, observed_at, observed_at) for listing_id in ids],
            )

    def get_seen_ids(self, query_key: str, active_only: bool = True) -> list[str]:
        query = "SELECT listing_id FROM seen_ids WHERE query_key = ?"
        params: list[Any] = [query_key]
        if active_only:
            query += " AND status = 'active'"
        with self._lock:
            rows = self._conn.execute(query, params).fetchall()
            return [row["listing_id"] for row in rows]

    def find_missing_seen_ids(
        self, query_key: str, current_ids: Iterable[str]
    ) -> list[str]:
        """Активные ранее объявления запроса, которых нет в текущей выдаче."""
        current = {str(item) for item in current_ids}
        with self._lock:
            rows = self._conn.execute(
                "SELECT listing_id FROM seen_ids WHERE query_key = ? AND status = 'active'",
                (query_key,),
            ).fetchall()
        return [row["listing_id"] for row in rows if row["listing_id"] not in current]

    def mark_seen_missing(
        self, query_key: str, listing_ids: Iterable[str], observed_at: str | None = None
    ) -> None:
        observed_at = observed_at or iso_now()
        ids = [str(item) for item in listing_ids]
        if not ids:
            return
        with self._lock, self._conn:
            self._conn.executemany(
                "UPDATE seen_ids SET status = 'missing', last_seen_at = ? "
                "WHERE query_key = ? AND listing_id = ?",
                [(observed_at, query_key, listing_id) for listing_id in ids],
            )

    # --------------------------- saved queries --------------------------

    def list_saved_queries(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM saved_queries ORDER BY sort_order, id"
            ).fetchall()
            return [dict(row) for row in rows]

    def create_bot_job(
        self,
        query_id: int,
        chat_id: int,
        username: str = "",
        *,
        mode: str = "pending",
        interval_minutes: int = 0,
    ) -> int:
        now = iso_now()
        with self._lock, self._conn:
            cursor = self._conn.execute(
                """
                INSERT INTO bot_jobs(
                    query_id, chat_id, username, mode, interval_minutes,
                    status, first_run_done, created_at
                ) VALUES (?, ?, ?, ?, ?, 'pending', 0, ?)
                """,
                (int(query_id), int(chat_id), username or "", mode,
                 max(0, int(interval_minutes)), now),
            )
            return int(cursor.lastrowid)

    def list_bot_jobs(
        self, *, chat_id: int | None = None, active_only: bool = False
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM bot_jobs"
        params: list[Any] = []
        clauses: list[str] = []
        if chat_id is not None:
            clauses.append("chat_id = ?")
            params.append(int(chat_id))
        if active_only:
            clauses.append("status IN ('pending', 'running', 'waiting')")
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY id"
        with self._lock:
            return [dict(row) for row in self._conn.execute(query, params).fetchall()]

    def get_bot_job(self, job_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM bot_jobs WHERE id = ?", (int(job_id),)
            ).fetchone()
            return dict(row) if row else None

    def update_bot_job(self, job_id: int, **values: Any) -> None:
        allowed = {
            "mode", "interval_minutes", "status", "first_run_done",
            "last_run_at", "next_run_at",
        }
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        assignments = ", ".join(f"{key} = ?" for key in updates)
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE bot_jobs SET {assignments} WHERE id = ?",
                (*updates.values(), int(job_id)),
            )

    def save_saved_query(
        self,
        query_id: int | None,
        *,
        query: str,
        city: str = "",
        local_priority: bool = False,
        max_listings: int = 50,
        pages: int = 1,
        price_min: int | None = None,
        price_max: int | None = None,
        delivery: str = "any",
        seller_type: str = "any",
        include_words: str = "",
        exclude_words: str = "",
        source_url: str = "",
        avito_sort: int = 0,
        model_mode: str = "strict",
        smart_filter: bool = False,
        category: str = "",
        memory: str = "",
        color: str = "",
        sim: str = "",
        condition: str = "",
        is_active: bool = True,
        notify_new: bool = True,
        notify_price: bool = True,
        notify_old: bool = False,
    ) -> dict[str, Any]:
        """Создаёт или обновляет сохранённый запрос и возвращает строку."""
        if delivery not in {"any", "require", "exclude"}:
            raise ValueError("Недопустимый фильтр доставки")
        if seller_type not in {"any", "private", "company"}:
            raise ValueError("Недопустимый фильтр продавца")
        timestamp = iso_now()
        query_text = (query or "").strip()
        if not query_text:
            raise ValueError("Пустой поисковый запрос")
        max_listings = int(max_listings)
        if not 1 <= max_listings <= 500:
            raise ValueError("Лимит объявлений должен быть от 1 до 500")
        pages = int(pages)
        if not 1 <= pages <= 5:
            raise ValueError("Глубина поиска должна быть от 1 до 5 страниц")
        avito_sort = int(avito_sort or 0)
        if avito_sort not in {0, 1, 2, 104, 172297}:
            raise ValueError("Недопустимая сортировка Avito")
        model_mode = str(model_mode or "strict").lower()
        if model_mode not in {"strict", "broad"}:
            raise ValueError("Недопустимый режим модели")
        values = (
            query_text,
            (city or "").strip(),
            1 if local_priority else 0,
            max_listings,
            pages,
            int(price_min) if price_min is not None else None,
            int(price_max) if price_max is not None else None,
            delivery,
            seller_type,
            include_words or "",
            exclude_words or "",
            (source_url or "").strip(),
            avito_sort,
            model_mode,
            1 if smart_filter else 0,
            category or "",
            memory or "",
            color or "",
            sim or "",
            condition or "",
            1 if is_active else 0,
            1 if notify_new else 0,
            1 if notify_price else 0,
            1 if notify_old else 0,
        )
        with self._lock, self._conn:
            row = None
            if query_id is not None:
                row = self._conn.execute(
                    "SELECT * FROM saved_queries WHERE id = ?", (int(query_id),)
                ).fetchone()
            if row is None:
                next_order = self._conn.execute(
                    "SELECT COALESCE(MAX(sort_order), 0) + 1 AS n FROM saved_queries"
                ).fetchone()["n"]
                cursor = self._conn.execute(
                    """
                    INSERT INTO saved_queries(
                        query, city, local_priority, max_listings, pages, price_min,
                        price_max, delivery, seller_type, include_words, exclude_words,
                        source_url, primed, sort_order, avito_sort, model_mode,
                        smart_filter, category, memory, color, sim, condition,
                        is_active, notify_new, notify_price, notify_old,
                        created_at, updated_at
                    ) VALUES (
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        *values[:12],
                        int(next_order),
                        *values[12:],
                        timestamp,
                        timestamp,
                    ),
                )
                query_id = int(cursor.lastrowid)
            else:
                self._conn.execute(
                    """
                    UPDATE saved_queries SET
                        query = ?,
                        city = ?,
                        local_priority = ?,
                        max_listings = ?,
                        pages = ?,
                        price_min = ?,
                        price_max = ?,
                        delivery = ?,
                        seller_type = ?,
                        include_words = ?,
                        exclude_words = ?,
                        source_url = ?,
                        avito_sort = ?,
                        model_mode = ?,
                        smart_filter = ?,
                        category = ?,
                        memory = ?,
                        color = ?,
                        sim = ?,
                        condition = ?,
                        is_active = ?,
                        notify_new = ?,
                        notify_price = ?,
                        notify_old = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (*values, timestamp, int(query_id)),
                )
            saved = self._conn.execute(
                "SELECT * FROM saved_queries WHERE id = ?", (int(query_id),)
            ).fetchone()
            return dict(saved)

    def mark_query_primed(self, query_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saved_queries SET primed = 1 WHERE id = ?",
                (int(query_id),),
            )

    def delete_saved_query(self, query_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM saved_queries WHERE id = ?", (int(query_id),)
            )

    def set_query_origin(self, query_id: int, origin: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE saved_queries SET origin = ?, updated_at = ? WHERE id = ?",
                (str(origin), iso_now(), int(query_id)),
            )

    def count_saved_queries(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM saved_queries"
            ).fetchone()
            return int(row["n"])

    # ---------------------------- subscribers ---------------------------

    @staticmethod
    def _clean_username(username: str | None) -> str:
        return (username or "").strip().lstrip("@")

    def upsert_subscriber(
        self,
        chat_id: int,
        username: str | None = None,
        first_name: str | None = None,
    ) -> dict[str, Any]:
        timestamp = iso_now()
        chat_id = int(chat_id)
        username_clean = self._clean_username(username)
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            if existing is None:
                self._conn.execute(
                    """
                    INSERT INTO subscribers(
                        chat_id, username, first_name, subscribed_at, updated_at, is_active
                    ) VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    (chat_id, username_clean, (first_name or "").strip(), timestamp, timestamp),
                )
            else:
                self._conn.execute(
                    """
                    UPDATE subscribers SET
                        username = CASE WHEN ? <> '' THEN ? ELSE username END,
                        first_name = CASE WHEN ? <> '' THEN ? ELSE first_name END,
                        updated_at = ?,
                        is_active = 1
                    WHERE chat_id = ?
                    """,
                    (
                        username_clean,
                        username_clean,
                        (first_name or "").strip(),
                        (first_name or "").strip(),
                        timestamp,
                        chat_id,
                    ),
                )
            row = self._conn.execute(
                "SELECT * FROM subscribers WHERE chat_id = ?", (chat_id,)
            ).fetchone()
            return dict(row)

    def get_subscriber(self, chat_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM subscribers WHERE chat_id = ?", (int(chat_id),)
            ).fetchone()
            return dict(row) if row else None

    def get_subscribers(self, active_only: bool = True) -> list[dict[str, Any]]:
        query = "SELECT * FROM subscribers"
        if active_only:
            query += " WHERE is_active = 1"
        query += " ORDER BY lower(username), chat_id"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
            return [dict(row) for row in rows]

    def set_subscriber_active(self, chat_id: int, active: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE subscribers SET is_active = ?, updated_at = ? WHERE chat_id = ?",
                (1 if active else 0, iso_now(), int(chat_id)),
            )

    @staticmethod
    def subscriber_label(subscriber: dict[str, Any]) -> str:
        username = (subscriber.get("username") or "").strip()
        if username:
            return f"@{username}"
        return f"@id{subscriber['chat_id']}"

    # ------------------------------ stats -------------------------------

    def get_query_listings(self, query_key: str) -> list[dict[str, Any]]:
        """Активные объявления, которые встречались в конкретном запросе."""
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT l.*
                FROM listings l
                JOIN seen_ids s ON s.listing_id = l.id
                WHERE s.query_key = ? AND l.status = 'active'
                ORDER BY l.price IS NULL, l.price ASC, l.last_seen_at DESC
                """,
                (str(query_key),),
            ).fetchall()
            return [dict(row) for row in rows]

    def get_stats(self) -> dict[str, int]:
        with self._lock:
            listings = self._conn.execute(
                "SELECT COUNT(*) AS n FROM listings"
            ).fetchone()["n"]
            active = self._conn.execute(
                "SELECT COUNT(*) AS n FROM listings WHERE status = 'active'"
            ).fetchone()["n"]
            missing = self._conn.execute(
                "SELECT COUNT(*) AS n FROM listings WHERE status = 'missing'"
            ).fetchone()["n"]
            no_price = self._conn.execute(
                "SELECT COUNT(*) AS n FROM listings WHERE price IS NULL"
            ).fetchone()["n"]
            subscribers = self._conn.execute(
                "SELECT COUNT(*) AS n FROM subscribers WHERE is_active = 1"
            ).fetchone()["n"]
        return {
            "listings": int(listings),
            "active": int(active),
            "missing": int(missing),
            "no_price": int(no_price),
            "subscribers": int(subscribers),
        }

    # ------------------------------- meta -------------------------------

    def set_favorite(self, listing_id: str, favorite: bool = True) -> None:
        with self._lock, self._conn:
            if favorite:
                self._conn.execute(
                    "INSERT OR REPLACE INTO favorites(listing_id, created_at) VALUES (?, ?)",
                    (str(listing_id), iso_now()),
                )
            else:
                self._conn.execute(
                    "DELETE FROM favorites WHERE listing_id = ?",
                    (str(listing_id),),
                )

    def list_favorite_ids(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute("SELECT listing_id FROM favorites").fetchall()
        return {str(row["listing_id"]) for row in rows}

    def is_favorite(self, listing_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM favorites WHERE listing_id = ?",
                (str(listing_id),),
            ).fetchone()
        return row is not None

    def blacklist_seller(self, seller: str) -> None:
        value = str(seller or "").strip()
        if not value:
            return
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO seller_blacklist(seller, created_at) VALUES (?, ?)",
                (value, iso_now()),
            )

    def list_blacklisted_sellers(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seller FROM seller_blacklist ORDER BY seller"
            ).fetchall()
        return [str(row["seller"]) for row in rows]

    def remove_blacklisted_seller(self, seller: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM seller_blacklist WHERE seller = ?",
                (str(seller or "").strip(),),
            )

    def is_seller_blacklisted(self, seller: str) -> bool:
        value = str(seller or "").strip()
        if not value:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM seller_blacklist WHERE seller = ?",
                (value,),
            ).fetchone()
        return row is not None

    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM meta WHERE key = ?", (str(key),)
            ).fetchone()
            return str(row["value"]) if row else default

    def save_digest(self, digest_id: str, payload: dict[str, Any]) -> None:
        timestamp = iso_now()
        with self._lock, self._conn:
            self._conn.execute(
                "DELETE FROM digests WHERE datetime(created_at) < "
                "datetime('now', '-7 days')"
            )
            self._conn.execute(
                "INSERT OR REPLACE INTO digests(id, payload, created_at) "
                "VALUES (?, ?, ?)",
                (
                    str(digest_id),
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    timestamp,
                ),
            )

    def get_digest(self, digest_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT payload FROM digests WHERE id = ?", (str(digest_id),)
            ).fetchone()
        if not row:
            return None
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO meta(key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(key), str(value)),
            )

    def checkpoint(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
