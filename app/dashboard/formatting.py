"""Persian-locale number formatting for the dashboard templates (Persian digits, ٬ and ٫)."""

import datetime as dt
import json

from app.agent.fa import TEHRAN, format_date_fa, to_fa_digits

DASH = "—"


def fa_number(value: float | int | None, decimals: int = 0) -> str:
    if value is None:
        return DASH
    text = f"{value:,.{decimals}f}"
    return to_fa_digits(text.replace(",", "٬").replace(".", "٫"))


def fa_compact(value: float | int | None) -> str:
    """1٫۲ میلیون, ۳۴٫۵ هزار, or the plain number below a thousand."""
    if value is None:
        return DASH
    if abs(value) >= 1_000_000:
        return f"{fa_number(value / 1_000_000, 1)} میلیون"
    if abs(value) >= 10_000:
        return f"{fa_number(value / 1_000, 1)} هزار"
    return fa_number(value)


def fa_percent(fraction: float | None, decimals: int = 1) -> str:
    return DASH if fraction is None else f"{fa_number(fraction * 100, decimals)}٪"


def fa_usd(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{fa_number(value, 4 if abs(value) < 1 else 2)} $"


def fa_ms(value: float | None) -> str:
    if value is None:
        return DASH
    return f"{fa_number(value / 1000, 2)} ث" if value >= 1000 else f"{fa_number(value)} م‌ث"


def fa_datetime(value: dt.datetime | None) -> str:
    if value is None:
        return DASH
    local = value.astimezone(TEHRAN)
    return f"{format_date_fa(local.date())}، {to_fa_digits(local.strftime('%H:%M'))}"


def pretty_json(value) -> str:
    """Indented JSON that keeps Persian readable (Jinja's tojson escapes it to \\uXXXX); autoescaped in HTML."""
    if value is None:
        return DASH
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


FILTERS = {
    "pretty_json": pretty_json,
    "fa_number": fa_number, "fa_compact": fa_compact, "fa_percent": fa_percent, "fa_usd": fa_usd,
    "fa_ms": fa_ms, "fa_datetime": fa_datetime, "fa_digits": lambda v: to_fa_digits(str(v)),
}
