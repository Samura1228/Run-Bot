"""Scheduler service.

Configures an :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler` with
cron jobs for the weekly (Mon 09:00) and monthly (1st 09:00) leaderboards. The
scheduler must be started on the same asyncio loop as python-telegram-bot (via
a PTB post-init hook).
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
from bot.utils.dates import previous_month_bounds, previous_week_bounds

logger = logging.getLogger(__name__)


async def run_weekly_leaderboard(
    bot: Bot,
    leaderboard: LeaderboardService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Post the previous Mon–Sun week's leaderboard to the target chat."""

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
    target_chat_id: Optional[int],
    tz: str,
) -> AsyncIOScheduler:
    """Build (but do not start) the AsyncIOScheduler with cron jobs.

    Args:
        bot: PTB bot instance used by jobs to send messages.
        leaderboard: Service for aggregating & formatting leaderboards.
        target_chat_id: Chat to post leaderboards to. If ``None``, the
            leaderboard jobs are not registered (see warning below).
        tz: IANA timezone name (e.g. ``Europe/Nicosia``).

    Returns:
        A configured, not-yet-started scheduler.
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

    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=zone),
        args=[bot, leaderboard, target_chat_id, tz],
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
        "Scheduler configured: weekly (Mon 09:00) & monthly (1st 09:00) in %s.",
        tz,
    )
    return scheduler