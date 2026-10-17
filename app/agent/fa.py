"""Persian text and date helpers (Asia/Tehran, Jalali calendar)."""

import datetime as dt
import re
from zoneinfo import ZoneInfo

import jdatetime

TEHRAN = ZoneInfo("Asia/Tehran")

_FA_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_AR_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_TO_ASCII = str.maketrans(_FA_DIGITS + _AR_DIGITS, "0123456789" * 2)
_TO_FA = str.maketrans("0123456789", _FA_DIGITS)

WEEKDAYS_FA = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # Monday=0
JALALI_MONTHS = [
    "فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
    "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند",
]


def to_ascii_digits(text: str) -> str:
    return text.translate(_TO_ASCII)


def to_fa_digits(text: str | int) -> str:
    return str(text).translate(_TO_FA)


def normalize(text: str) -> str:
    """Lower-case, unify Arabic/Persian letters and digits, collapse spaces and ZWNJ."""
    text = to_ascii_digits(text).replace("ي", "ی").replace("ك", "ک").replace("ة", "ه")
    text = text.replace("‌", " ")
    return re.sub(r"\s+", " ", text).strip().lower()


def now_tehran() -> dt.datetime:
    return dt.datetime.now(TEHRAN)


def jalali(d: dt.date) -> jdatetime.date:
    return jdatetime.date.fromgregorian(date=d)


def jalali_iso(d: dt.date) -> str:
    j = jalali(d)
    return f"{j.year:04d}-{j.month:02d}-{j.day:02d}"


def weekday_fa(d: dt.date) -> str:
    return WEEKDAYS_FA[d.weekday()]


def format_date_fa(d: dt.date) -> str:
    """e.g. «سه‌شنبه ۲۸ مهر ۱۴۰۵»"""
    j = jalali(d)
    return f"{weekday_fa(d)} {to_fa_digits(j.day)} {JALALI_MONTHS[j.month - 1]} {to_fa_digits(j.year)}"


def parse_date(value: str) -> dt.date:
    """Parse YYYY-MM-DD (also with / and Persian digits). Years below 1700 are Jalali."""
    cleaned = to_ascii_digits(value).strip().replace("/", "-")
    match = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", cleaned)
    if not match:
        raise ValueError(f"date must look like YYYY-MM-DD, got {value!r}")
    year, month, day = (int(g) for g in match.groups())
    if year < 1700:
        return jdatetime.date(year, month, day).togregorian()
    return dt.date(year, month, day)


def parse_time(value: str) -> dt.time:
    cleaned = to_ascii_digits(value).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2})?", cleaned)
    if not match:
        raise ValueError(f"time must look like HH:MM (24h), got {value!r}")
    return dt.time(int(match.group(1)), int(match.group(2)))


def calendar_table(today: dt.date, days: int = 31) -> str:
    """Upcoming dates with Jalali equivalents, so the model never has to do calendar arithmetic."""
    lines = []
    for offset in range(days):
        d = today + dt.timedelta(days=offset)
        lines.append(f"{d.isoformat()} | {jalali_iso(d)} | {weekday_fa(d)}")
    return "\n".join(lines)


def has_persian(text: str) -> bool:
    return bool(re.search(r"[؀-ۿ]", text))
