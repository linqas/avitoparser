"""XLSX-отчёт и сортируемая таблица результатов Avito Radar."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Callable, Iterable

import customtkinter as ctk
import tkinter as tk
from tkinter import messagebox, ttk

from utils import (
    format_datetime,
    format_delivery,
    format_location,
    format_price_short,
    open_listing_url,
)

logger = logging.getLogger("avito_radar.reporter")

SORT_LABELS = {
    "Цена ↑": "price_asc",
    "Цена ↓": "price_desc",
    "Дата": "date",
    "%Выгоды от рынка": "market_diff",
}
SORT_DEFAULT = "market_diff"

_EXCEL_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class ReporterError(RuntimeError):
    pass


def _sort_date_key(listing: dict[str, Any]) -> tuple[int, str]:
    published = listing.get("published_at") or ""
    # Сначала самые свежие. Неизвестная дата оказывается внизу.
    return (1 if published else 0, published)


def split_and_sort(
    listings: Iterable[dict[str, Any]], sort_key: str = "date"
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Возвращает две группы: с ценой и без цены.

    Объявления без цены никогда не участвуют в сортировке по цене и остаются
    отдельной группой. Внутри группы без цены — сортировка по дате.
    """
    all_items = list(listings)
    priced = [item for item in all_items if item.get("price") is not None]
    no_price = [item for item in all_items if item.get("price") is None]
    if sort_key in {"price_asc", "price_desc"}:
        # Стабильная сортировка: при равной цене — сначала более свежие.
        priced.sort(key=_sort_date_key, reverse=True)
        priced.sort(
            key=lambda item: int(item["price"]),
            reverse=sort_key == "price_desc",
        )
    elif sort_key == "market_diff":
        priced.sort(key=_sort_date_key, reverse=True)
        priced.sort(
            key=lambda item: (
                1 if not isinstance(item.get("market_diff"), (int, float)) else 0,
                float(item.get("market_diff") or 0),
            )
        )
    else:
        sort_key = "date"
        priced.sort(key=_sort_date_key, reverse=True)
    no_price.sort(key=_sort_date_key, reverse=True)
    return priced, no_price


def _status_label(value: str) -> str:
    return "снято" if value == "missing" else "активно"


def _plural_ru(value: int, one: str, few: str, many: str) -> str:
    number = abs(int(value)) % 100
    if 11 <= number <= 14:
        return many
    number %= 10
    if number == 1:
        return one
    if 2 <= number <= 4:
        return few
    return many


def _safe_excel(value: Any) -> Any:
    if isinstance(value, str):
        return _EXCEL_ILLEGAL.sub("", value)
    return value


def _style_sheet(ws, headers: list[str], widths: list[int]) -> None:
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    fill = PatternFill("solid", fgColor="1F4E79")
    font = Font(bold=True, color="FFFFFF")
    for column, header in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=column, value=header)
        cell.fill = fill
        cell.font = font
        cell.alignment = Alignment(vertical="center")
        ws.column_dimensions[get_column_letter(column)].width = widths[column - 1]
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
    ws.row_dimensions[1].height = 24


def _write_listing_rows(
    ws,
    listings: list[dict[str, Any]],
    headers: list[str],
    widths: list[int],
    *,
    no_price: bool = False,
) -> None:
    from openpyxl.styles import Alignment, Font

    _style_sheet(ws, headers, widths)
    link_font = Font(color="0563C1", underline="single")
    for row_index, listing in enumerate(listings, start=2):
        values = [
            listing.get("title", ""),
            "Не указана" if no_price else listing.get("price"),
            format_datetime(listing.get("published_at")),
            format_location(listing),
            format_delivery(listing.get("delivery") or "unknown"),
            _status_label(listing.get("status") or "active"),
            listing.get("url") or "",
        ]
        for column, value in enumerate(values, start=1):
            cell = ws.cell(row=row_index, column=column, value=_safe_excel(value))
            cell.alignment = Alignment(vertical="top", wrap_text=column in {1, 4})
        if not no_price and listing.get("price") is not None:
            ws.cell(row=row_index, column=2).number_format = '#,##0 "₽"'
        url = str(listing.get("url") or "")
        if url.startswith(("http://", "https://")):
            url_cell = ws.cell(row=row_index, column=7)
            url_cell.hyperlink = url
            url_cell.font = link_font
    if ws.max_row > 1:
        ws.auto_filter.ref = f"A1:G{ws.max_row}"


