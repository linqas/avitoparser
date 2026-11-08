"""Общие утилиты Avito Radar: конфиг, пути, даты, цены, логирование.

Здесь намеренно нет ничего, что печатает токен бота или данные продавца.
"""

from __future__ import annotations

import ctypes
import json
import logging
import logging.handlers
import os
import re
import shutil
import subprocess
import tempfile
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

try:  # Python 3.11+; toml нужен по требованиям проекта.
    import toml
except ImportError:  # pragma: no cover - запасной вариант только для чтения
    toml = None  # type: ignore[assignment]

try:
    from zoneinfo import ZoneInfo

    MOSCOW_TZ = ZoneInfo("Europe/Moscow")
except Exception:  # pragma: no cover - Windows без tzdata
    MOSCOW_TZ = timezone(timedelta(hours=3), "MSK")

APP_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = APP_DIR / "config.toml"

DEFAULT_CONFIG: dict[str, Any] = {
    "app": {
        "name": "Avito Radar",
        "database_path": "data/avito_radar.db",
        "log_path": "logs/avito_radar.log",
        "export_dir": "exports",
        "max_listings": 50,
        "fresh_hours": 3.0,
        "missing_check": False,
        "gmt_offset_hours": 3,
        "headless": False,
    },
    "notifications": {
        "new": True,
        "price": True,
        "old": False,
        "sort": "date",
        "snapshot": True,
        "max_price": 0,
        "keywords": "",
    },
    "scraper": {
        "base_url": "https://www.avito.ru",
        "transport": "auto",
        "auto_captcha": True,
        "http_delay_seconds": 5.0,
        "navigation_timeout_ms": 60000,
        "action_pause_min": 5.0,
        "action_pause_max": 10.0,
        "scroll_attempts": 3,
        "scan_limit": 50,
        "captcha_wait_seconds": 0,
        "hidden_window": True,
        "show_on_captcha": True,
        "auto_ip_check": True,
        "ip_check_wait_seconds": 30,
        "enrich_missing_dates": True,
        "date_lookup_pause_min": 1.0,
        "date_lookup_pause_max": 2.0,
    },
    "telegram": {
        "bot_token": "",
        "api_base": "https://api.telegram.org",
        "proxy_url": "",
        "poll_timeout_seconds": 25,
        "request_timeout_seconds": 15,
    },
    "defaults": {
        "mode": "once",
        "interval_minutes": 30,
        "sort": "date",
        "delivery": "any",
    },
}


class ConfigError(RuntimeError):
    """Конфигурация отсутствует или повреждена."""


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Читает config.toml и дополняет его безопасными значениями по умолчанию."""
    config_path = Path(path) if path else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        raise ConfigError(f"Файл конфигурации не найден: {config_path}")
    try:
        if toml is not None:
            loaded = toml.loads(config_path.read_text(encoding="utf-8"))
        else:  # pragma: no cover
            import tomllib

            loaded = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ConfigError(f"Не удалось прочитать config.toml: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError("config.toml должен содержать TOML-таблицы")
    return deep_merge(DEFAULT_CONFIG, loaded)


def project_path(value: str | Path, base: Path | None = None) -> Path:
    """Превращает относительный путь из конфига в путь рядом с проектом."""
    path = Path(value)
    if path.is_absolute():
        return path
    return (base or APP_DIR) / path


def app_path(config: dict[str, Any], key: str, fallback: str) -> Path:
    section = config.get("app", {}) if isinstance(config, dict) else {}
    return project_path(section.get(key, fallback))


def int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def float_or_default(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _escape_toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def save_telegram_token(path: str | Path, token: str) -> None:
    """Точечно обновляет [telegram].bot_token, сохраняя остальные строки файла.

    Токен не попадает в логи. Если структура файла неожиданная, обновление
    всё равно проверяется повторным разбором TOML перед записью.
    """
    config_path = Path(path)
    token = (token or "").strip()
    if "\n" in token or "\r" in token:
        raise ValueError("Токен не должен содержать перевод строки")

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    heading_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "[telegram]":
            heading_index = index
            break

    assignment = f"bot_token = {_escape_toml_string(token)}"

    if heading_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[telegram]", assignment])
    else:
        section_end = len(lines)
        for index in range(heading_index + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_end = index
                break
        replaced = False
        for index in range(heading_index + 1, section_end):
            if re.match(r"^\s*bot_token\s*=", lines[index]):
                lines[index] = assignment
                replaced = True
                break
        if not replaced:
            lines.insert(heading_index + 1, assignment)

    new_text = "\n".join(lines).rstrip() + "\n"
    if toml is not None:
        try:
            toml.loads(new_text)
        except Exception as exc:
            raise ValueError(f"Не удалось сохранить токен: {exc}") from exc
    elif not token.isprintable():  # pragma: no cover
        raise ValueError("Некорректный токен")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="config_", suffix=".toml.tmp", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_gmt_offset(path: str | Path, hours: Any) -> float:
    """Точечно сохраняет [app].gmt_offset_hours, не теряя остальной config."""
    config_path = Path(path)
    try:
        value = float(hours)
    except (TypeError, ValueError) as exc:
        raise ValueError("Часовой пояс должен быть числом") from exc
    if not -12.0 <= value <= 14.0:
        raise ValueError("Часовой пояс должен быть в диапазоне GMT-12…GMT+14")
    assignment = (
        f"gmt_offset_hours = {int(value)}"
        if value.is_integer()
        else f"gmt_offset_hours = {value:g}"
    )

    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    heading_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "[app]":
            heading_index = index
            break
    if heading_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[app]", assignment])
    else:
        section_end = len(lines)
        for index in range(heading_index + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_end = index
                break
        replaced = False
        for index in range(heading_index + 1, section_end):
            if re.match(r"^\s*gmt_offset_hours\s*=", lines[index]):
                lines[index] = assignment
                replaced = True
                break
        if not replaced:
            lines.insert(heading_index + 1, assignment)

    new_text = "\n".join(lines).rstrip() + "\n"
    if toml is not None:
        try:
            toml.loads(new_text)
        except Exception as exc:
            raise ValueError(f"Не удалось сохранить часовой пояс: {exc}") from exc
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="config_", suffix=".toml.tmp", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return value


def save_scraper_setting(path: str | Path, key: str, value: Any) -> None:
    """Точечно сохраняет скаляр [scraper], не теряя комментарии config."""
    if key not in {"transport", "auto_captcha", "http_delay_seconds"}:
        raise ValueError(f"Недопустимая настройка scraper: {key}")
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = f"{value:g}" if isinstance(value, float) else str(value)
    else:
        rendered = _escape_toml_string(str(value))
    assignment = f"{key} = {rendered}"

    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    heading_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip().lower() == "[scraper]":
            heading_index = index
            break
    if heading_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend(["[scraper]", assignment])
    else:
        section_end = len(lines)
        for index in range(heading_index + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_end = index
                break
        replaced = False
        for index in range(heading_index + 1, section_end):
            if re.match(rf"^\s*{re.escape(key)}\s*=", lines[index]):
                lines[index] = assignment
                replaced = True
                break
        if not replaced:
            lines.insert(heading_index + 1, assignment)

    new_text = "\n".join(lines).rstrip() + "\n"
    if toml is not None:
        try:
            toml.loads(new_text)
        except Exception as exc:
            raise ValueError(f"Не удалось сохранить настройку {key}: {exc}") from exc
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="config_", suffix=".toml.tmp", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def save_config_scalar(
    path: str | Path, section: str, key: str, value: Any
) -> None:
    """Точечно меняет один скаляр в TOML, сохраняя остальной файл."""
    section = str(section).strip()
    key = str(key).strip()
    if not section or not key:
        raise ValueError("Пустая секция или ключ config")
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, (int, float)):
        rendered = f"{value:g}" if isinstance(value, float) else str(value)
    else:
        rendered = _escape_toml_string(str(value))
    assignment = f"{key} = {rendered}"

    config_path = Path(path)
    text = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    lines = text.splitlines()
    heading = f"[{section}]"
    heading_index: int | None = None
    for index, line in enumerate(lines):
        if line.strip().lower() == heading:
            heading_index = index
            break
    if heading_index is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.extend([heading, assignment])
    else:
        section_end = len(lines)
        for index in range(heading_index + 1, len(lines)):
            stripped = lines[index].strip()
            if stripped.startswith("[") and stripped.endswith("]"):
                section_end = index
                break
        replaced = False
        for index in range(heading_index + 1, section_end):
            if re.match(rf"^\s*{re.escape(key)}\s*=", lines[index]):
                lines[index] = assignment
                replaced = True
                break
        if not replaced:
            lines.insert(heading_index + 1, assignment)

    new_text = "\n".join(lines).rstrip() + "\n"
    if toml is not None:
        try:
            toml.loads(new_text)
        except Exception as exc:
            raise ValueError(f"Не удалось сохранить {section}.{key}: {exc}") from exc
    config_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix="config_", suffix=".toml.tmp", dir=str(config_path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(new_text)
        os.replace(temp_name, config_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def get_telegram_token(config: dict[str, Any] | None = None) -> str:
    if config is None:
        config = load_config()
    return str(config.get("telegram", {}).get("bot_token", "") or "").strip()


_DISPLAY_GMT_OFFSET_HOURS = 3.0


def set_display_gmt_offset(value: Any) -> float:
    """Задаёт пояс для отображения дат; значения вне -12..+14 не принимаются."""
    global _DISPLAY_GMT_OFFSET_HOURS
    try:
        hours = float(value)
    except (TypeError, ValueError):
        hours = 3.0
    if not -12.0 <= hours <= 14.0:
        hours = 3.0
    _DISPLAY_GMT_OFFSET_HOURS = hours
    return hours


def display_timezone() -> Any:
    """Часовой пояс отображения — системный пояс Windows."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def now_moscow() -> datetime:
    return datetime.now(MOSCOW_TZ)


