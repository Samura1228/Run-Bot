"""Photo handler.

Orchestrates the full pipeline for photo messages:
download → hash → dedup → vision → decision → silent log.

On a successful, eligible current-week run the row is written to the Google
Sheet FIRST; once the write is confirmed and the INFO log is emitted, the bot
replies to the chat with "✅ Nice run, {name}! +{points} points.". All
non-eligible / failure paths are handled silently (no chat reply), remaining
observable only via logs, per the blueprint.
"""

from __future__ import annotations

import logging
from datetime import date

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.models import WorkoutLogRow
from bot.services.leaderboard import LeaderboardService  # noqa: F401 (type hints)
from bot.services.sheets import SheetsService
from bot.services.vision import ClaudeVisionService
from bot.utils.dates import current_week_bounds, in_range
from bot.utils.hashing import compute_image_hash
from bot.utils.points import (
    ACTIVITY_MIN_MINUTES,
    BONUS_ACTIVITIES,
    BONUS_ACTIVITY_POINTS,
    DEFAULT_PLAN,
    activity_label,
    format_points,
    workout_points,
)

# Human-friendly nouns for the below-minimum-duration warning per activity.
_BELOW_MIN_NOUN = {
    "walking": "Walk",
    "cycling": "Ride",
    "strength": "Strength/stretch",
}

logger = logging.getLogger(__name__)


