"""Jinja2 setup shared by the HTML pages."""
from datetime import datetime, timezone

from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")


def localtime(value: datetime | None) -> str:
    """Show a UTC datetime in the server's local time zone (display only).

    Naive values are the DB convention (naive UTC); aware ones come from the API schemas.
    """
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def duration(ms: int | None) -> str:
    if ms is None:
        return "—"
    return f"{ms} ms" if ms < 1000 else f"{ms / 1000:.1f} s"


templates.env.filters["localtime"] = localtime
templates.env.filters["duration"] = duration