def as_moscow(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=MOSCOW_TZ)
    return value.astimezone(MOSCOW_TZ)


def iso_now() -> str:
    return now_moscow().isoformat(timespec="seconds")


MONTHS = {
    "января": 1,
    "январь": 1,
    "февраля": 2,
    "февраль": 2,
    "марта": 3,
    "март": 3,
    "апреля": 4,
    "апрель": 4,
    "мая": 5,
    "май": 5,
    "июня": 6,
    "июнь": 6,
    "июля": 7,
    "июль": 7,
    "августа": 8,
    "август": 8,
    "сентября": 9,
    "сентябрь": 9,
    "октября": 10,
    "октябрь": 10,
    "ноября": 11,
    "ноябрь": 11,
    "декабря": 12,
    "декабрь": 12,
}

_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)

_TEXT_REPLACEMENTS = (
    "\u00a0",
    "\u202f",
    "\u2009",
    "\n",
    "\r",
    "\t",
    "«",
    "»",
    "“",
    "”",
)


def clean_text(value: Any) -> str:
    text = str(value or "")
    for item in _TEXT_REPLACEMENTS:
        text = text.replace(item, " ")
    text = re.sub(r"\s+", " ", text)
    return text.strip(" ,;|•")


def parse_price(value: Any) -> int | None:
    """'1 250 ₽' -> 1250, 'Цена не указана' -> None, 'Бесплатно' -> 0."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return int(round(float(value)))
        except (OverflowError, ValueError):
            return None
    text = clean_text(value).lower()
    if not text:
        return None
    if "не указана" in text or "нет цены" in text or "цена не" in text:
        return None
    if "бесплатно" in text or text in {"0", "0 ₽"}:
        return 0
    match = re.search(r"\d[\d\s\u00a0\u202f]*(?:[.,]\d+)*", text)
    if not match:
        return None
    number_text = re.sub(r"[\s\u00a0\u202f]", "", match.group(0)).rstrip(".,")
    suffix = text[match.end() : match.end() + 16].lstrip()
    has_unit = suffix.startswith(("млн", "миллион", "тыс", "тысяч"))
    if has_unit:
        if "," in number_text:
            number_text = number_text.replace(".", "").replace(",", ".")
        elif number_text.count(".") > 1:
            number_text = number_text.replace(".", "")
    else:
        number_text = number_text.replace(".", "").replace(",", ".")
    try:
        number = float(number_text)
    except ValueError:
        return None
    if suffix.startswith(("млн", "миллион")):
        number *= 1_000_000
    elif suffix.startswith(("тыс", "тысяч")):
        number *= 1_000
    return int(round(number))


def parse_views(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return int(value)
        except (OverflowError, ValueError):
            return None
    text = clean_text(value).lower()
    if not text:
        return None
    match = re.search(r"\d[\d\s\u00a0\u202f]*(?:[.,]\d+)*", text)
    if not match:
        return None
    number_text = re.sub(r"[\s\u00a0\u202f]", "", match.group(0)).rstrip(".,")
    suffix = text[match.end() : match.end() + 12].lstrip()
    if suffix.startswith(("тыс", "тысяч")):
        if "," in number_text:
            number_text = number_text.replace(".", "").replace(",", ".")
        elif number_text.count(".") > 1:
            number_text = number_text.replace(".", "")
    else:
        number_text = number_text.replace(".", "").replace(",", ".")
    try:
        number = float(number_text)
    except ValueError:
        return None
    if suffix.startswith(("тыс", "тысяч")):
        number *= 1000
    return int(round(number))


def _time_from_text(text: str) -> tuple[int, int] | None:
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)", text)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return hour, minute
    return None


def _with_time(day: datetime, text: str) -> datetime:
    parsed_time = _time_from_text(text)
    if parsed_time:
        return day.replace(hour=parsed_time[0], minute=parsed_time[1], second=0, microsecond=0)
    return day.replace(hour=0, minute=0, second=0, microsecond=0)


def _safe_year_for(month: int, day: int, now: datetime) -> int:
    year = now.year
    try:
        candidate = datetime(year, month, day, tzinfo=MOSCOW_TZ)
    except ValueError:
        return year
    if candidate > now + timedelta(days=2):
        year -= 1
    return year


def parse_published_at(value: Any, now: datetime | None = None) -> str | None:
    """Преобразует относительную/короткую дату Avito в абсолютный ISO 8601.

    Пустая или нераспознанная дата -> None. Все даты приводятся к Europe/Moscow.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return as_moscow(value).isoformat(timespec="seconds")
    text = clean_text(value).lower()
    if not text:
        return None
    now = as_moscow(now) if now else now_moscow()

    text = re.sub(r"^(опубликовано|размещено|дата публикации)\s*[:\-]?\s*", "", text)

    # ISO-дата, которую мог вернуть сам Avito или сохранённая история.
    iso_candidate = text.replace("z", "+00:00")
    if re.match(r"^\d{4}-\d{2}-\d{2}", iso_candidate):
        try:
            return as_moscow(datetime.fromisoformat(iso_candidate)).isoformat(timespec="seconds")
        except ValueError:
            pass

    if "только что" in text or "прямо сейчас" in text:
        return now.isoformat(timespec="seconds")

    relative_match = re.search(
        r"(\d+|один|одну|два|две|три|четыре|пять|шесть|семь|восемь|девять|десять)\s*"
        r"(минут\w*|мин\b|час\w*|день|дня|дней|сут\w*|недел\w*|месяц\w*|год\w*|лет)\s*назад",
        text,
    )
    if relative_match:
        amount_raw = relative_match.group(1)
        words = {
            "один": 1,
            "одну": 1,
            "два": 2,
            "две": 2,
            "три": 3,
            "четыре": 4,
            "пять": 5,
            "шесть": 6,
            "семь": 7,
            "восемь": 8,
            "девять": 9,
            "десять": 10,
        }
        amount = int(amount_raw) if amount_raw.isdigit() else words.get(amount_raw, 1)
        unit = relative_match.group(2)
        if unit.startswith(("минут", "мин")):
            result = now - timedelta(minutes=amount)
        elif unit.startswith("час"):
            result = now - timedelta(hours=amount)
        elif unit.startswith("недел"):
            result = now - timedelta(weeks=amount)
        elif unit.startswith("месяц"):
            result = now - timedelta(days=30 * amount)
        elif unit.startswith(("год", "лет")):
            result = now - timedelta(days=365 * amount)
        else:
            result = now - timedelta(days=amount)
        return result.isoformat(timespec="seconds")

    if "позавчера" in text:
        return _with_time(now - timedelta(days=2), text).isoformat(timespec="seconds")
    if "вчера" in text:
        return _with_time(now - timedelta(days=1), text).isoformat(timespec="seconds")
    if "сегодня" in text:
        return _with_time(now, text).isoformat(timespec="seconds")

    numeric_date = re.search(r"(?<!\d)(\d{1,2})[./](\d{1,2})[./](\d{2,4})(?!\d)", text)
    if numeric_date:
        day, month, year = (int(part) for part in numeric_date.groups())
        if year < 100:
            year += 2000
        try:
            result = datetime(year, month, day, tzinfo=MOSCOW_TZ)
            parsed_time = _time_from_text(text)
            if parsed_time:
                result = result.replace(hour=parsed_time[0], minute=parsed_time[1])
            return result.isoformat(timespec="seconds")
        except ValueError:
            pass

    month_pattern = "|".join(sorted(MONTHS, key=len, reverse=True))
    word_date = re.search(rf"(?<!\d)(\d{{1,2}})\s+({month_pattern})(?:\s+(\d{{4}}))?", text)
    if word_date:
        day = int(word_date.group(1))
        month = MONTHS[word_date.group(2)]
        explicit_year = word_date.group(3)
        year = int(explicit_year) if explicit_year else _safe_year_for(month, day, now)
        try:
            result = datetime(year, month, day, tzinfo=MOSCOW_TZ)
            parsed_time = _time_from_text(text)
            if parsed_time:
                result = result.replace(hour=parsed_time[0], minute=parsed_time[1])
            return result.isoformat(timespec="seconds")
        except ValueError:
            pass

    only_time = _time_from_text(text)
    if only_time and any(day in text for day in _WEEKDAYS):
        result = now.replace(hour=only_time[0], minute=only_time[1], second=0, microsecond=0)
        return result.isoformat(timespec="seconds")

    return None


