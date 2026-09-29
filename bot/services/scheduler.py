"""Scheduler service.

Configures an :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler` with
cron jobs for the weekly boards (Mon 09:05 — the coach-created TEAM board
followed by the individual leaderboard) and the monthly leaderboard (1st
09:00). The scheduler must be started on the same asyncio loop as
python-telegram-bot (via a PTB post-init hook).
"""

from __future__ import annotations

import logging
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
    """Post the team board, then the previous week's individual leaderboard.

    The team board goes FIRST so the two read as one Monday post: teams, then
    everyone's personal totals. It is a no-op when no team round is active, and
    its failures are swallowed inside :func:`run_weekly_team_board`, so the
    individual board always posts regardless.
    """

    try:
        await run_weekly_team_board(bot, leaderboard, sheets, target_chat_id, tz)
    except Exception as exc:  # noqa: BLE001 - never block the individual board
        logger.error("Team board raised; continuing to the individual board: %s", exc)

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


async def run_weekly_team_board(
    bot: Bot,
    leaderboard: LeaderboardService,
    sheets: SheetsService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Post the TEAM board for the just-finished week, then retire the round.

    Called at the start of the Monday 09:05 job, so the team board lands just
    above the individual one. A silent no-op unless an ``active`` round exists
    whose week has ended: teams are only tracked while a round is active, and
    a round created mid-week is reported on the following Monday.

    Once posted the round is marked ``posted`` so it can never be reported
    twice (which also makes a scheduler misfire or a restart safe). The status
    is written LAST, only after a confirmed send, so a failed send leaves the
    round active and it is retried next Monday.
    """

    try:
        current = await sheets.get_current_team_round()
    except Exception as exc:
        logger.error("Team board: failed to read the current round: %s", exc)
        return

    if current is None:
        logger.debug("No active team round; nothing to post.")
        return

    today = today_in(tz)
    end_date = current["end_date"]
    if today <= end_date:
        logger.debug(
            "Team round %s runs until %s; not posting yet.",
            current["round_id"],
            end_date,
        )
        return

    start_date = current["start_date"]
    try:
        entries = await leaderboard.aggregate_teams(
            current["teams"], start_date, end_date
        )
        message = leaderboard.format_teams(entries, start_date, end_date)
    except Exception as exc:
        logger.error("Failed to build the team board: %s", exc)
        return

    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=f"{message}\n\n({start_date} – {end_date})",
        )
    except TelegramError as exc:
        # Leave the round active so next Monday retries it.
        logger.error("Failed to send the team board: %s", exc)
        return

    logger.info(
        "Posted team board for round %s (%s–%s).",
        current["round_id"],
        start_date,
        end_date,
    )

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

    # The weekly job posts the TEAM board and then the individual board; the
    # monthly job is independent. Each swallows its own errors, so a failure in
    # one can never stop the other from posting.
    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=zone),
        args=[bot, leaderboard, sheets, target_chat_id, tz],
        id="weekly_leaderboard",
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
        "Scheduler configured: weekly team + individual boards (Mon 09:05) & "
        "monthly (1st 09:00) in %s.",
        tz,
    )
    return scheduler