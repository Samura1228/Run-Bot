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

from bot.models import WorkoutLogRow
from bot.services.leaderboard import LeaderboardService
from bot.services.sheets import PAIRS_STATUS_POSTED, SheetsService
from bot.utils.dates import (
    previous_month_bounds,
    previous_week_bounds,
    today_in,
)
from bot.utils.points import DEFAULT_PLAN, streak_bonus

logger = logging.getLogger(__name__)


async def evaluate_weekly_streaks(
    sheets: SheetsService,
    tz: str,
) -> None:
    """Evaluate streaks for the previous Mon–Sun week and award streak bonuses.

    For each user with a plan row (plus any user who logged running workouts in
    the previous week but has no plan row → treated as :data:`DEFAULT_PLAN`):

    1. Count their completed running workouts in the previous week.
    2. If ``completed >= plan`` → increment their streak, else reset to 0.
    3. Persist the new streak (upsert, preserving plan/username).
    4. If the new streak's bonus is > 0, append a ``streak_bonus`` row to the
       ``Log`` worksheet dated to the previous week's Sunday, so it falls within
       that week and is picked up by the leaderboard aggregation.

    Idempotent-ish: before awarding, it checks the Log for an existing
    ``streak_bonus`` row for that user dated to the same previous-week Sunday and
    skips if present, avoiding double-counting on scheduler misfire/coalesce.
    """

    prev_start, prev_end = previous_week_bounds(tz)

    try:
        plan_rows = await sheets.list_plans()
    except Exception as exc:
        logger.error("Streak rollover: failed to list plans: %s", exc)
        plan_rows = []

    # Build a user_id -> {plan, streak, username} map from plan rows.
    users: dict[int, dict] = {}
    for row in plan_rows:
        users[row["user_id"]] = {
            "plan": row["plan"],
            "streak": row["streak"],
            "username": row["username"],
        }

    # Also include any user who logged running workouts in the previous week but
    # has no plan row (treated as DEFAULT_PLAN, streak 0).
    try:
        prev_rows = await sheets.read_rows_in_range(prev_start, prev_end)
    except Exception as exc:
        logger.error("Streak rollover: failed to read previous week rows: %s", exc)
        prev_rows = []
    for row in prev_rows:
        uid = row["telegram_user_id"]
        if uid not in users:
            users[uid] = {
                "plan": DEFAULT_PLAN,
                "streak": 0,
                "username": row.get("telegram_username", ""),
            }
        elif not users[uid]["username"]:
            # Backfill username from the log if the plan row lacked one.
            users[uid]["username"] = row.get("telegram_username", "")

    for uid, info in users.items():
        plan = info["plan"]
        old_streak = info["streak"]
        username = info["username"]

        try:
            completed = await sheets.count_user_workouts_in_week(
                uid, prev_start, prev_end
            )
        except Exception as exc:
            logger.error(
                "Streak rollover: failed to count workouts for user %s: %s",
                uid,
                exc,
            )
            continue

        new_streak = old_streak + 1 if completed >= plan else 0

        try:
            await sheets.set_streak(uid, new_streak, username=username, plan=plan)
        except Exception as exc:
            logger.error(
                "Streak rollover: failed to persist streak for user %s: %s",
                uid,
                exc,
            )
            # Continue; still attempt bonus below only if streak persisted? We
            # persisted-or-failed; skip bonus on failure to avoid inconsistency.
            continue

        bonus = streak_bonus(new_streak) if new_streak >= 1 else 0

        logger.info(
            "Streak: user=%s completed=%s/%s streak=%s bonus=%s",
            uid,
            completed,
            plan,
            new_streak,
            bonus,
        )

        if bonus <= 0:
            continue

        # Idempotency guard: skip if a streak_bonus row already exists for this
        # user dated to the previous week's Sunday.
        try:
            already = await sheets.has_streak_bonus_for_date(uid, prev_end)
        except Exception as exc:
            logger.error(
                "Streak rollover: dedup check failed for user %s: %s", uid, exc
            )
            already = False
        if already:
            logger.info(
                "Streak: bonus already recorded for user=%s date=%s; skipping.",
                uid,
                prev_end,
            )
            continue

        bonus_row = WorkoutLogRow(
            timestamp=WorkoutLogRow.now_timestamp(),
            telegram_user_id=uid,
            telegram_username=username,
            display_name="",
            workout_date=prev_end.isoformat(),
            activity_type="streak_bonus",
            points=bonus,
            image_hash="-",
            telegram_file_id="-",
            chat_id=0,
            message_id=0,
        )
        try:
            await sheets.append_workout(bonus_row)
        except Exception as exc:
            logger.error(
                "Streak rollover: failed to append bonus row for user %s: %s",
                uid,
                exc,
            )


async def run_weekly_leaderboard(
    bot: Bot,
    leaderboard: LeaderboardService,
    sheets: SheetsService,
    target_chat_id: int,
    tz: str,
) -> None:
    """Award streak bonuses, then post the previous week's leaderboard.

    Streak bonuses are evaluated/recorded BEFORE the leaderboard is aggregated
    so this week's board reflects them.
    """

    # Record streak bonuses first so they're included in the aggregation below.
    try:
        await evaluate_weekly_streaks(sheets, tz)
    except Exception as exc:
        logger.error("Streak rollover raised; continuing to leaderboard: %s", exc)

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

    # Record streak bonuses first so they're included in the aggregation below
    # (idempotent — a no-op if the weekly 09:05 job already recorded them).
    try:
        await evaluate_weekly_streaks(sheets, tz)
    except Exception as exc:
        logger.error(
            "Streak rollover raised; continuing to pairs board: %s", exc
        )

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
        sheets: Sheets service used by the weekly job for the streak rollover
            and by the pairs job to read the current round.
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