"""Small, independently testable classroom moderation rules."""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


_HOMEWORK = re.compile(r"^H\.W(?=$|\s|:|-)", re.IGNORECASE)


def is_present(member: dict) -> bool:
    """Whether getChatMember describes someone currently in the public group."""
    status = member.get("status")
    if status in {"member", "administrator", "creator"}:
        return True
    return status == "restricted" and member.get("is_member") is True


def homework(text: str | None) -> bool:
    """Preserve messages/captions starting with the explicit H.W marker."""
    return isinstance(text, str) and _HOMEWORK.match(text) is not None


def timezone_for(name: str) -> tzinfo:
    """Use system timezone data, with a modern Tehran fallback for Windows."""
    if name == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        if name == "Asia/Tehran":
            # Iran no longer observes daylight saving time. This fallback is
            # appropriate for current schedules when system IANA data is absent.
            return timezone(timedelta(hours=3, minutes=30), name="Asia/Tehran")
        raise ValueError(
            f"Timezone {name!r} is unavailable. Use UTC or Asia/Tehran, "
            "or install system IANA timezone data."
        ) from exc


def period_key(now: int, period: str, tz: tzinfo) -> str:
    """Return a local calendar key for an optional message quota reset."""
    if period in ("total", "cooldown"):
        return period
    local = datetime.fromtimestamp(now, tz)
    if period == "daily":
        return local.date().isoformat()
    if period == "weekly":
        year, week, _ = local.isocalendar()
        return f"{year}-W{week:02d}"
    raise ValueError("quota_period must be cooldown, daily, weekly, or total")


def period_label(period: str) -> str:
    labels = {
        "cooldown": "بازنشانی یک ساعت پس از اتمام سهمیه",
        "daily": "روزانه",
        "weekly": "هفتگی",
        "total": "تا بازنشانی مدیر",
    }
    try:
        return labels[period]
    except KeyError as exc:
        raise ValueError("quota_period must be cooldown, daily, weekly, or total") from exc


def next_daily_report(now: int, hour: int, minute: int, tz: tzinfo) -> int:
    """Timestamp of the next local daily report strictly after now."""
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Invalid report time")
    local = datetime.fromtimestamp(now, tz)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= local:
        target += timedelta(days=1)
    return int(target.timestamp())


def next_weekly_report(
    now: int, weekday: int, hour: int, minute: int, tz: tzinfo
) -> int:
    """Timestamp of the next report strictly after now; Monday is weekday 0."""
    if not 0 <= weekday <= 6:
        raise ValueError("report_weekday must be between 0 (Monday) and 6 (Sunday)")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("Invalid report time")
    local = datetime.fromtimestamp(now, tz)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    target += timedelta(days=(weekday - local.weekday()) % 7)
    if target <= local:
        target += timedelta(days=7)
    return int(target.timestamp())
