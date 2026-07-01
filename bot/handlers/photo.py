"""Photo handler.

Orchestrates the full pipeline for photo messages:
download → hash → dedup → vision → decision → log/reply.

All non-eligible / failure paths are handled silently (no reply, no log) to
avoid spamming the group, per the blueprint.
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
from bot.utils.points import resolve_points

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

        points = resolve_points(verdict.activity_type, self._activity_points)
        if points == 0:
            logger.info(
                "Activity type %r has no points mapping; ignoring.",
                verdict.activity_type,
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

        # 8) Build the row and append.
        username = (user.username or "").strip()
        display_name = " ".join(
            part for part in [user.first_name, user.last_name] if part
        ).strip()

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

        try:
            await self._sheets.append_workout(row)
        except Exception as exc:
            # Do NOT reply if the write failed — avoid claiming an unrecorded point.
            logger.error("Failed to append workout to Sheet: %s", exc)
            return

        # 9) Confirmation reply.
        who = display_name or (f"@{username}" if username else "runner")
        reply_text = (
            f"✅ Nice run, {who}! +{points} points logged for {verdict.workout_date}."
        )
        try:
            await message.reply_text(reply_text)
        except TelegramError as exc:
            logger.error("Failed to send confirmation reply: %s", exc)