def detect_delivery(*parts: Any) -> str:
    """Возвращает yes/no/unknown только по явным признакам на карточке."""
    text = " ".join(clean_text(part).lower() for part in parts if part)
    if not text:
        return "unknown"
    if any(marker in text for marker in ("авито доставка", "доставка", "delivery")):
        return "yes"
    if any(marker in text for marker in ("самовывоз", "без доставки", "только самовывоз")):
        return "no"
    return "unknown"


def normalize_delivery(value: Any) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    text = clean_text(value).lower()
    if text in {"yes", "да", "true", "1"}:
        return "yes"
    if text in {"no", "нет", "false", "0"}:
        return "no"
    if text in {"", "unknown", "неизвестно", "n/a", "none"}:
        return "unknown"
    return detect_delivery(text)


def extract_avito_id(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    if text.isdigit() and len(text) >= 6:
        return text
    match = re.search(r"_(\d{6,})(?=[/?#]|$)", text)
    if match:
        return match.group(1)
    match = re.search(r"/(\d{6,})(?=[/?#]|$)", text)
    if match:
        return match.group(1)
    return ""


def clean_city(value: Any) -> str:
    text = clean_text(value)
    text = re.sub(r",?\s*\d+(?:[.,]\d+)?\s*км\s*$", "", text, flags=re.IGNORECASE)
    return clean_text(text)


def normalize_listing(
    raw: dict[str, Any],
    *,
    now: datetime | None = None,
    base_url: str = "https://www.avito.ru",
) -> dict[str, Any]:
    """Приводит сырой словарь со страницы к формату хранилища.

    Собираются только публичные поля объявления; имя и телефон продавца сюда
    не попадают.
    """
    url = clean_text(raw.get("url"))
    if url.startswith("/"):
        url = urljoin(base_url.rstrip("/") + "/", url.lstrip("/"))
    listing_id = clean_text(raw.get("id")) or extract_avito_id(url)
    if not listing_id:
        raise ValueError("В карточке не найден id объявления")
    if not listing_id.isdigit():
        listing_id = extract_avito_id(listing_id) or listing_id

    title = clean_text(raw.get("title")) or "Без названия"
    price = parse_price(raw.get("price"))
    published_raw = raw.get("published_at", raw.get("published_raw", raw.get("date")))
    published_at: str | None
    if isinstance(published_raw, str) and published_raw.strip().startswith(("20", "19")):
        published_at = parse_published_at(published_raw, now=now)
    else:
        published_at = parse_published_at(published_raw, now=now)

    delivery_raw = raw.get("delivery")
    if delivery_raw is None or clean_text(delivery_raw) == "":
        delivery = detect_delivery(raw.get("delivery_text"), raw.get("seller_text"))
    else:
        delivery = normalize_delivery(delivery_raw)

    location_text = fix_city_name(clean_city(raw.get("city") or raw.get("location")))
    url_city = fix_city_name(city_from_url(url))
    # Русское название из карточки точнее транслитерации slug из URL.
    if location_text and re.search(r"[А-Яа-яЁё]", location_text):
        city = location_text
        metro = clean_metro(location_text, city)
    elif url_city:
        city = url_city
        metro = clean_metro(location_text, city)
    else:
        city = location_text
        metro = ""

    return {
        "id": str(listing_id),
        "title": title,
        "price": price,
        "published_at": published_at,
        "city": city,
        "metro": metro,
        "delivery": delivery,
        "seller": clean_text(raw.get("seller") or raw.get("seller_text")),
        "seller_type": (
            "private"
            if "частн" in clean_text(raw.get("seller") or raw.get("seller_text")).casefold()
            else "company"
            if any(
                word
                in clean_text(raw.get("seller") or raw.get("seller_text")).casefold()
                for word in ("компания", "агентство", "магазин", "застройщик")
            )
            else "unknown"
        ),
        "url": url,
        "views": parse_views(raw.get("views")),
    }


def format_price(value: Any) -> str:
    price = parse_price(value) if not isinstance(value, int) else value
    if price is None:
        return "не указана"
    return f"{price:,}".replace(",", " ") + " ₽"


def format_price_short(value: Any) -> str:
    price = parse_price(value) if not isinstance(value, int) else value
    return "—" if price is None else f"{price:,}".replace(",", " ") + " ₽"


def format_datetime(value: Any, include_time: bool = True) -> str:
    if not value:
        return "неизвестно"
    try:
        parsed = datetime.fromisoformat(str(value))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=MOSCOW_TZ)
        parsed = parsed.astimezone(display_timezone())
        return parsed.strftime("%d.%m.%Y %H:%M" if include_time else "%d.%m.%Y")
    except (ValueError, TypeError):
        return str(value)


