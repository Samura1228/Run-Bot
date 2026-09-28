"""Scheduler service.

Configures an :class:`~apscheduler.schedulers.asyncio.AsyncIOScheduler` with
cron jobs for the **pairs round** board (daily 09:00 — posts only when a
coach-created round has ended, so a round finishing Sunday is reported Monday
09:00), the weekly individual leaderboard (Mon 09:05) and the monthly
leaderboard (1st 09:00). The scheduler must be started on the same asyncio loop
as python-telegram-bot (via a PTB post-init hook).
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
from bot.services.sheets import PAIRS_STATUS_POSTED, SheetsService
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
    """Post the previous week's leaderboard.

    Aggregated
    so this week's board reflects them.
    """

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


async def run_pairs_round_board(
    bot: Bot,
    leaderboard: LeaderboardService,
    sheets: SheetsService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Post the FINAL board for a coach-created pairs round that has ended.

    Runs daily at 09:00 and does nothing unless an ``active`` round exists whose
    ``end_date`` has passed — i.e. the board posts at 09:00 on the day AFTER the
    round ends (a round ending Sunday posts Monday 09:00, matching the old fixed
    schedule). If no round is configured, or the current one is still running,
    this is a silent no-op: **pairs are only tracked while a round is active**.

    Once posted, the round is marked ``posted`` so it is never reported twice —
    the guard also makes a scheduler misfire/restart safe. A round whose window
    closed while the bot was down is still picked up on the next daily run,
    rather than being lost.

    Failures are logged and swallowed so this can never prevent the individual
    weekly board (a separate job) from posting.
    """

    try:
        current = await sheets.get_current_pairs_round()
    except Exception as exc:
        logger.error("Pairs board: failed to read the current round: %s", exc)
        return

    if current is None:
        logger.debug("No active pairs round; nothing to post.")
        return

    today = today_in(tz)
    end_date = current["end_date"]
    if today <= end_date:
        logger.debug(
            "Pairs round %s runs until %s; not posting yet.",
            current["round_id"],
            end_date,
        )
        return

    start_date = current["start_date"]

    try:
        entries = await leaderboard.aggregate_pairs(
            current["members"], start_date, end_date
        )
        message = leaderboard.format_pairs(entries, start_date, end_date)
    except Exception as exc:
        logger.error("Failed to build the pairs round board: %s", exc)
        return

    try:
        await bot.send_message(
            chat_id=target_chat_id,
            text=f"{message}\n\n({start_date} – {end_date})",
        )
    except TelegramError as exc:
        # Leave the round active so the next daily run retries it.
        logger.error("Failed to send the pairs round board: %s", exc)
        return

    logger.info(
        "Posted final pairs board for round %s (%s–%s).",
        current["round_id"],
        start_date,
        end_date,
    )

    # Close the round LAST: only a confirmed send may retire it.
    try:
        await sheets.set_pairs_round_status(
            current["round_id"], PAIRS_STATUS_POSTED
        )
    except Exception as exc:
        logger.error(
            "Pairs round %s posted but could not be marked posted: %s",
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
        sheets: Sheets service used by the pairs job to read the current round.
        target_chat_id: Chat to post leaderboards to. If ``None``, the
            leaderboard jobs are not registered (see warning below).
        tz: IANA timezone name (e.g. ``Europe/Nicosia``).

    Returns:
        A configured, not-yet-started scheduler.

    Note:
        The pairs job is registered unconditionally but is a no-op unless a
        coach-created round has finished (see :func:`run_pairs_round_board`);
        pairs are no longer configured at deploy time.
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

    # Pairs round board first (daily 09:00 — a no-op unless a coach-created
    # round has ended), then the individual board (Mon 09:05). Each job is
    # registered independently and swallows its own errors, so a failure in one
    # can never stop the other from posting.
    scheduler.add_job(
        run_pairs_round_board,
        CronTrigger(hour=9, minute=0, timezone=zone),
        args=[bot, leaderboard, sheets, target_chat_id, tz],
        id="pairs_round_board",
        misfire_grace_time=3600,
        coalesce=True,
        replace_existing=True,
    )
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
        "Scheduler configured: pairs round board (daily 09:00, posts only when "
        "a coach-created round has ended), weekly (Mon 09:05) & monthly "
        "(1st 09:00) in %s.",
        tz,
    )
    return scheduler