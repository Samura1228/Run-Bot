"""Scheduler service.

Configures an :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler` with
one daily job (09:05 — the coach-created TEAM board, then the individual
leaderboard on Mondays) and the monthly leaderboard (1st 09:00). The daily
cadence exists because a team round can start and end on any date. The
scheduler must be started on the same asyncio loop as python-telegram-bot
(via a PTB post-init hook).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from telegram import Bot
from telegram.error import TelegramError

from bot.services.leaderboard import LeaderboardService
from bot.services.sheets import TEAMS_STATUS_POSTED, SheetsService
from bot.utils.dates import (
    previous_month_bounds,
    previous_week_bounds,
    today_in,
)

logger = logging.getLogger(__name__)


async def run_weekly_leaderboard(
    bot: Bot,
    leaderboard: LeaderboardService,
    sheets: SheetsService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Post the day's boards: the team board, then Monday's individual one.

    Runs DAILY at 09:05 because a team round can end on any day. The team
    board goes first so that on a Monday the two read as one post: teams, then
    everyone's personal totals. Team-board failures are swallowed inside
    :func:`run_team_board`, so the individual board always posts regardless.

    The individual leaderboard is weekly, so it only posts on Mondays.
    """

    try:
        await run_team_board(bot, leaderboard, sheets, target_chat_id, tz)
    except Exception as exc:  # noqa: BLE001 - never block the individual board
        logger.error("Team board raised; continuing to the individual board: %s", exc)

    if today_in(tz).weekday() != 0:  # Monday
        return

    start_date, end_date = previous_week_bounds(tz)
    try:
        entries = await leaderboard.aggregate(start_date, end_date)
        message = leaderboard.format_weekly(entries, start_date, end_date)
    except Exception as exc:
        logger.error("Failed to build weekly leaderboard: %s", exc)
        return

    try:
        await bot.send_message(chat_id=target_chat_id, text=message)
        logger.info("Posted weekly leaderboard for %s–%s.", start_date, end_date)
    except TelegramError as exc:
        logger.error("Failed to send weekly leaderboard: %s", exc)


# Standings are posted every N days counted from the round's FIRST day.
TEAM_STANDINGS_EVERY_DAYS = 3


def team_board_due(
    today: date, start_date: date, end_date: date
) -> Optional[str]:
    """Return which team board is due today, or ``None``.

    ``"final"`` on the first morning AFTER the round's last day — so a round
    ending Sunday reports Monday, once the Sunday it counts has fully elapsed.
    ``"standings"`` on every :data:`TEAM_STANDINGS_EVERY_DAYS`-th day from the
    start (day 3, 6, 9 …), while the round is running.

    The start day itself is never a standings day (nothing has happened yet),
    and a round whose start is still in the future posts nothing at all.
    """

    if today > end_date:
        # Only the morning right after the round: an older round that was
        # never posted is still caught, since it stays ``active`` until it is.
        return "final"
    if today <= start_date:
        return None
    if (today - start_date).days % TEAM_STANDINGS_EVERY_DAYS == 0:
        return "standings"
    return None


