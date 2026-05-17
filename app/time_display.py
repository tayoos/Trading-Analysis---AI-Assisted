"""
Format UTC timestamps for the UI using the container TZ env var (e.g. Europe/London).
Times are always stored in UTC; only display is localized.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def display_timezone_name() -> str:
    """IANA timezone from TZ env, or empty to mean browser-local in JS."""
    return (os.getenv("TZ") or "").strip()


def display_timezone() -> ZoneInfo:
    name = display_timezone_name()
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    return ZoneInfo("UTC")


def format_datetime(iso: str | None) -> str:
    """Format a UTC ISO timestamp in the configured display timezone."""
    if not iso:
        return "Never"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso[:19].replace("T", " ")

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    local = dt.astimezone(display_timezone())
    abbrev = local.tzname() or display_timezone_name() or "UTC"
    return f"{local.strftime('%d/%m/%Y, %H:%M:%S')} {abbrev}"
