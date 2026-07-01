"""Timezone-aware week/month boundary helpers.

All boundary math is done in the configured timezone (default Europe/Nicosia),
then reduced to plain calendar :class:`datetime.date` values. Because the
eligibility comparison is date-based, there is no ambiguity around midnight or
DST — the timezone is only used to determine what "today" is.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


def today_in(tz: str) -> date:
    """Return today's calendar date in the given IANA timezone."""

    return datetime.now(ZoneInfo(tz)).date()


def current_week_bounds(tz: str) -> tuple[date, date]:
    """Return the current Mon–Sun week bounds (inclusive) for ``tz``.

    Returns:
        A ``(week_start, week_end)`` tuple where ``week_start`` is Monday and
        ``week_end`` is Sunday.
    """

    today = today_in(tz)
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday
    return week_start, week_end


def previous_week_bounds(tz: str) -> tuple[date, date]:
    """Return the previous full Mon–Sun week bounds (inclusive) for ``tz``.

    Used by the Monday-morning weekly leaderboard job.
    """

    today = today_in(tz)
    this_monday = today - timedelta(days=today.weekday())
    prev_week_start = this_monday - timedelta(days=7)
    prev_week_end = this_monday - timedelta(days=1)  # previous Sunday
    return prev_week_start, prev_week_end


def previous_month_bounds(tz: str) -> tuple[date, date]:
    """Return the previous full calendar month bounds (inclusive) for ``tz``.

    Used by the 1st-of-month monthly leaderboard job.
    """

    today = today_in(tz)
    first_of_this_month = today.replace(day=1)
    prev_month_end = first_of_this_month - timedelta(days=1)
    prev_month_start = prev_month_end.replace(day=1)
    return prev_month_start, prev_month_end


def in_range(day: date, start: date, end: date) -> bool:
    """Return True if ``day`` falls within ``[start, end]`` inclusive."""

    return start <= day <= end