def export_xlsx(
    listings: Iterable[dict[str, Any]],
    output_path: str | Path,
    *,
    sort_key: str = "date",
    price_history: dict[str, list[dict[str, Any]]] | None = None,
    rejected: Iterable[dict[str, Any]] | None = None,
) -> Path:
    """Создаёт XLSX с отдельными листами для цен и объявлений без цены."""
    from openpyxl import Workbook

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    priced, no_price = split_and_sort(listings, sort_key)
    headers = [
        "Заголовок",
        "Цена",
        "Опубликовано",
        "Город / метро",
        "Авито Доставка",
        "Статус",
        "Ссылка",
    ]
    widths = [48, 15, 18, 30, 16, 10, 60]

    workbook = Workbook()
    sheet_priced = workbook.active
    sheet_priced.title = "С ценой"
    _write_listing_rows(sheet_priced, priced, headers, widths)

    sheet_no_price = workbook.create_sheet("Без цены")
    _write_listing_rows(sheet_no_price, no_price, headers, widths, no_price=True)

    rejected_list = list(rejected or [])
    if rejected_list:
        rejected_sheet = workbook.create_sheet("Не подходящие")
        rejected_headers = [
            "Запрос",
            "Заголовок",
            "Цена",
            "Город",
            "Причина",
        ]
        rejected_widths = [22, 45, 14, 24, 52]
        _style_sheet(rejected_sheet, rejected_headers, rejected_widths)
        for row_index, entry in enumerate(rejected_list, start=2):
            listing = entry.get("listing") or {}
            values = [
                entry.get("query") or "",
                listing.get("title", "") or "Без названия",
                (
                    "Не указана"
                    if listing.get("price") is None
                    else listing.get("price")
                ),
                format_location(listing),
                entry.get("reason") or "",
            ]
            for column, value in enumerate(values, start=1):
                rejected_sheet.cell(
                    row=row_index, column=column, value=_safe_excel(value)
                )
            if listing.get("price") is not None:
                rejected_sheet.cell(row=row_index, column=3).number_format = (
                    '#,##0 "₽"'
                )
        rejected_sheet.auto_filter.ref = f"A1:E{rejected_sheet.max_row}"

    if price_history:
        history_sheet = workbook.create_sheet("История цен")
        history_headers = ["Заголовок", "Цена", "Когда замечено"]
        history_widths = [55, 15, 22]
        _style_sheet(history_sheet, history_headers, history_widths)
        row = 2
        for listing in [*priced, *no_price]:
            listing_id = str(listing.get("id"))
            for entry in price_history.get(listing_id, []):
                history_sheet.cell(
                    row=row, column=1, value=_safe_excel(listing.get("title") or "")
                )
                price = entry.get("price")
                price_cell = history_sheet.cell(
                    row=row, column=2, value="Не указана" if price is None else price
                )
                if price is not None:
                    price_cell.number_format = '#,##0 "₽"'
                history_sheet.cell(
                    row=row, column=3, value=format_datetime(entry.get("observed_at"))
                )
                row += 1
        if row > 2:
            history_sheet.auto_filter.ref = f"A1:C{row - 1}"

    try:
        workbook.save(output)
    except PermissionError as exc:
        raise ReporterError(
            f"Не удалось записать файл «{output.name}». Закройте его в Excel и повторите."
        ) from exc
    except Exception as exc:
        raise ReporterError(f"Ошибка экспорта XLSX: {exc}") from exc
    logger.info(
        "XLSX сохранён: %s (с ценой: %s, без цены: %s)",
        output,
        len(priced),
        len(no_price),
    )
    return output


