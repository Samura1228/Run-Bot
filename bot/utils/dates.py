"""Timezone-aware week/month boundary helpers.

All boundary math is done in the configured timezone (default Europe/Nicosia),
then reduced to plain calendar :class:`datetime.date` values. Because the
eligibility comparison is date-based, there is no ambiguity around midnight or
DST — the timezone is only used to determine what "today" is.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

# Guard rails for a team round length (in days), so a typo like "28/09/26 -
# 04/10/36" can't create a round that never ends.
MIN_ROUND_DAYS = 1
MAX_ROUND_DAYS = 365

# "28/09/26 - 04/10/26" and friends. Day and month first (DD/MM — never MM/DD),
# a 2- or 4-digit year, ``/ . -`` inside a date, and ``- – —`` between the two.
# ``\d{4}`` is tried before ``\d{2}`` so "2026" is not read as "20" with "26"
# left over. Note a date may itself use "-", which is why the whole range is
# matched in one anchored pattern rather than split on the dash.
_DATE_RANGE_RE = re.compile(
    r"^\s*(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})"
    r"\s*[-–—]\s*"
    r"(\d{1,2})[./-](\d{1,2})[./-](\d{4}|\d{2})\s*$"
)


def _build_date(day: str, month: str, year: str) -> Optional[date]:
    """Build a date from DD, MM and a 2- or 4-digit year, or None if invalid."""

    value = int(year)
    if len(year) == 2:
        value += 2000
    try:
        return date(value, int(month), int(day))
    except ValueError:
        return None


def parse_date_range(raw: str) -> Optional[tuple[date, date]]:
    """Parse ``"28/09/26 - 04/10/26"`` into an inclusive ``(start, end)``.

    Day comes FIRST (DD/MM), matching how the coach writes dates; the year may
    be 2 or 4 digits. Returns ``None`` when the text is malformed, either date
    is not a real calendar date, the end is before the start, or the span falls
    outside :data:`MIN_ROUND_DAYS`–:data:`MAX_ROUND_DAYS` — the caller turns any
    of those into one "nothing was saved" reply.
    """

    match = _DATE_RANGE_RE.match(raw or "")
    if match is None:
        return None
    start = _build_date(*match.group(1, 2, 3))
    end = _build_date(*match.group(4, 5, 6))
    if start is None or end is None:
        return None
    span = (end - start).days + 1
    if not MIN_ROUND_DAYS <= span <= MAX_ROUND_DAYS:
        return None
    return start, end


def now_in(tz: str) -> datetime:
    """Return the current timezone-aware moment in the given IANA timezone."""

    return datetime.now(ZoneInfo(tz))


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


def week_bounds_containing(day: date) -> tuple[date, date]:
    """Return the Mon–Sun week bounds (inclusive) that contain ``day``.

    Timezone-free on purpose: the argument is already a calendar date. Used to
    score a workout against the week its ``workout_date`` actually belongs to
    (which, for an accepted late submission, is the PREVIOUS week — not the week
    it was submitted in).
    """

    week_start = day - timedelta(days=day.weekday())  # Monday
    return week_start, week_start + timedelta(days=6)  # Sunday


def accepted_workout_window(
    tz: str,
    grace_until_hour: int,
    now: Optional[datetime] = None,
) -> tuple[date, date]:
    """Return the inclusive date window a submission may be dated within.

    Normally this is just the current Mon–Sun week. During the late-submission
    grace period — MONDAY before ``grace_until_hour`` local time, i.e. before
    the weekly leaderboard posts — the window is EXTENDED backwards to include
    the whole previous Mon–Sun week, so a workout finished on Sunday but posted
    just after midnight still counts toward the week being reported at 09:00.

    Args:
        tz: IANA timezone name used to resolve "now" and the week boundaries.
        grace_until_hour: The Monday hour (0–23) until which previous-week
            workouts are still accepted. ``0`` disables the grace period.
        now: Optional explicit submission moment (any timezone; it is converted
            to ``tz``). Defaults to the current time.

    Returns:
        An ``(start, end)`` tuple of calendar dates, inclusive, where ``end`` is
        always the current week's Sunday.
    """

    zone = ZoneInfo(tz)
    moment = now.astimezone(zone) if now is not None else now_in(tz)
    today = moment.date()
    week_start = today - timedelta(days=today.weekday())  # Monday
    week_end = week_start + timedelta(days=6)  # Sunday

    if (
        grace_until_hour > 0
        and moment.weekday() == 0  # Monday
        and moment.hour < grace_until_hour
    ):
        return week_start - timedelta(days=7), week_end
    return week_start, week_end


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