class PhotoHandler:
    """Callable handler for incoming photo messages."""

    def __init__(
        self,
        settings: Settings,
        vision: ClaudeVisionService,
        sheets: SheetsService,
        activity_points: dict[str, int],
    ) -> None:
        self._settings = settings
        self._vision = vision
        self._sheets = sheets
        self._activity_points = activity_points

    async def __call__(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Entry point registered with a ``MessageHandler(filters.PHOTO)``."""

        message = update.effective_message
        if message is None or not message.photo:
            return

        user = message.from_user
        if user is None:
            return

        # 1) Download the largest photo's bytes.
        largest = message.photo[-1]
        try:
            tg_file = await context.bot.get_file(largest.file_id)
            image_bytearray = await tg_file.download_as_bytearray()
            image_bytes = bytes(image_bytearray)
        except TelegramError as exc:
            logger.warning("Failed to download photo: %s", exc)
            return
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Unexpected error downloading photo: %s", exc)
            return

        if not image_bytes:
            logger.warning("Downloaded empty image bytes; ignoring.")
            return

        # 2) Compute hash for dedup.
        image_hash = compute_image_hash(image_bytes)

        # 3) Dedup check BEFORE the costly vision call.
        try:
            if await self._sheets.is_duplicate(user.id, image_hash):
                logger.info(
                    "Duplicate submission from user %s; ignoring.", user.id
                )
                return
        except Exception as exc:
            logger.error("Dedup check failed: %s", exc)
            # Fail-open: continue; a race-safe re-check runs before append.

        # 4) Vision analysis.
        verdict = await self._vision.analyze(image_bytes)
        if verdict is None:
            # Parse/API/validation failure → silent ignore.
            return

        # 5) Eligibility.
        if not verdict.is_eligible(self._settings.min_confidence):
            logger.info(
                "Verdict not eligible (garmin=%s type=%s completed=%s "
                "date=%s conf=%.2f); ignoring.",
                verdict.is_garmin,
                verdict.activity_type,
                verdict.is_completed,
                verdict.workout_date,
                verdict.confidence,
            )
            return

        # Gate: only awardable activity types proceed. Running uses the
        # plan-based model; walking/cycling/strength are flat bonus activities.
        # Anything else ("other"/unrecognized) is silently ignored.
        activity = verdict.activity_type
        if activity != "running" and activity not in BONUS_ACTIVITIES:
            logger.info(
                "Activity type %r is not awardable; ignoring.",
                activity,
            )
            return

        # 6) Date-window: must be within the current Mon–Sun week.
        assert verdict.workout_date is not None  # guaranteed by eligibility
        try:
            wdate = date.fromisoformat(verdict.workout_date)
        except ValueError:
            logger.warning("Invalid workout_date after validation; ignoring.")
            return

        week_start, week_end = current_week_bounds(self._settings.timezone)
        if not in_range(wdate, week_start, week_end):
            logger.info(
                "Workout date %s is outside current week (%s–%s); ignoring.",
                wdate,
                week_start,
                week_end,
            )
            return

        # 7) Race-safe dedup re-check just before append.
        try:
            if await self._sheets.is_duplicate(user.id, image_hash):
                logger.info(
                    "Duplicate detected on re-check for user %s; ignoring.",
                    user.id,
                )
                return
        except Exception as exc:
            logger.error("Race-safe dedup re-check failed: %s", exc)

        # 7b) Points decision — branch by activity_type.
        # Identity fields are needed for both the row and the reply.
        username = (user.username or "").strip()
        display_name_early = " ".join(
            part for part in [user.first_name, user.last_name] if part
        ).strip()
        who = f"@{username}" if username else (
            display_name_early or user.first_name or "runner"
        )

        if activity == "running":
            # RUNNING (unchanged): plan-based fractional points. Count how many
            # running workouts the user has ALREADY logged this current week
            # (excluding this one, streak_bonus rows, and other users), then
            # compute the plan-based per-workout value.
            try:
                plan = await self._sheets.get_plan(user.id)
            except Exception as exc:
                logger.error(
                    "Failed to fetch plan for user %s; using default: %s",
                    user.id,
                    exc,
                )
                plan = None
            if plan is None:
                plan = DEFAULT_PLAN

            try:
                workouts_so_far = await self._sheets.count_user_workouts_in_week(
                    user.id, week_start, week_end
                )
            except Exception as exc:
                logger.error(
                    "Failed to count this-week workouts for user %s; "
                    "assuming 0: %s",
                    user.id,
                    exc,
                )
                workouts_so_far = 0

            points = workout_points(plan, workouts_so_far)
            reply_text = f"✅ Nice run, {who}! +{format_points(points)} points."
        else:
            # BONUS ACTIVITY (walking/cycling/strength): flat points once the
            # per-activity minimum duration is met. These are SEPARATE bonus
            # points — they do NOT touch the plan/streak/overachievement.
            dur = verdict.duration_minutes
            if dur is None:
                # Duration couldn't be read → can't score. No log, no points.
                try:
                    await message.reply_text(
                        "⚠️ Couldn't read the duration — no points awarded."
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to send no-duration reply: %s", exc)
                logger.info(
                    "Bonus activity %r for user %s has no readable duration; "
                    "not logged.",
                    activity,
                    user.id,
                )
                return

            minimum = ACTIVITY_MIN_MINUTES[activity]
            if dur < minimum:
                # Below the minimum → do NOT log, do NOT award; short reply.
                noun = _BELOW_MIN_NOUN[activity]
                try:
                    await message.reply_text(
                        f"⚠️ {noun} is {dur} min — minimum is {minimum} min "
                        f"to earn points."
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.error("Failed to send below-minimum reply: %s", exc)
                logger.info(
                    "Bonus activity %r for user %s is %d min (< %d min "
                    "minimum); not logged.",
                    activity,
                    user.id,
                    dur,
                    minimum,
                )
                return

            points = float(BONUS_ACTIVITY_POINTS)
            reply_text = (
                f"✅ Nice {activity_label(activity)}, {who}! +5 points."
            )

        # 8) Build the row and append.
        display_name = display_name_early

        # Opportunistically keep the username directory fresh so coach commands
        # can resolve @username → id for people who post. Best-effort only: it
        # upserts ONLY identity columns (never the plan/streak) and MUST NOT
        # block or fail the workout logging below.
        try:
            await self._sheets.touch_user(user.id, username, display_name)
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning(
                "touch_user failed for poster %s (non-fatal): %s", user.id, exc
            )

        row = WorkoutLogRow(
            timestamp=WorkoutLogRow.now_timestamp(),
            telegram_user_id=user.id,
            telegram_username=username,
            display_name=display_name,
            workout_date=verdict.workout_date,
            activity_type=verdict.activity_type,
            points=points,
            image_hash=image_hash,
            telegram_file_id=largest.file_id,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )

        # Write to the Sheet FIRST and confirm success before logging.
        # append_workout retries transient failures and returns True only once
        # the row is confirmed written; it raises on final failure.
        try:
            appended = await self._sheets.append_workout(row)
        except Exception as exc:
            # The write ultimately failed (after retries). Logging is silent:
            # log an ERROR (visible in Railway logs) but send NO chat message.
            logger.error("Failed to append workout to Sheet: %s", exc)
            appended = False

        if not appended:
            # Silent failure — observable via the ERROR log above only.
            return

        # 9) Success: INFO log first, then a chat reply confirming the activity.
        logger.info(
            "Logged workout: user=%s date=%s activity=%s points=%s",
            user.id,
            verdict.workout_date,
            activity,
            points,
        )

        # The row is already safely written. Send the pre-computed plain-text
        # confirmation (no parse_mode to avoid Markdown/HTML injection via the
        # name). The exact wording was chosen per-activity above.
        try:
            await message.reply_text(reply_text)
        except TelegramError as exc:
            # The log is already saved; a failed reply must not crash the
            # handler or undo the write. Log and move on.
            logger.error("Failed to send success reply: %s", exc)
        except Exception as exc:  # pragma: no cover - defensive
            logger.error("Unexpected error sending success reply: %s", exc)