class ListingsTable(ctk.CTkFrame):
    """Таблица на ttk.Treeview внутри CustomTkinter с группой «Без цены»."""

    COLUMNS = (
        ("link", "Ссылка", 52, "center"),
        ("title", "Заголовок", 210, "w"),
        ("price", "Цена", 62, "e"),
        ("market_diff", "От рынка", 92, "center"),
        ("city", "Город", 95, "w"),
        ("delivery", "Доставка", 64, "center"),
        ("published", "Опубликовано", 96, "center"),
        ("favorite", "★", 28, "center"),
    )

    def __init__(
        self,
        master: Any,
        *,
        on_select: Callable[[dict[str, Any] | None], None] | None = None,
        **kwargs: Any,
    ):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_select = on_select
        self._listings: list[dict[str, Any]] = []
        self._rejected: list[dict[str, Any]] = []
        self._query_key = ""
        self._rejected_open = True
        self._rejected_iids: list[str] = []
        self._quick_filter = ""
        self._view_filters: dict[str, Any] = {}
        self._sort_key = SORT_DEFAULT
        self._urls: dict[str, str] = {}
        self._by_iid: dict[str, dict[str, Any]] = {}
        self._refresh_job: str | None = None

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        self.title_label = ctk.CTkLabel(
            top,
            text="Результаты цикла",
            font=ctk.CTkFont(size=16, weight="bold"),
        )
        self.title_label.pack(side="left")
        self.sort_segment = ctk.CTkSegmentedButton(
            top,
            values=list(SORT_LABELS),
            command=self._on_sort,
            width=390,
        )
        self.sort_segment.set("%Выгоды от рынка")
        self.sort_segment.pack(side="right")
        self.count_label = ctk.CTkLabel(
            top, text="0 объявлений", text_color=("#4a4a4a", "#b8b8b8")
        )
        self.count_label.pack(side="right", padx=(0, 12))
        self.copy_hint_label = ctk.CTkLabel(
            top,
            text="ПКМ — копировать ссылку",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
        )
        self.copy_hint_label.pack(side="right", padx=(0, 10))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", pady=(0, 4))
        self.open_button = ctk.CTkButton(
            actions,
            text="Открыть выбранное",
            width=150,
            height=27,
            command=self._open_selected,
            state="disabled",
        )
        self.open_button.pack(side="left")
        ctk.CTkLabel(
            actions,
            text="или двойной клик по строке",
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
        ).pack(side="left", padx=(8, 0))

        container = tk.Frame(self, bg="#1d1e1e", highlightthickness=0)
        container.pack(fill="both", expand=True)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Avito.Treeview",
            background="#1d1e1e",
            foreground="#e8e8e8",
            fieldbackground="#1d1e1e",
            bordercolor="#343638",
            rowheight=24,
            font=("Segoe UI", 8),
        )
        style.configure(
            "Avito.Treeview.Heading",
            background="#2b2d2e",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
        )
        style.map(
            "Avito.Treeview",
            background=[("selected", "#1f538d")],
            foreground=[("selected", "#ffffff")],
        )
        style.map("Avito.Treeview.Heading", background=[("active", "#3a3d3e")])

        self.tree = ttk.Treeview(
            container,
            columns=[column[0] for column in self.COLUMNS],
            show="headings",
            style="Avito.Treeview",
            selectmode="browse",
        )
        self.tree.tag_configure("bargain", foreground="#64e08f")
        self.tree.tag_configure("below_market", foreground="#a4d98b")
        self.tree.tag_configure("above_market", foreground="#e2b36d")
        self.tree.tag_configure("expensive", foreground="#ff8585")

        for key, heading, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(
                key,
                width=width,
                minwidth=width,
                anchor=anchor,
                stretch=key == "title",
            )
        vertical = ctk.CTkScrollbar(
            container,
            orientation="vertical",
            command=self.tree.yview,
            width=14,
            fg_color="#1d1e1e",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        horizontal = ctk.CTkScrollbar(
            container,
            orientation="horizontal",
            command=self.tree.xview,
            height=14,
            fg_color="#1d1e1e",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        horizontal.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)

        self.tree.tag_configure(
            "group", background="#2b2d2e", foreground="#d9e7f5", font=("Segoe UI", 9, "bold")
        )
        self.tree.tag_configure(
            "group_rejected",
            background="#3a2626",
            foreground="#ffc4c4",
            font=("Segoe UI", 9, "bold"),
        )
        self.tree.tag_configure("rejected", foreground="#c98b8b")
        self.tree.tag_configure("no_price", foreground="#cfcfcf")
        self.tree.tag_configure("missing", foreground="#c77b7b")
        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<Button-1>", self._on_left_click)
        self.tree.bind("<Button-3>", self._open_context_menu)
        self.tree.bind("<Control-c>", lambda _e: self._copy_selected())
        self.tree.bind("<Motion>", self._on_motion)
        self.tree.bind("<Leave>", lambda _e: self.tree.configure(cursor=""))
        self.tree.bind("<<TreeviewSelect>>", self._on_selection_change)
        self._context_iid: str | None = None
        self._context_menu = tk.Menu(
            self,
            tearoff=0,
            bg="#2b2d2e",
            fg="#e8e8e8",
            activebackground="#1f538d",
            activeforeground="#ffffff",
            bd=0,
        )
        self._context_menu.add_command(
            label="Открыть объявление", command=self._open_context_url
        )
        self._context_menu.add_command(
            label="Копировать ссылку", command=self._copy_context_url
        )
        self._context_menu.add_command(
            label="Подробнее", command=self._show_context_details
        )

    def _on_sort(self, label: str) -> None:
        self._sort_key = SORT_LABELS.get(label, "date")
        self.refresh()

    @property
    def sort_key(self) -> str:
        return self._sort_key

    def set_sort_key(self, sort_key: str) -> None:
        self._sort_key = (
            sort_key
            if sort_key in {"price_asc", "price_desc", "date", "market_diff"}
            else "market_diff"
        )
        reverse = {value: label for label, value in SORT_LABELS.items()}
        self.sort_segment.set(reverse.get(self._sort_key, "%Выгоды от рынка"))
        self.refresh()

    def set_listings(self, listings: Iterable[dict[str, Any]]) -> None:
        self._listings = list(listings)
        self.refresh_immediate()

    def set_data(
        self,
        listings: Iterable[dict[str, Any]],
        rejected: Iterable[dict[str, Any]] = (),
    ) -> None:
        self._listings = list(listings)
        self._rejected = list(rejected)
        self.refresh_immediate()

    def set_query_key(self, query_key: str) -> None:
        self._query_key = str(query_key or "")
        self.refresh_immediate()

    def set_title(self, text: str) -> None:
        if hasattr(self, "title_label"):
            self.title_label.configure(text=str(text or "Результаты"))

    def set_quick_filter(self, text: str) -> None:
        self._quick_filter = (text or "").strip().casefold()
        self.refresh()

    def set_view_filters(
        self,
        *,
        price_min: int | None = None,
        price_max: int | None = None,
        only_delivery: bool = False,
        min_discount: float = 0.0,
        only_favorite: bool = False,
    ) -> None:
        self._view_filters = {
            "price_min": price_min,
            "price_max": price_max,
            "only_delivery": bool(only_delivery),
            "min_discount": max(0.0, float(min_discount)),
            "only_favorite": bool(only_favorite),
        }
        self.refresh()

    def refresh(self) -> None:
        """Отложенная перерисовка: при быстром вводе не дёргает таблицу."""
        try:
            if self._refresh_job is not None:
                self.after_cancel(self._refresh_job)
            self._refresh_job = self.after(80, self._refresh_now)
        except Exception:
            self._refresh_now()

    def refresh_immediate(self) -> None:
        try:
            if self._refresh_job is not None:
                self.after_cancel(self._refresh_job)
                self._refresh_job = None
        except Exception:
            pass
        self._refresh_now()

    def _refresh_now(self) -> None:
        self._refresh_job = None
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self._urls.clear()
        self._by_iid.clear()
        self.open_button.configure(state="disabled")

        def matches(listing: dict[str, Any]) -> bool:
            if not self._quick_filter:
                return True
            hay = (
                str(listing.get("title") or "")
                + " "
                + str(listing.get("city") or "")
                + " "
                + str(listing.get("metro") or "")
            ).casefold()
            return self._quick_filter in hay

        visible = [listing for listing in self._listings if matches(listing)]
        if self._query_key:
            visible = [
                listing
                for listing in visible
                if listing.get("_query_key") == self._query_key
            ]
        filters = self._view_filters
        if filters:
            price_min = filters.get("price_min")
            price_max = filters.get("price_max")
            only_delivery = filters.get("only_delivery")
            only_favorite = filters.get("only_favorite")
            min_discount = float(filters.get("min_discount") or 0.0)
            result = []
            for listing in visible:
                price = listing.get("price")
                if price_min is not None and (
                    price is None or int(price) < int(price_min)
                ):
                    continue
                if price_max is not None and (
                    price is None or int(price) > int(price_max)
                ):
                    continue
                if only_delivery and listing.get("delivery") != "yes":
                    continue
                if only_favorite and not listing.get("is_favorite"):
                    continue
                market_diff = listing.get("market_diff")
                if min_discount > 0:
                    if not isinstance(market_diff, (int, float)):
                        continue
                    if (-float(market_diff)) < min_discount:
                        continue
                result.append(listing)
            visible = result

        rejected = [
            entry
            for entry in self._rejected
            if matches(entry.get("listing") or {})
            and (
                not self._query_key
                or entry.get("query_key") == self._query_key
                or (entry.get("listing") or {}).get("_query_key") == self._query_key
            )
        ]

        def sorted_group(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            priced, no_price = split_and_sort(items, self._sort_key)
            return priced + no_price

        city_items = [item for item in visible if item.get("_scope") == "city"]
        other_items = [item for item in visible if item.get("_scope") == "other"]
        all_items = [
            item for item in visible if item.get("_scope") not in {"city", "other"}
        ]
        groups: list[tuple[str, str, list[dict[str, Any]]]] = []
        if city_items:
            groups.append(("group_city", f"В вашем городе · {len(city_items)}", city_items))
        if other_items:
            groups.append(("group_other", f"Другие города · {len(other_items)}", other_items))
        if all_items:
            groups.append(("group_all", f"Все объявления · {len(all_items)}", all_items))

        count = len(visible)
        count_text = (
            f"{count} {_plural_ru(count, 'объявление', 'объявления', 'объявлений')}"
        )
        if rejected:
            count_text += f" · не подходят: {len(rejected)}"
        self.count_label.configure(text=count_text)

        for group_id, title, items in groups:
            self._insert_group(group_id, title, sorted_group(items))
        if rejected:
            self._insert_rejected_group(rejected)

    def _insert_group(
        self,
        group_id: str,
        title: str,
        listings: list[dict[str, Any]],
    ) -> None:
        self.tree.insert(
            "",
            "end",
            iid=group_id,
            values=("", f"— {title} —", "", "", "", "", "", ""),
            tags=("group",),
        )
        for listing in listings:
            self._insert_listing(listing)

    def _insert_rejected_group(self, entries: list[dict[str, Any]]) -> None:
        arrow = "▾" if self._rejected_open else "▸"
        self.tree.insert(
            "",
            "end",
            iid="group_rejected",
            values=(
                "",
                f"{arrow} Не подходят · {len(entries)}",
                "",
                "",
                "",
                "",
                "",
                "",
            ),
            tags=("group_rejected",),
        )
        self._rejected_iids = []
        for index, entry in enumerate(entries):
            self._insert_rejected(entry, index)
        if not self._rejected_open:
            for iid in self._rejected_iids:
                self.tree.detach(iid)

    def _insert_listing(self, listing: dict[str, Any]) -> None:
        iid = f"item_{listing.get('id')}"
        tags: list[str] = []
        if listing.get("price") is None:
            tags.append("no_price")
        if listing.get("status") == "missing":
            tags.append("missing")
        market_diff = listing.get("market_diff")
        if isinstance(market_diff, (int, float)):
            if market_diff <= -0.10:
                tags.append("bargain")
            elif market_diff <= -0.04:
                tags.append("below_market")
            elif market_diff >= 0.10:
                tags.append("expensive")
            elif market_diff >= 0.04:
                tags.append("above_market")
        self.tree.insert(
            "",
            "end",
            iid=iid,
            values=(
                "Открыть" if listing.get("url") else "—",
                listing.get("title", "") or "Без названия",
                format_price_short(listing.get("price")),
                (
                    f"{listing['market_diff']:+.0%}"
                    if isinstance(listing.get("market_diff"), (int, float))
                    else "Мало данных"
                ),
                format_location(listing),
                format_delivery(listing.get("delivery") or "unknown"),
                format_datetime(listing.get("published_at")),
                "★" if listing.get("is_favorite") else "",
            ),
            tags=tuple(tags),
        )
        if listing.get("url"):
            self._urls[iid] = str(listing["url"])
        self._by_iid[iid] = listing

    def _insert_rejected(self, entry: dict[str, Any], index: int) -> None:
        listing = dict(entry.get("listing") or {})
        reason = str(entry.get("reason") or "")
        listing["_reject_reason"] = reason
        listing_id = str(listing.get("id") or f"rejected_{index}")
        iid = f"rejected_{listing_id}"
        self.tree.insert(
            "",
            "end",
            iid=iid,
            values=(
                "Открыть" if listing.get("url") else "—",
                listing.get("title", "") or "Без названия",
                format_price_short(listing.get("price")),
                (
                    f"{listing['market_diff']:+.0%}"
                    if isinstance(listing.get("market_diff"), (int, float))
                    else "Мало данных"
                ),
                format_location(listing),
                format_delivery(listing.get("delivery") or "unknown"),
                format_datetime(listing.get("published_at")),
                "★" if listing.get("is_favorite") else "",
            ),
            tags=("rejected",),
        )
        if listing.get("url"):
            self._urls[iid] = str(listing["url"])
        self._by_iid[iid] = listing
        self._rejected_iids.append(iid)

    def _toggle_rejected(self) -> None:
        self._rejected_open = not self._rejected_open
        arrow = "▾" if self._rejected_open else "▸"
        count = len(self._rejected_iids)
        self.tree.item(
            "group_rejected",
            values=("", f"{arrow} Не подходят · {count}", "", "", "", "", "", ""),
        )
        for iid in self._rejected_iids:
            if self._rejected_open:
                self.tree.reattach(iid, "", "end")
            else:
                self.tree.detach(iid)

    def _on_left_click(self, event: Any) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        iid = self.tree.identify_row(event.y)
        if iid == "group_rejected":
            self._toggle_rejected()
            return "break"
        if self.tree.identify_column(event.x) != "#1":
            return
        if iid and iid in self._urls:
            self.tree.selection_set(iid)
            self._open_url(iid)

    def _on_selection_change(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        enabled = bool(selection and selection[0] in self._by_iid)
        self.open_button.configure(
            state="normal" if enabled and selection[0] in self._urls else "disabled"
        )
        if self.on_select is not None:
            listing = self._by_iid.get(selection[0]) if selection else None
            self.on_select(listing)

    def _on_motion(self, event: Any) -> None:
        over_link = (
            self.tree.identify_region(event.x, event.y) == "cell"
            and self.tree.identify_column(event.x) == "#1"
            and self.tree.identify_row(event.y) in self._urls
        )
        self.tree.configure(cursor="hand2" if over_link else "")

    def _open_context_menu(self, event: Any) -> None:
        iid = self.tree.identify_row(event.y)
        if not iid or iid not in self._urls:
            return
        self._context_iid = iid
        self.tree.selection_set(iid)
        state = "normal" if iid in self._urls else "disabled"
        self._context_menu.entryconfigure(0, state=state)
        self._context_menu.entryconfigure(1, state=state)
        try:
            self._context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self._context_menu.grab_release()

    def _open_context_url(self) -> None:
        if self._context_iid:
            self._open_url(self._context_iid)

    def _copy_context_url(self) -> None:
        if self._context_iid:
            self._copy_url(self._context_iid)

    def _show_context_details(self) -> None:
        if not self._context_iid:
            return
        listing = self._by_iid.get(self._context_iid) or {}
        window = tk.Toplevel(self)
        window.title("Объявление")
        window.geometry("560x340")
        window.transient(self.winfo_toplevel())
        text = "\n".join(
            [
                f"Заголовок: {listing.get('title') or '—'}",
                f"Цена: {format_price_short(listing.get('price'))}",
                f"От рынка: {listing.get('market_diff'):+.0%}"
                if isinstance(listing.get("market_diff"), (int, float))
                else "От рынка: —",
                f"Город: {format_location(listing) or '—'}",
                f"Доставка: {format_delivery(listing.get('delivery') or 'unknown')}",
                f"Опубликовано: {format_datetime(listing.get('published_at'))}",
                f"Ссылка: {listing.get('url') or '—'}",
            ]
        )
        label = ctk.CTkLabel(
            window,
            text=text,
            justify="left",
            anchor="nw",
            wraplength=520,
        )
        label.pack(fill="both", expand=True, padx=16, pady=16)
        ctk.CTkButton(
            window,
            text="Открыть в браузере",
            command=lambda: self._open_url(self._context_iid or ""),
        ).pack(pady=(0, 14))

    def _copy_selected(self) -> None:
        selection = self.tree.selection()
        if selection:
            self._copy_url(selection[0])

    def _copy_url(self, iid: str) -> None:
        url = self._urls.get(iid)
        if not url:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(url)
            self.update_idletasks()
            self.copy_hint_label.configure(
                text="Ссылка скопирована", text_color=("#1f6b38", "#8fe0a8")
            )
            self.after(
                1600,
                lambda: self.copy_hint_label.configure(
                    text="ПКМ — копировать ссылку",
                    text_color=("#777777", "#8f8f8f"),
                ),
            )
            logger.info("Ссылка на объявление скопирована")
        except Exception:
            logger.debug("Не удалось скопировать ссылку", exc_info=True)

    def _open_url(self, iid: str) -> None:
        url = self._urls.get(iid)
        if not url:
            return
        logger.info("Открываю объявление: %s", url)
        try:
            opened = open_listing_url(url)
            if not opened:
                logger.warning("Не удалось открыть объявление: %s", url)
                messagebox.showwarning(
                    "Avito Radar",
                    "Не удалось открыть объявление в браузере по умолчанию.\n"
                    "Ссылка скопирована в буфер обмена — вставьте её в браузер.",
                )
                self.clipboard_clear()
                self.clipboard_append(url)
        except Exception as exc:
            logger.debug("Не удалось открыть ссылку", exc_info=True)
            messagebox.showerror(
                "Avito Radar", f"Не удалось открыть объявление:\n{exc}"
            )

    def _open_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            logger.info("Сначала выберите объявление в таблице")
            return
        self._open_url(selection[0])


class MarketTable(ctk.CTkFrame):
    """Сравнение цен по группам «модель + память»."""

    COLUMNS = (
        ("query", "Запрос", 115, "w"),
        ("title", "Заголовок", 320, "w"),
        ("price", "Цена", 68, "e"),
        ("market", "Рынок", 72, "e"),
        ("delta", "От рынка", 58, "center"),
        ("notes", "Заметки", 190, "w"),
        ("link", "Ссылка", 55, "center"),
    )

    def __init__(self, master: Any, **kwargs: Any):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._urls: dict[str, str] = {}

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            top,
            text="Аналитика рынка",
            font=ctk.CTkFont(size=16, weight="bold"),
        ).pack(side="left")
        self.count_label = ctk.CTkLabel(
            top, text="0 объявлений", text_color=("#4a4a4a", "#b8b8b8")
        )
        self.count_label.pack(side="right")
        self.sort_segment = ctk.CTkSegmentedButton(
            top, values=list(SORT_LABELS), command=self._on_sort, width=390
        )
        self.sort_segment.set("%Выгоды от рынка")
        self.sort_segment.pack(side="right", padx=(0, 10))
        self._rows: list[dict[str, Any]] = []
        self._query_key = ""
        self._sort_key = "market_diff"

        ctk.CTkLabel(
            self,
            text=(
                "Медиана по активным объявлениям из базы. В группе нужно "
                "минимум 3 объявления с ценой; выбросы отбрасываются."
            ),
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#8f8f8f"),
            anchor="w",
            justify="left",
        ).pack(fill="x", pady=(0, 4))

        container = tk.Frame(self, bg="#1d1e1e", highlightthickness=0)
        container.pack(fill="both", expand=True)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "Market.Treeview",
            background="#1d1e1e",
            foreground="#e8e8e8",
            fieldbackground="#1d1e1e",
            bordercolor="#343638",
            rowheight=24,
            font=("Segoe UI", 8),
        )
        style.configure(
            "Market.Treeview.Heading",
            background="#2b2d2e",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
        )
        style.map(
            "Market.Treeview",
            background=[("selected", "#1f538d")],
            foreground=[("selected", "#ffffff")],
        )
        style.map("Market.Treeview.Heading", background=[("active", "#3a3d3e")])

        self.tree = ttk.Treeview(
            container,
            columns=[column[0] for column in self.COLUMNS],
            show="headings",
            style="Market.Treeview",
            selectmode="browse",
        )
        for key, heading, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(
                key,
                width=width,
                minwidth=width,
                anchor=anchor,
                stretch=key == "title",
            )
        vertical = ctk.CTkScrollbar(
            container,
            orientation="vertical",
            command=self.tree.yview,
            width=14,
            fg_color="#1d1e1e",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        horizontal = ctk.CTkScrollbar(
            container,
            orientation="horizontal",
            command=self.tree.xview,
            height=14,
            fg_color="#1d1e1e",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        horizontal.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("cheap", foreground="#8fd18f")
        self.tree.tag_configure("defect", foreground="#c77b7b")
        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<Button-1>", self._click_link)

    def _on_sort(self, label: str) -> None:
        self._sort_key = SORT_LABELS.get(label, "market_diff")
        self._refresh()

    def set_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        self._rows = list(rows)
        self._refresh()

    def set_query_key(self, query_key: str) -> None:
        self._query_key = str(query_key or "")
        self._refresh()

    def _refresh(self) -> None:
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self._urls.clear()
        rows = [
            row
            for row in self._rows
            if not self._query_key or row.get("_query_key") == self._query_key
        ]
        if self._sort_key == "market_diff":
            rows.sort(
                key=lambda row: (
                    1 if not isinstance(row.get("vs_market"), (int, float)) else 0,
                    float(row.get("vs_market") or 0),
                )
            )
        elif self._sort_key in {"price_asc", "price_desc"}:
            rows.sort(
                key=lambda row: (
                    (row.get("listing") or {}).get("price") is None,
                    (row.get("listing") or {}).get("price") or 0,
                ),
                reverse=self._sort_key == "price_desc",
            )
        else:
            rows.sort(
                key=lambda row: str((row.get("listing") or {}).get("published_at") or ""),
                reverse=True,
            )
        count = 0
        for row in rows:
            listing = row.get("listing") or {}
            market = row.get("market")
            vs_market = row.get("vs_market")
            notes = str(row.get("notes") or "")
            tags: tuple[str, ...] = ()
            if "неисправен" in notes or "дефект" in notes:
                tags = ("defect",)
            elif "дешевле" in notes or "дёшево" in notes:
                tags = ("cheap",)
            iid = self.tree.insert(
                "",
                "end",
                values=(
                    str(row.get("query") or ""),
                    str(listing.get("title") or ""),
                    format_price_short(listing.get("price")),
                    format_price_short(market),
                    f"{vs_market:+.0%}" if vs_market is not None else "Мало данных",
                    notes,
                    "открыть",
                ),
                tags=tags,
            )
            url = str(listing.get("url") or "")
            if url:
                self._urls[iid] = url
            count += 1
        self.count_label.configure(text=f"{count} объявлений")

    def _click_link(self, event: Any) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#7":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self._open_selected()

    def _open_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        url = self._urls.get(selection[0], "")
        if not url:
            return
        try:
            if not open_listing_url(url):
                messagebox.showwarning(
                    "Avito Radar",
                    "Не удалось открыть ссылку. Скопируйте её из таблицы.",
                )
        except Exception as exc:
            messagebox.showerror("Avito Radar", f"Ошибка открытия ссылки:\n{exc}")


class RejectedTable(ctk.CTkFrame):
    """Таблица отклонённых карточек с указанием запроса и причины."""

    COLUMNS = (
        ("query", "Запрос", 125, "w"),
        ("title", "Заголовок", 210, "w"),
        ("price", "Цена", 68, "e"),
        ("market_diff", "От рынка", 62, "center"),
        ("city", "Город", 100, "w"),
        ("reason", "Причина", 190, "w"),
        ("link", "Ссылка", 52, "center"),
    )

    def __init__(self, master: Any, **kwargs: Any):
        super().__init__(master, fg_color="transparent", **kwargs)
        self._rows: list[dict[str, Any]] = []
        self._urls: dict[str, str] = {}
        self._sort_key = "market_diff"
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            top, text="Не подходящие", font=ctk.CTkFont(size=16, weight="bold")
        ).pack(side="left")
        self.count_label = ctk.CTkLabel(
            top,
            text="0 объявлений",
            text_color=("#4a4a4a", "#b8b8b8"),
        )
        self.count_label.pack(side="right")
        self.sort_segment = ctk.CTkSegmentedButton(
            top, values=list(SORT_LABELS), command=self._on_sort, width=390
        )
        self.sort_segment.set("%Выгоды от рынка")
        self.sort_segment.pack(side="right", padx=(0, 10))

        container = tk.Frame(self, bg="#1d1e1e", highlightthickness=0)
        container.pack(fill="both", expand=True)
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "AvitoRejected.Treeview",
            background="#1d1e1e",
            foreground="#e8e8e8",
            fieldbackground="#1d1e1e",
            bordercolor="#343638",
            rowheight=24,
            font=("Segoe UI", 8),
        )
        style.configure(
            "AvitoRejected.Treeview.Heading",
            background="#2b2d2e",
            foreground="#ffffff",
            relief="flat",
            font=("Segoe UI", 8, "bold"),
        )
        self.tree = ttk.Treeview(
            container,
            columns=[column[0] for column in self.COLUMNS],
            show="headings",
            style="AvitoRejected.Treeview",
            selectmode="browse",
        )
        for key, heading, width, anchor in self.COLUMNS:
            self.tree.heading(key, text=heading)
            self.tree.column(
                key, width=width, minwidth=width, anchor=anchor, stretch=False
            )
        vertical = ctk.CTkScrollbar(
            container,
            orientation="vertical",
            command=self.tree.yview,
            width=14,
            fg_color="#1d1e1e",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        horizontal = ctk.CTkScrollbar(
            container,
            orientation="horizontal",
            command=self.tree.xview,
            height=14,
            fg_color="#1d1e1e",
            button_color="#3d4247",
            button_hover_color="#545a60",
            corner_radius=8,
        )
        self.tree.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns", padx=(3, 0))
        horizontal.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        container.grid_rowconfigure(0, weight=1)
        container.grid_columnconfigure(0, weight=1)
        self.tree.tag_configure("missing", foreground="#c77b7b")
        self.tree.bind("<Double-1>", self._open_selected)
        self.tree.bind("<Button-1>", self._click_link)

    def _on_sort(self, label: str) -> None:
        self._sort_key = SORT_LABELS.get(label, "market_diff")
        self._refresh()

    def set_rows(self, rows: Iterable[dict[str, Any]]) -> None:
        self._rows = list(rows)
        self._refresh()

    def _refresh(self) -> None:
        for item in self.tree.get_children(""):
            self.tree.delete(item)
        self._urls.clear()
        rows = list(self._rows)
        if self._sort_key == "market_diff":
            rows.sort(
                key=lambda entry: (
                    1
                    if not isinstance(
                        (entry.get("listing") or {}).get("market_diff"),
                        (int, float),
                    )
                    else 0,
                    float((entry.get("listing") or {}).get("market_diff") or 0),
                )
            )
        elif self._sort_key in {"price_asc", "price_desc"}:
            rows.sort(
                key=lambda entry: (
                    (entry.get("listing") or {}).get("price") is None,
                    (entry.get("listing") or {}).get("price") or 0,
                ),
                reverse=self._sort_key == "price_desc",
            )
        else:
            rows.sort(
                key=lambda entry: str(
                    (entry.get("listing") or {}).get("published_at") or ""
                ),
                reverse=True,
            )
        count = 0
        for index, entry in enumerate(rows):
            listing = entry.get("listing") or {}
            reason = entry.get("reason") or ""
            query = entry.get("query") or ""
            market_diff = listing.get("market_diff")
            iid = f"rejected_{index}"
            self.tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    query,
                    listing.get("title", "") or "Без названия",
                    format_price_short(listing.get("price")),
                    f"{market_diff:+.0%}" if isinstance(market_diff, (int, float)) else "Мало данных",
                    format_location(listing),
                    reason,
                    "открыть" if listing.get("url") else "—",
                ),
                tags=("missing",) if listing.get("status") == "missing" else (),
            )
            url = str(listing.get("url") or "")
            if url:
                self._urls[iid] = url
            count += 1
        self.count_label.configure(
            text=(
                f"{count} "
                f"{_plural_ru(count, 'объявление', 'объявления', 'объявлений')}"
            )
        )

    def _click_link(self, event: Any) -> None:
        if self.tree.identify_region(event.x, event.y) != "cell":
            return
        if self.tree.identify_column(event.x) != "#7":
            return
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self._open_selected()

    def _open_selected(self, _event: Any = None) -> None:
        selection = self.tree.selection()
        if not selection:
            return
        url = self._urls.get(selection[0], "")
        if not url:
            return
        if not open_listing_url(url):
            messagebox.showwarning(
                "Avito Radar",
                "Не удалось открыть ссылку. Она скопирована в буфер обмена.",
            )
            try:
                self.clipboard_clear()
                self.clipboard_append(url)
            except Exception:
                pass