def format_delivery(value: str) -> str:
    return {"yes": "есть", "no": "нет", "unknown": "нет"}.get(value, "нет")


def format_views_short(value: Any) -> str:
    """Просмотры: число, если известно, иначе «нет» (вместо прочерка)."""
    if value is None or value == "":
        return "нет"
    try:
        return str(int(value))
    except (TypeError, ValueError):
        return str(value)


def format_location(listing: dict[str, Any]) -> str:
    """Город и, если есть, метро/район: «Москва · м. Смоленская»."""
    city = fix_city_name(listing.get("city"))
    metro = clean_text(listing.get("metro"))
    if city and metro:
        lower = metro.casefold()
        if lower.startswith(("м.", "метро", "р-н", "район")):
            return f"{city} · {metro}"
        return f"{city} · м. {metro}"
    return city or metro or "—"


def truncate_message(text: str, limit: int = 3900) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def is_captcha_text(title: str, body: str) -> bool:
    """Отличает страницу проверки/ограничения Avito от обычной выдачи."""
    title_text = (title or "").lower()
    body_text = (body or "").lower()
    if "заблокирован" in title_text or "blocked" in title_text:
        return True
    markers = (
        "подтвердите, что вы не робот",
        "нужна небольшая проверка",
        "проверка браузера",
        "доступ ограничен",
        "доступ временно ограничен",
        "проблема с доступом",
        "проблема с ip",
        "проблемы с ip",
        "пройти проверку",
        "страница не загрузилась",
        "слишком много запросов",
        "captcha",
        "капча",
    )
    return any(marker in body_text for marker in markers)


def is_no_results_text(text: str) -> bool:
    haystack = clean_text(text).lower()
    return any(
        marker in haystack
        for marker in (
            "ничего не найдено",
            "мы ничего не нашли",
            "объявления не найдены",
            "по вашему запросу ничего",
            "нет объявлений",
        )
    )