async def run_team_board(
    bot: Bot,
    leaderboard: LeaderboardService,
    sheets: SheetsService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Post the team board when one is due, then retire a finished round.

    Runs daily at 09:05 ahead of the individual board. A silent no-op unless
    an ``active`` round exists and :func:`team_board_due` says today is either
    a standings day or the morning after the round ended.

    A round is marked ``posted`` only after a CONFIRMED send of the final
    board, so a failed send leaves it active and the next morning retries;
    that also makes a misfire or restart safe. Standings posts never change
    the status — they can repeat harmlessly.
    """

    try:
        current = await sheets.get_current_team_round()
    except Exception as exc:
        logger.error("Team board: failed to read the current round: %s", exc)
        return

    if current is None:
        logger.debug("No active team round; nothing to post.")
        return

    start_date = current["start_date"]
    end_date = current["end_date"]
    due = team_board_due(today_in(tz), start_date, end_date)
    if due is None:
        logger.debug(
            "Team round %s: no board due today (%s–%s).",
            current["round_id"],
            start_date,
            end_date,
        )
        return

    try:
        entries = await leaderboard.aggregate_teams(
            current["teams"], start_date, end_date
        )
        message = leaderboard.format_teams(
            entries, start_date, end_date, final=due == "final"
        )
    except Exception as exc:
        logger.error("Failed to build the team board: %s", exc)
        return

    footer = (
        f"({start_date} – {end_date})"
        if due == "final"
        else f"({start_date} – {end_date}, in progress)"
    )
    try:
        await bot.send_message(
            chat_id=target_chat_id, text=f"{message}\n\n{footer}"
        )
    except TelegramError as exc:
        # Leave the round active so the next morning retries it.
        logger.error("Failed to send the team board: %s", exc)
        return

    logger.info(
        "Posted %s team board for round %s (%s–%s).",
        due,
        current["round_id"],
        start_date,
        end_date,
    )

    if due != "final":
        return

    try:
        await sheets.set_team_round_status(
            current["round_id"], TEAMS_STATUS_POSTED
        )
    except Exception as exc:
        logger.error(
            "Team round %s posted but could not be marked posted: %s",
            current["round_id"],
            exc,
        )


async def run_monthly_leaderboard(
    bot: Bot,
    leaderboard: LeaderboardService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Post the previous calendar month's leaderboard to the target chat."""

    start_date, end_date = previous_month_bounds(tz)
    try:
        entries = await leaderboard.aggregate(start_date, end_date)
        message = leaderboard.format_monthly(entries, start_date, end_date)
    except Exception as exc:
        logger.error("Failed to build monthly leaderboard: %s", exc)
        return

    try:
        await bot.send_message(chat_id=target_chat_id, text=message)
        logger.info("Posted monthly leaderboard for %s–%s.", start_date, end_date)
    except TelegramError as exc:
        logger.error("Failed to send monthly leaderboard: %s", exc)


def build_scheduler(
    bot: Bot,
    leaderboard: LeaderboardService,
    sheets: SheetsService,
    target_chat_id: Optional[int],
    tz: str,
) -> AsyncIOScheduler:
    """Build (but do not start) the AsyncIOScheduler with cron jobs.

    Args:
        bot: PTB bot instance used by jobs to send messages.
        leaderboard: Service for aggregating & formatting leaderboards.
        sheets: Sheets service used by the weekly job to read the team round.
        target_chat_id: Chat to post leaderboards to. If ``None``, the
            leaderboard jobs are not registered (see warning below).
        tz: IANA timezone name (e.g. ``Europe/Nicosia``).

    Returns:
        A configured, not-yet-started scheduler.

    Note:
        The team board is not a separate job: it runs at the start of the
        weekly job (see :func:`run_weekly_team_board`) and is a no-op unless a
        coach-created round has finished.
    """

    zone = ZoneInfo(tz)
    scheduler = AsyncIOScheduler(timezone=zone)

    # Without a target chat the leaderboards have nowhere to post; skip the jobs
    # entirely (rather than fire and fail every week/month) and warn clearly.
    if target_chat_id is None:
        logger.warning(
            "TARGET_CHAT_ID not set — weekly/monthly leaderboards will not be "
            "posted. Run /chatid in your group to discover the ID, then set "
            "TARGET_CHAT_ID and redeploy."
        )
        return scheduler

    # One daily job posts the TEAM board (when due) and then the individual
    # board on Mondays; the monthly job is independent. Each swallows its own
    # errors, so a failure in one can never stop the other from posting.
    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(hour=9, minute=5, timezone=zone),
        args=[bot, leaderboard, sheets, target_chat_id, tz],
        id="daily_boards",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )
    scheduler.add_job(
        run_monthly_leaderboard,
        CronTrigger(day=1, hour=9, minute=0, timezone=zone),
        args=[bot, leaderboard, target_chat_id, tz],
        id="monthly_leaderboard",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )

    logger.info(
        "Scheduler configured: daily boards 09:05 (team board when due, "
        "individual on Mondays) & monthly (1st 09:00) in %s.",
        tz,
    )
    return scheduler