class DetailsPanel(ctk.CTkFrame):
    """Правая панель с подробностями выбранного объявления."""

    def __init__(
        self,
        master: Any,
        *,
        on_open: Callable[[str], None] | None = None,
        on_copy: Callable[[str], None] | None = None,
        on_favorite: Callable[[dict[str, Any], bool], None] | None = None,
        on_blacklist: Callable[[str], None] | None = None,
        on_close: Callable[[], None] | None = None,
        **kwargs: Any,
    ):
        super().__init__(master, fg_color=("#ffffff", "#1f2020"), corner_radius=10, **kwargs)
        self.on_open = on_open
        self.on_copy = on_copy
        self.on_favorite = on_favorite
        self.on_blacklist = on_blacklist
        self.on_close = on_close
        self._listing: dict[str, Any] = {}
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.pack(fill="x", padx=10, pady=(10, 4))
        self.title_label = ctk.CTkLabel(
            header,
            text="Выберите объявление",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
            justify="left",
            wraplength=260,
        )
        self.title_label.pack(side="left", fill="x", expand=True)
        self.close_button = ctk.CTkButton(
            header,
            text="×",
            width=28,
            height=26,
            fg_color="transparent",
            hover_color=("#eef1f4", "#2b3035"),
            text_color=("#666666", "#c9d1d9"),
            command=self._close,
        )
        self.close_button.pack(side="right", padx=(4, 0))
        self.info_label = ctk.CTkLabel(
            self,
            text="",
            anchor="w",
            justify="left",
            wraplength=290,
            font=ctk.CTkFont(size=11),
        )
        self.info_label.pack(fill="x", padx=12)
        ctk.CTkLabel(
            self, text="История цены", anchor="w", font=ctk.CTkFont(size=11, weight="bold")
        ).pack(fill="x", padx=12, pady=(10, 2))
        self.history_label = ctk.CTkLabel(
            self,
            text="—",
            anchor="w",
            justify="left",
            wraplength=290,
            font=ctk.CTkFont(size=10),
            text_color=("#777777", "#9aaebb"),
        )
        self.history_label.pack(fill="x", padx=12)
        self.open_button = ctk.CTkButton(
            self, text="Открыть в браузере", command=self._open
        )
        self.open_button.pack(fill="x", padx=12, pady=(12, 4))
        self.copy_button = ctk.CTkButton(
            self,
            text="Копировать ссылку",
            command=self._copy,
            fg_color=("#e3e9f2", "#263442"),
            hover_color=("#d2dce9", "#31465a"),
            text_color=("#1e4d78", "#b9d8f5"),
        )
        self.copy_button.pack(fill="x", padx=12, pady=4)
        self.favorite_button = ctk.CTkButton(
            self, text="В избранное", command=self._favorite
        )
        self.favorite_button.pack(fill="x", padx=12, pady=4)
        self.blacklist_button = ctk.CTkButton(
            self,
            text="Заблокировать продавца",
            fg_color=("#f0e2e2", "#3a2626"),
            hover_color=("#e4d3d3", "#4a3030"),
            text_color=("#8f2020", "#ffb3b3"),
            command=self._blacklist,
        )
        self.blacklist_button.pack(fill="x", padx=12, pady=(4, 12))

    def clear(self) -> None:
        self._listing = {}
        self.title_label.configure(text="Выберите объявление")
        self.info_label.configure(text="")
        self.history_label.configure(text="—")
        self.open_button.configure(state="disabled")
        self.copy_button.configure(state="disabled")
        self.favorite_button.configure(state="disabled")
        self.blacklist_button.configure(state="disabled")

    def set_listing(
        self,
        listing: dict[str, Any] | None,
        *,
        favorite: bool = False,
        history: Iterable[dict[str, Any]] = (),
    ) -> None:
        if not listing:
            self.clear()
            return
        self._listing = dict(listing)
        self.title_label.configure(text=str(listing.get("title") or "Без названия"))
        market = listing.get("market_diff")
        lines = [
            f"Цена: {format_price_short(listing.get('price'))}",
            (
                f"От рынка: {market:+.0%}"
                if isinstance(market, (int, float))
                else "От рынка: недостаточно данных для расчёта рынка"
            ),
            f"Город: {format_location(listing) or '—'}",
            f"Доставка: {format_delivery(listing.get('delivery') or 'unknown')}",
            (
                f"Продавец: {listing.get('seller') or 'неизвестен'} · "
                f"{ {'private': 'частный', 'company': 'компания'}.get(str(listing.get('seller_type') or ''), 'тип не определён') }"
            ),
            f"Опубликовано: {format_datetime(listing.get('published_at'))}",
        ]
        if listing.get("_reject_reason"):
            lines.append(f"Причина: {listing['_reject_reason']}")
        self.info_label.configure(text="\n".join(lines))
        history_lines = [
            f"{format_datetime(item.get('observed_at'))}: {format_price_short(item.get('price'))}"
            for item in list(history)[-6:]
        ]
        self.history_label.configure(text="\n".join(history_lines) or "—")
        self.open_button.configure(state="normal" if listing.get("url") else "disabled")
        self.copy_button.configure(state="normal" if listing.get("url") else "disabled")
        self.favorite_button.configure(
            state="normal", text="Убрать из избранного" if favorite else "В избранное"
        )
        self.blacklist_button.configure(
            state="normal" if listing.get("seller") else "disabled"
        )

    def _close(self) -> None:
        if self.on_close:
            self.on_close()

    def _open(self) -> None:
        if self.on_open and self._listing.get("url"):
            self.on_open(str(self._listing["url"]))

    def _copy(self) -> None:
        if self.on_copy and self._listing.get("url"):
            self.on_copy(str(self._listing["url"]))

    def _favorite(self) -> None:
        if self.on_favorite and self._listing:
            self.on_favorite(self._listing, True)

    def _blacklist(self) -> None:
        if self.on_blacklist and self._listing.get("seller"):
            self.on_blacklist(str(self._listing["seller"]))