_CITY_SLUGS = {
    "москва": "moskva",
    "санкт-петербург": "sankt-peterburg",
    "петербург": "sankt-peterburg",
    "спб": "sankt-peterburg",
    "новосибирск": "novosibirsk",
    "екатеринбург": "ekaterinburg",
    "казань": "kazan",
    "нижний новгород": "nizhniy_novgorod",
    "челябинск": "chelyabinsk",
    "самара": "samara",
    "омск": "omsk",
    "ростов-на-дону": "rostov-na-donu",
    "уфа": "ufa",
    "красноярск": "krasnoyarsk",
    "воронеж": "voronezh",
    "пермь": "perm",
    "волгоград": "volgograd",
    "краснодар": "krasnodar",
    "саратов": "saratov",
    "тюмень": "tyumen",
    "курган": "kurgan",
    "тольятти": "tolyatti",
    "ижевск": "izhevsk",
    "барнаул": "barnaul",
    "ульяновск": "ulyanovsk",
    "иркутск": "irkutsk",
    "хабаровск": "khabarovsk",
    "ярославль": "yaroslavl",
    "владивосток": "vladivostok",
    "махачкала": "makhachkala",
    "томск": "tomsk",
    "оренбург": "orenburg",
    "кемерово": "kemerovo",
    "новокузнецк": "novokuznetsk",
    "рязань": "ryazan",
    "астрахань": "astrakhan",
    "набережные челны": "naberezhnye_chelny",
    "пенза": "penza",
    "липецк": "lipetsk",
    "киров": "kirov",
    "чебоксары": "cheboksary",
    "балашиха": "balashikha",
    "калининград": "kaliningrad",
    "тула": "tula",
    "курск": "kursk",
    "севастополь": "sevastopol",
    "сочи": "sochi",
    "ставрополь": "stavropol",
    "улан-удэ": "ulan-ude",
    "тверь": "tver",
    "магнитогорск": "magnitogorsk",
    "иваново": "ivanovo",
    "брянск": "bryansk",
    "белгород": "belgorod",
    "сургут": "surgut",
    "владимир": "vladimir",
    "нижний тагил": "nizhniy_tagil",
    "архангельск": "arkhangelsk",
    "чита": "chita",
    "калуга": "kaluga",
    "смоленск": "smolensk",
    "волжский": "volzhskiy",
    "череповец": "cherepovets",
    "орёл": "orel",
    "орел": "orel",
    "саранск": "saransk",
    "вологда": "vologda",
    "мурманск": "murmansk",
    "якутск": "yakutsk",
    "тамбов": "tambov",
    "грозный": "groznyy",
}

_TRANSLIT = str.maketrans(
    {
        "а": "a",
        "б": "b",
        "в": "v",
        "г": "g",
        "д": "d",
        "е": "e",
        "ё": "e",
        "ж": "zh",
        "з": "z",
        "и": "i",
        "й": "y",
        "к": "k",
        "л": "l",
        "м": "m",
        "н": "n",
        "о": "o",
        "п": "p",
        "р": "r",
        "с": "s",
        "т": "t",
        "у": "u",
        "ф": "f",
        "х": "kh",
        "ц": "ts",
        "ч": "ch",
        "ш": "sh",
        "щ": "shch",
        "ъ": "",
        "ы": "y",
        "ь": "",
        "э": "e",
        "ю": "yu",
        "я": "ya",
    }
)


def city_to_slug(city: str) -> str:
    """Город -> slug Avito. Неизвестные города транслитерируются."""
    normalized = clean_text(city).lower().replace("ё", "е")
    normalized = re.sub(r"^(?:г\.?\s*|город\s+)", "", normalized).strip()
    if not normalized:
        return ""
    if normalized in _CITY_SLUGS:
        return _CITY_SLUGS[normalized]
    if re.fullmatch(r"[a-z0-9_-]+", normalized):
        return normalized
    transliterated = normalized.translate(_TRANSLIT)
    transliterated = re.sub(r"[^a-z0-9]+", "_", transliterated).strip("_")
    return transliterated


def _display_city_name(source: str) -> str:
    words = re.split(r"([\s-]+)", source.replace("ё", "е"))
    small_words = {"и", "в", "на", "по", "под"}
    result: list[str] = []
    for word in words:
        if not word:
            continue
        if re.fullmatch(r"[\s-]+", word):
            result.append(word)
        elif word in small_words and result:
            result.append(word)
        else:
            result.append(word[:1].upper() + word[1:])
    return "".join(result)


_SLUG_TO_CITY: dict[str, str] = {}
for _city_name, _slug in _CITY_SLUGS.items():
    _SLUG_TO_CITY.setdefault(_slug, _display_city_name(_city_name))

_SLUG_TO_CITY.update(
    {
        "moskva_i_mo": "Москва и Московская область",
        "moskovskaya_oblast": "Московская область",
        "sankt-peterburg_i_lo": "Санкт-Петербург и Ленинградская область",
        "leningradskaya_oblast": "Ленинградская область",
        "rossiya": "Россия",
        "krym": "Крым",
        "arhangelsk": "Архангельск",
        "lyubertsy": "Люберцы",
        "mahachkala": "Махачкала",
        "kogalym": "Когалым",
        "podolsk": "Подольск",
        "simferopol": "Симферополь",
        "engels": "Энгельс",
        "korolev": "Королёв",
        "mirnoe": "Мирное",
        "himki": "Химки",
        "moskovskaya_oblast_krasnogorsk": "Московская область, Красногорск",
    }
)

CITY_NAME_FIXES = {
    "алметевск": "Альметьевск",
    "амурскайа област благовещенск": "Амурская область, Благовещенск",
    "архангелскайа област мирныы": "Архангельская область, Мирный",
    "астрахан": "Астрахань",
    "белгородскайа област алексеевка": "Белгородская область, Алексеевка",
    "бугулма": "Бугульма",
    "васюринскайа": "Васюринская",
    "ешкар ола": "Йошкар-Ола",
    "керч": "Керчь",
    "кинел": "Кинель",
    "кировскайа област киров": "Кировская область, Киров",
    "комсомолск на амуре": "Комсомольск-на-Амуре",
    "медын": "Медынь",
    "молочныы": "Молочный",
    "московскайа област ивантеевка": "Московская область, Ивантеевка",
    "мурманскайа област кировск": "Мурманская область, Кировск",
    "налчик": "Нальчик",
    "новоселие": "Новоселье",
    "новыы уренгой": "Новый Уренгой",
    "нойабрск": "Ноябрьск",
    "первоуралск": "Первоуральск",
    "петропавловск камчацкий": "Петропавловск-Камчатский",
    "прохладныы": "Прохладный",
    "славянск на кубани": "Славянск-на-Кубани",
    "старайа купавна": "Старая Купавна",
    "староминскайа": "Староминская",
    "чебаркул": "Чебаркуль",
    "челябинскайа област карабаш": "Челябинская область, Карабаш",
    "шахуня": "Шахунья",
    "южно сахалинск": "Южно-Сахалинск",
    "ясныы": "Ясный",
}


def fix_city_name(value: Any) -> str:
    text = clean_text(value)
    if not text:
        return ""
    key = text.casefold().replace("ё", "е")
    key = re.sub(r"\s+", " ", key).strip()
    return CITY_NAME_FIXES.get(key, text)


_CITY_LATIN_OVERRIDES = {
    "arhangelsk": "Архангельск",
    "lyubertsy": "Люберцы",
    "mahachkala": "Махачкала",
    "kogalym": "Когалым",
    "podolsk": "Подольск",
    "simferopol": "Симферополь",
    "engels": "Энгельс",
    "korolev": "Королёв",
    "mirnoe": "Мирное",
    "himki": "Химки",
}

