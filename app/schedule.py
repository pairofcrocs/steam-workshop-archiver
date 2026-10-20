"""Schedule validation, next-run computation and human descriptions."""

import math
import os
import time
from datetime import datetime, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from croniter import croniter

MAX_INTERVAL_HOURS = 24 * 366
_DAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


def local_zone() -> tzinfo:
    """The container's IANA timezone (TZ, then /etc/localtime), so cron times follow DST.
    A fixed offset from ``astimezone()`` would drift by an hour across DST changes."""
    name = os.environ.get("TZ", "").lstrip(":")
    if name:
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            pass
    try:
        with open("/etc/localtime", "rb") as f:
            return ZoneInfo.from_file(f)
    except (OSError, ValueError):
        return datetime.now().astimezone().tzinfo


def validate(schedule_type: str, value: str) -> str | None:
    """Error message, or None when valid."""
    if schedule_type == "none":
        return None
    if schedule_type == "interval":
        try:
            hours = float(value)
        except (TypeError, ValueError):
            return "Interval must be a number of hours."
        if not math.isfinite(hours) or hours < 0.25 or hours > MAX_INTERVAL_HOURS:
            return f"Interval must be between 0.25 and {MAX_INTERVAL_HOURS} hours."
        return None
    if schedule_type == "cron":
        if len((value or "").split()) != 5:
            return "Cron expressions need exactly 5 fields: minute hour day month weekday."
        if not croniter.is_valid(value):
            return f"Invalid cron expression: {value!r}"
        try:
            next_run("cron", value)
        except (ValueError, KeyError, OverflowError):  # e.g. "0 0 31 2 *" never happens
            return f"That cron expression never matches a real date: {value!r}"
        return None
    return "Schedule type must be 'none', 'interval' or 'cron'."


def next_run(schedule_type: str, value: str, after: float | None = None, prev_due: float | None = None) -> float | None:
    """Next run strictly after *after* (default now), in the container's local timezone.

    Intervals stay anchored to the previous due time so they don't drift."""
    now = time.time() if after is None else after
    if schedule_type == "interval":
        period = float(value) * 3600
        if prev_due:
            missed = max(0, math.floor((now - prev_due) / period) + 1)
            return prev_due + missed * period
        return now + period
    if schedule_type == "cron":
        start = datetime.fromtimestamp(now, tz=local_zone())
        return croniter(value, start).get_next(float)
    return None


def upcoming(schedule_type: str, value: str, count: int = 3) -> list[float]:
    out: list[float] = []
    t = time.time()
    for _ in range(count):
        nxt = next_run(schedule_type, value, after=t, prev_due=out[-1] if out and schedule_type == "interval" else None)
        if nxt is None:
            break
        out.append(nxt)
        t = nxt
    return out


def describe(schedule_type: str, value: str) -> str:
    if schedule_type == "none":
        return "Manual only"
    if schedule_type == "interval":
        h = float(value)
        if h % 24 == 0:
            days = int(h // 24)
            return "Every day" if days == 1 else f"Every {days} days"
        if h == 1:
            return "Every hour"
        return f"Every {h:g} hours"
    minute, hour, dom, month, dow = value.split()
    if minute.isdigit() and hour.isdigit():
        at = f"{int(hour):02d}:{int(minute):02d}"
        if dom == "*" and month == "*" and dow == "*":
            return f"Every day at {at}"
        if dom == "*" and month == "*" and dow.isdigit() and int(dow) <= 7:
            return f"Every {_DAYS[int(dow) % 7]} at {at}"
        if dom.isdigit() and month == "*" and dow == "*":
            return f"Monthly on day {dom} at {at}"
    if minute.isdigit() and hour.startswith("*/") and dom == month == dow == "*":
        return f"Every {hour[2:]} hours at :{int(minute):02d}"
    if minute.startswith("*/") and hour == dom == month == dow == "*":
        return f"Every {minute[2:]} minutes"
    return f"Cron: {value}"