_LATIN_TRANSLIT = (
    ("shch", "щ"),
    ("sch", "щ"),
    ("zh", "ж"),
    ("kh", "х"),
    ("ts", "ц"),
    ("ch", "ч"),
    ("sh", "ш"),
    ("yu", "ю"),
    ("ya", "я"),
    ("yo", "ё"),
    ("ye", "е"),
    ("iy", "ий"),
    ("ay", "ай"),
    ("ey", "ей"),
    ("oy", "ой"),
    ("a", "а"),
    ("b", "б"),
    ("v", "в"),
    ("g", "г"),
    ("d", "д"),
    ("e", "е"),
    ("z", "з"),
    ("i", "и"),
    ("k", "к"),
    ("l", "л"),
    ("m", "м"),
    ("n", "н"),
    ("o", "о"),
    ("p", "п"),
    ("r", "р"),
    ("s", "с"),
    ("t", "т"),
    ("u", "у"),
    ("f", "ф"),
    ("h", "х"),
    ("c", "ц"),
    ("y", "ы"),
    ("j", "й"),
)


def _latin_city_to_russian(value: str) -> str:
    text = value.casefold().replace("_", " ").replace("-", " ")
    text = re.sub(r"\s+", " ", text).strip()
    if not text or not re.fullmatch(r"[a-z ]+", text):
        return ""
    direct = _CITY_LATIN_OVERRIDES.get(text.replace(" ", "_"))
    if direct:
        return direct
    result: list[str] = []
    index = 0
    while index < len(text):
        if text[index] == " ":
            result.append(" ")
            index += 1
            continue
        matched = False
        for latin, cyrillic in _LATIN_TRANSLIT:
            if text.startswith(latin, index):
                result.append(cyrillic)
                index += len(latin)
                matched = True
                break
        if not matched:
            result.append(text[index])
            index += 1
    translated = "".join(result)
    return translated[:1].upper() + translated[1:] if translated else ""


def city_from_url(url: str) -> str:
    """Достаёт город из пути объявления Avito: /moskva/... -> Москва."""
    try:
        path = urlsplit(url or "").path.strip("/")
    except Exception:
        return ""
    if not path:
        return ""
    slug = path.split("/", 1)[0].casefold()
    if (
        not slug
        or ":" in slug
        or slug in {
            "web",
            "api",
            "user",
            "users",
            "brands",
            "catalog",
            "help",
            "support",
            "sitemap",
        }
    ):
        return ""
    if slug in _SLUG_TO_CITY:
        return _SLUG_TO_CITY[slug]
    # Для небольших населённых пунктов словаря нет. Пробуем перевести
    # латинский slug обратно в кириллицу, иначе оставляем читаемый вариант.
    translated = _latin_city_to_russian(slug)
    if translated:
        return translated
    return _display_city_name(slug.replace("_", " ").replace("-", " "))


_GENERIC_AVITO_SEGMENTS = {
    "telefony",
    "mobilnye_telefony",
    "noutbuki",
    "kompyutery",
    "tovary_dlya_kompyutera",
    "audio",
    "video",
    "igry",
    "bytovaya_elektronika",
    "odezhda_obuv_aksessuary",
    "dlya_doma_i_dachi",
    "remont_i_stroitelstvo",
    "transport",
    "nedvizhimost",
    "uslugi",
    "rabota",
    "predlozheniya_uslug",
    "zhivotnye",
    "dlya_detey_i_igrushek",
}


def is_avito_search_url(value: str) -> bool:
    """Похоже ли значение на ссылку Avito, а не на обычный запрос."""
    text = (value or "").strip()
    if not re.match(r"^https?://", text, flags=re.IGNORECASE):
        return False
    try:
        host = (urlsplit(text).hostname or "").casefold()
    except ValueError:
        return False
    return host == "avito.ru" or host.endswith(".avito.ru")


def _title_from_avito_path(path: str, source_url: str) -> str:
    segments = [clean_text(part) for part in path.strip("/").split("/") if part]
    if not segments:
        return ""
    city = city_from_url(source_url)
    if city and len(segments) > 1:
        segments = segments[1:]

    last_raw = segments[-1] if segments else ""
    # Avito добавляет к последнему slug длинный закодированный хвост -ASgBAg...
    if "-" in last_raw:
        head, tail = last_raw.split("-", 1)
        if len(tail) >= 6:
            last_raw = head
    model_words = [w for w in re.split(r"[_-]+", last_raw) if w]

    title_words: list[str] = []
    if len(segments) >= 2:
        previous_raw = segments[-2]
        previous_words = [w for w in re.split(r"[_-]+", previous_raw) if w]
        previous_key = "_".join(previous_words).casefold()
        if previous_key not in _GENERIC_AVITO_SEGMENTS:
            title_words.extend(previous_words)
    title_words.extend(model_words)
    if not title_words:
        return ""
    text = " ".join(title_words)
    fixes = {
        "iphone": "iPhone",
        "ipad": "iPad",
        "macbook": "MacBook",
        "imac": "iMac",
        "airpods": "AirPods",
        "samsung": "Samsung",
        "xiaomi": "Xiaomi",
        "honor": "Honor",
        "huawei": "Huawei",
        "chernyy": "черный",
        "cherniy": "черный",
        "belyy": "белый",
        "beliy": "белый",
        "siniy": "синий",
        "sinij": "синий",
        "seryy": "серый",
        "seryj": "серый",
        "zelenyy": "зеленый",
        "zeleniy": "зеленый",
        "krasnyy": "красный",
        "krasniy": "красный",
        "serebristyy": "серебристый",
        "zolotoy": "золотой",
        "fioletovyy": "фиолетовый",
        "rozovyy": "розовый",
        "zheltyy": "желтый",
        "goluboy": "голубой",
    }
    words = []
    for word in text.split():
        words.append(fixes.get(word.casefold(), word[:1].upper() + word[1:]))
    return " ".join(words)


_MODEL_STOP_WORDS = {
    "черный",
    "чёрный",
    "белый",
    "синий",
    "серый",
    "зеленый",
    "зелёный",
    "красный",
    "серебристый",
    "золотой",
    "фиолетовый",
    "розовый",
    "желтый",
    "жёлтый",
    "голубой",
    "black",
    "white",
    "blue",
    "gray",
    "grey",
    "green",
    "red",
    "silver",
    "gold",
    "purple",
    "pink",
    "yellow",
    "новый",
    "новая",
    "новое",
    "б/у",
    "бу",
    "used",
    "восстановленный",
    "восстановленное",
    "refurbished",
}


def model_query_from_title(value: str) -> str:
    """Оставляет название модели, убирая цвет/состояние/память из запроса."""
    text = str(value or "").replace("ё", "е")
    words = []
    for word in text.split():
        normalized = word.casefold().strip(" ,;")
        if normalized in _MODEL_STOP_WORDS:
            continue
        if re.fullmatch(r"\d+\s*(?:гб|gb|тб|tb)", normalized):
            continue
        words.append(word)
    return " ".join(words).strip()


def parse_avito_search_url(url: str) -> dict[str, Any]:
    """Разбирает ссылку Avito на город, запрос, цену и localPriority."""
    text = (url or "").strip()
    if not is_avito_search_url(text):
        raise ValueError("Это не ссылка Avito")
    parts = urlsplit(text)
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    # Ссылка на конкретное объявление заканчивается числовым ID, а не фильтром
    # категории/модели. Такую ссылку нельзя использовать как поисковую выдачу.
    if not params.get("q") and re.search(r"/[^/]+_\d{6,}/?$", parts.path):
        raise ValueError(
            "Похоже, это ссылка на объявление, а не на результаты поиска Avito. "
            "Откройте поиск и скопируйте ссылку из адресной строки."
        )
    city = city_from_url(text)
    first_segment = next(
        (part for part in parts.path.strip("/").split("/") if part), ""
    ).casefold()
    if first_segment in {"rossiya", "russia", "all", "vsya_rossiya"}:
        city = ""
    query = clean_text(params.get("q"))
    if not query:
        query = _title_from_avito_path(parts.path, text)

    price_min = None
    price_max = None
    for key in ("pmin", "price_min"):
        if params.get(key):
            try:
                price_min = int(re.sub(r"\D", "", params[key]))
                break
            except (TypeError, ValueError):
                pass
    for key in ("pmax", "price_max"):
        if params.get(key):
            try:
                price_max = int(re.sub(r"\D", "", params[key]))
                break
            except (TypeError, ValueError):
                pass

    local_priority = str(params.get("localPriority", "")).strip().lower() in {
        "1",
        "true",
        "yes",
    }
    raw_sort = str(params.get("s", "") or "").strip()
    if raw_sort.startswith("172297"):
        avito_sort = 172297
    else:
        try:
            avito_sort = int(raw_sort or 0)
        except (TypeError, ValueError):
            avito_sort = 0
    if avito_sort not in {0, 1, 2, 104, 172297}:
        avito_sort = 0
    return {
        "source_url": text,
        "query": query or "Поиск по ссылке Avito",
        "city": city,
        "price_min": price_min,
        "price_max": price_max,
        "local_priority": local_priority,
        "avito_sort": avito_sort,
    }


def build_effective_source_url(
    source_url: str,
    *,
    city: str = "",
    price_min: int | None = None,
    price_max: int | None = None,
    local_priority: bool = False,
    avito_sort: int = 0,
) -> str:
    """Аккуратно переопределяет известные параметры в исходной ссылке Avito."""
    parts = urlsplit(source_url)
    segments = parts.path.split("/")
    source_city = city_from_url(source_url)
    city = clean_text(city)
    if city:
        slug = city_to_slug(city)
        if slug and not (
            source_city
            and source_city.casefold().replace("ё", "е")
            == city.casefold().replace("ё", "е")
        ):
            if source_city and len(segments) > 1:
                segments[1] = slug
            elif not source_city:
                segments.insert(1, slug)
    path = "/".join(segments)

    pairs = parse_qsl(parts.query, keep_blank_values=True)

    def remove_key(key: str) -> None:
        nonlocal pairs
        pairs = [(k, v) for k, v in pairs if k != key]

    if price_min is not None:
        remove_key("pmin")
        pairs.append(("pmin", str(int(price_min))))
    if price_max is not None:
        remove_key("pmax")
        pairs.append(("pmax", str(int(price_max))))
    remove_key("localPriority")
    if local_priority:
        pairs.append(("localPriority", "1"))
    remove_key("s")
    if int(avito_sort or 0) == 172297:
        pairs.append(("s", "172297_desc"))
    elif int(avito_sort or 0) in {1, 2, 104}:
        pairs.append(("s", str(int(avito_sort))))

    return urlunsplit(
        (parts.scheme, parts.netloc, path, urlencode(pairs, doseq=True), parts.fragment)
    )


def clean_metro(value: Any, city: str = "") -> str:
    """Очищает район/метро от времени в пути и повторения города."""
    text = clean_text(value)
    if not text:
        return ""
    text = re.sub(
        r",?\s*(?:до\s*)?\d+\s*(?:[–—-]\s*\d+\s*)?мин\.?",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(
        r",?\s*\d+(?:[.,]\d+)?\s*км\s*$", "", text, flags=re.IGNORECASE
    )
    text = clean_text(text).strip(" ,·")
    if city:
        city_norm = clean_text(city).casefold().replace("ё", "е")
        text_norm = text.casefold().replace("ё", "е")
        if text_norm.startswith(city_norm):
            text = text[len(city) :].strip(" ,·")
        if text.casefold().replace("ё", "е") == city_norm:
            return ""
    return clean_text(text).strip(" ,·")


TOKEN_PATTERN = re.compile(r"(?<!\d)\d{6,12}:[A-Za-z0-9_-]{20,}")


def redact_secrets(value: Any) -> str:
    """Удаляет похожий на Telegram-токен фрагмент из текста/исключения."""
    return TOKEN_PATTERN.sub("<токен скрыт>", str(value or ""))


def find_local_chromium() -> Path | None:
    """Ищет Chromium, установленный Playwright локально для проекта."""
    roots: list[Path] = [APP_DIR / ".playwright-browsers"]
    env_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    if env_root and env_root != "0":
        roots.append(Path(env_root))
    local_app = os.environ.get("LOCALAPPDATA")
    if local_app:
        roots.append(Path(local_app) / "ms-playwright")
    patterns = (
        "chromium-*/chrome-win64/chrome.exe",
        "chromium-*/chrome-win/chrome.exe",
        "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
        "chromium-*/chrome-linux/chrome",
    )
    for root in roots:
        try:
            if not root.exists():
                continue
        except OSError:
            continue
        for pattern in patterns:
            matches = sorted(root.glob(pattern), reverse=True)
            if matches:
                return matches[0]
    return None


def _is_elevated_pid(pid: int) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        process_query_limited_information = 0x1000
        token_query = 0x0008
        token_elevation = 20
        kernel32 = ctypes.windll.kernel32
        advapi32 = ctypes.windll.advapi32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        try:
            token = wintypes.HANDLE()
            if not advapi32.OpenProcessToken(handle, token_query, ctypes.byref(token)):
                return False
            try:
                elevation = wintypes.DWORD()
                size = wintypes.DWORD()
                if not advapi32.GetTokenInformation(
                    token,
                    token_elevation,
                    ctypes.byref(elevation),
                    ctypes.sizeof(elevation),
                    ctypes.byref(size),
                ):
                    return False
                return bool(elevation.value)
            finally:
                kernel32.CloseHandle(token)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return False


def _current_process_is_elevated() -> bool:
    return _is_elevated_pid(os.getpid())


def _edge_process_is_elevated() -> bool:
    if os.name != "nt":
        return False
    try:
        output = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq msedge.exe", "/FO", "CSV", "/NH"],
            text=True,
            encoding="cp866",
            errors="ignore",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except Exception:
        return False
    for line in output.splitlines():
        fields = [field.strip('"') for field in line.split('","')]
        if len(fields) >= 2 and fields[0].casefold() == "msedge.exe":
            try:
                if _is_elevated_pid(int(fields[1])):
                    return True
            except ValueError:
                continue
    return False


def enable_windows_double_buffering(window: Any) -> bool:
    """Включает WS_EX_COMPOSITED: Windows рисует окно и дочерние виджеты без мерцания."""
    if os.name != "nt":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = int(window.winfo_id())
        parent = user32.GetParent(hwnd) or hwnd
        ex_style = user32.GetWindowLongW(parent, -20)
        if not ex_style & 0x02000000:
            user32.SetWindowLongW(parent, -20, ex_style | 0x02000000)
        # Заставляем Windows применить новый стиль без перезапуска окна.
        user32.SetWindowPos(
            parent, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0020
        )
        return True
    except Exception:
        return False


_preferred_browser = "auto"


def set_preferred_browser(value: str) -> None:
    global _preferred_browser
    _preferred_browser = (value or "auto").strip().lower()


def _find_windows_browser(preferred: str = "auto") -> str | None:
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    local_app = os.environ.get("LocalAppData", "")
    edge = str(Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe")
    if not Path(edge).is_file():
        edge = str(Path(program_files) / "Microsoft/Edge/Application/msedge.exe")
    edge_usable = Path(edge).is_file() and not (
        _edge_process_is_elevated() and not _current_process_is_elevated()
    )
    candidates = {
        "edge": [edge if edge_usable else ""],
        "chrome": [
            str(Path(program_files) / "Google/Chrome/Application/chrome.exe"),
            str(Path(program_files_x86) / "Google/Chrome/Application/chrome.exe"),
            str(Path(local_app) / "Google/Chrome/Application/chrome.exe")
            if local_app
            else "",
        ],
        "firefox": [str(Path(program_files) / "Mozilla Firefox/firefox.exe")],
    }
    preferred = (preferred or "auto").lower()
    if preferred in candidates:
        for candidate in candidates[preferred]:
            if candidate and Path(candidate).is_file():
                return candidate
        executable = {"edge": "msedge", "chrome": "chrome", "firefox": "firefox"}[
            preferred
        ]
        return shutil.which(executable)
    for candidate in (
        candidates["edge"]
        + candidates["chrome"]
        + candidates["firefox"]
    ):
        if candidate and Path(candidate).is_file():
            return candidate
    for name in (("msedge",) if edge_usable else ()) + ("chrome", "firefox"):
        found = shutil.which(name)
        if found:
            return found
    return None


def open_listing_url(url: str) -> bool:
    """Открывает объявление в браузере по умолчанию, с запасными способами."""
    if not url:
        return False
    url = str(url).strip()
    if url.startswith("/"):
        url = "https://www.avito.ru" + url
    if not url.startswith(("http://", "https://")):
        return False
    errors: list[str] = []
    if os.name == "nt":
        def shell_execute() -> bool:
            try:
                result = ctypes.windll.shell32.ShellExecuteW(
                    None, "open", url, None, None, 1
                )
                return int(result) > 32
            except Exception as exc:
                errors.append(f"ShellExecuteW: {exc}")
                return False

        if _preferred_browser == "auto" and shell_execute():
            return True
        browser = _find_windows_browser(_preferred_browser)
        if browser:
            try:
                arguments = [browser]
                if _preferred_browser != "auto":
                    arguments.append("--new-window")
                arguments.append(url)
                subprocess.Popen(arguments, close_fds=True)
                return True
            except Exception as exc:
                errors.append(f"{browser}: {exc}")
        if _preferred_browser != "auto" and shell_execute():
            return True
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            errors.append(f"startfile: {exc}")
        try:
            # explorer открывает URL в браузере по умолчанию даже из elevated-процесса.
            subprocess.Popen(["explorer.exe", url], close_fds=True)
            return True
        except Exception as exc:
            errors.append(f"explorer: {exc}")
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", url],
                close_fds=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            return True
        except Exception as exc:
            errors.append(f"cmd start: {exc}")
    try:
        if webbrowser.open(url, new=2):
            return True
    except Exception as exc:
        errors.append(f"webbrowser: {exc}")
    logger.warning(
        "Не удалось открыть ссылку в браузере по умолчанию: %s", "; ".join(errors)
    )
    return False


class _RedactingFilter(logging.Filter):
    """Страховка: даже случайно переданный токен не попадёт в лог."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        if TOKEN_PATTERN.search(message):
            record.msg = redact_secrets(message)
            record.args = ()
        return True


class RedactingFormatter(logging.Formatter):
    """Форматирует запись вместе с traceback, вырезая токен из любого места."""

    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


def setup_logging(log_path: str | Path, level: int = logging.INFO) -> logging.Logger:
    log_file = Path(log_path)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(level)
    if getattr(root, "_avito_radar_configured", False):
        return logging.getLogger("avito_radar")

    formatter = RedactingFormatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%d.%m.%Y %H:%M:%S",
    )
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=2_000_000,
        backupCount=3,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    file_handler.addFilter(_RedactingFilter())
    root.addHandler(file_handler)

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(_RedactingFilter())
    root.addHandler(console)
    root._avito_radar_configured = True  # type: ignore[attr-defined]
    logging.getLogger("avito_radar").info("Логирование запущено: %s", log_file)
    return logging.getLogger("avito_radar")


def logger_for(name: str) -> logging.Logger:
    return logging.getLogger(f"avito_radar.{name}")
