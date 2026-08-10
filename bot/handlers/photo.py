"""Photo handler.

Orchestrates the full pipeline for photo messages:
download → hash → dedup → vision → decision → log + reply.

Screenshots from BOTH supported apps — Garmin Connect and WHOOP — flow through
this single pipeline and score identically. WHOOP workout screens often show
only a time-of-day range with no date, so a missing ``workout_date`` falls back
to the submission (message) date instead of being rejected.

A workout dated in the just-finished week is still accepted during the
Monday-morning late-submission grace period (before the 09:00/09:05 leaderboards
post) — see :func:`bot.utils.dates.accepted_workout_window`. Such a row keeps its
REAL ``workout_date`` and is scored against the week that date belongs to.

On a successful, eligible run the row is written to the Google Sheet FIRST; once
the write is confirmed and the INFO log is emitted, the bot replies to the chat
with "✅ Nice run, {name}! +{points} points.". Rejections are never silent: every
path that declines to award points sends a short, friendly explanation via
:func:`_safe_reply` (the only exception is a duplicate re-submission, which stays
silent by design), and all decisions remain observable via logs.
"""

from __future__ import annotations

import logging
from datetime import date
from zoneinfo import ZoneInfo

from telegram import Update
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.models import WorkoutLogRow
from bot.services.leaderboard import LeaderboardService  # noqa: F401 (type hints)
from bot.services.sheets import SheetsService
from bot.services.vision import ClaudeVisionService
from bot.utils.dates import (
    accepted_workout_window,
    current_week_bounds,
    in_range,
    today_in,
    week_bounds_containing,
)
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


async def _safe_reply(message, text: str) -> None:
    """Send a plain-text reply, swallowing/logging any Telegram failure.

    Mirrors the helper of the same name in :mod:`bot.handlers.commands` (kept
    local so the photo pipeline does not import the command module). Used by
    every rejection path so a failed send can never crash the handler or undo a
    confirmed Sheet write.
    """

    try:
        await message.reply_text(text)
    except TelegramError as exc:
        logger.error("Failed to send reply: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error sending reply: %s", exc)


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

    def _submission_date(self, message) -> date:
        """Return the message's calendar date in the configured timezone.

        Used as the workout-date fallback when the screenshot shows no date at
        all (typical for WHOOP workout screens, which only show a time-of-day
        range). Falls back to "today" if the message carries no timestamp.
        """

        tz = ZoneInfo(self._settings.timezone)
        sent_at = getattr(message, "date", None)
        if sent_at is None:
            return today_in(self._settings.timezone)
        return sent_at.astimezone(tz).date()

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
            # Parse/API/validation failure → tell the poster rather than drop
            # the submission silently.
            logger.info(
                "Vision analysis returned no usable verdict for user %s.",
                user.id,
            )
            await _safe_reply(
                message,
                "⚠️ Couldn't read this screenshot — no points awarded. Please "
                "try again with a clear workout summary screenshot.",
            )
            return

        # 5a) Reject summary screens explicitly (not a completed-workout
        # summary): Garmin achievements/badges/personal-records screens AND
        # WHOOP daily overviews (day Strain / Recovery / Sleep / Health
        # Monitor / coach cards). Unlike other non-eligible verdicts, this one
        # gets a clear user-facing reply so the poster knows to send the
        # workout summary instead — and NO points are awarded / row written.
        if verdict.is_achievement:
            logger.info(
                "Summary/achievements screenshot from user %s (source=%s); "
                "not a workout — no points awarded.",
                user.id,
                verdict.source,
            )
            await _safe_reply(
                message,
                "⚠️ This looks like a summary/achievements screen, not a "
                "completed workout. Please send the workout summary "
                "screenshot from Garmin or WHOOP.",
            )
            return

        # 5a-bis) Missing date → fall back to the submission date. WHOOP workout
        # screens usually show only a time-of-day range (e.g. "8:12 PM to
        # 9:11 PM") and no date at all, so rejecting a dateless screenshot
        # would drop valid workouts. The message's own timestamp (converted to
        # the configured timezone) is the workout day in practice, since people
        # post right after training. Garmin screenshots keep whatever date the
        # model read; this only applies when NO date was visible.
        if verdict.workout_date is None:
            fallback = self._submission_date(message)
            logger.info(
                "No date on screenshot from user %s (source=%s); falling back "
                "to the submission date %s.",
                user.id,
                verdict.source,
                fallback,
            )
            verdict = verdict.model_copy(
                update={"workout_date": fallback.isoformat()}
            )

        # 5b) Eligibility.
        if not verdict.is_eligible(self._settings.min_confidence):
            logger.info(
                "Verdict not eligible (supported=%s source=%s type=%s "
                "completed=%s date=%s conf=%.2f); rejecting.",
                verdict.is_garmin,
                verdict.source,
                verdict.activity_type,
                verdict.is_completed,
                verdict.workout_date,
                verdict.confidence,
            )
            await _safe_reply(
                message,
                "⚠️ Couldn't confirm a completed workout in this screenshot — "
                "no points awarded. Please send the workout summary screenshot "
                "from Garmin or WHOOP.",
            )
            return

        # Gate: only awardable activity types proceed. Running uses the
        # plan-based model; walking/cycling/strength are flat bonus activities.
        # Anything else ("other"/unrecognized) earns no points — but the poster
        # is told so instead of being ignored.
        activity = verdict.activity_type
        if activity != "running" and activity not in BONUS_ACTIVITIES:
            logger.info(
                "Activity type %r is not awardable; rejecting.",
                activity,
            )
            await _safe_reply(
                message,
                "⚠️ This activity type doesn't earn points. Points are awarded "
                "for running, walking, cycling and strength workouts.",
            )
            return

        # 6) Date-window: the current Mon–Sun week, EXTENDED backwards over the
        # previous week during the Monday-morning late-submission grace period
        # (before the 09:00/09:05 boards post). See
        # :func:`accepted_workout_window`.
        assert verdict.workout_date is not None  # guaranteed by eligibility
        try:
            wdate = date.fromisoformat(verdict.workout_date)
        except ValueError:
            logger.warning("Invalid workout_date after validation; ignoring.")
            await _safe_reply(
                message,
                "⚠️ Couldn't read the workout date — no points awarded.",
            )
            return

        accepted_start, accepted_end = accepted_workout_window(
            self._settings.timezone,
            self._settings.late_submission_grace_until_hour,
            now=getattr(message, "date", None),
        )
        if not in_range(wdate, accepted_start, accepted_end):
            logger.info(
                "Workout date %s is outside the accepted window (%s–%s); "
                "rejecting.",
                wdate,
                accepted_start,
                accepted_end,
            )
            await _safe_reply(
                message,
                f"⚠️ This workout is dated {wdate.isoformat()}, which is "
                "outside the week we're currently counting. Points can only be "
                "added for the current week.",
            )
            return

        # Score the workout against the week its OWN date belongs to. For a
        # normal submission that is the current week; for one accepted under the
        # grace period it is the PREVIOUS week — which is exactly the week being
        # reported at 09:00/09:05, so the running weekly count (and therefore the
        # plan-based 30/plan rate and the overachievement halving) stays correct.
        week_start, week_end = week_bounds_containing(wdate)
        if (week_start, week_end) != current_week_bounds(self._settings.timezone):
            logger.info(
                "Late submission accepted under the grace period: workout date "
                "%s scored against its own week (%s–%s).",
                wdate,
                week_start,
                week_end,
            )

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
            # running workouts the user has ALREADY logged in the week THIS
            # workout's date belongs to (excluding this one, streak_bonus rows,
            # and other users), then compute the plan-based per-workout value.
            # Using the workout's own week keeps a late submission scored against
            # the week it was actually performed in.
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
                await _safe_reply(
                    message,
                    "⚠️ Couldn't read the duration — no points awarded.",
                )
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
                await _safe_reply(
                    message,
                    f"⚠️ {noun} is {dur} min — minimum is {minimum} min "
                    f"to earn points.",
                )
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
            # The write ultimately failed (after retries). Log an ERROR (visible
            # in Railway logs) and tell the poster so the submission is never
            # dropped without a word.
            logger.error("Failed to append workout to Sheet: %s", exc)
            appended = False

        if not appended:
            await _safe_reply(
                message,
                "⚠️ Couldn't save this workout just now — please send the "
                "screenshot again in a few minutes.",
            )
            return

        # 9) Success: INFO log first, then a chat reply confirming the activity.
        logger.info(
            "Logged workout: user=%s source=%s date=%s activity=%s points=%s",
            user.id,
            verdict.source,
            verdict.workout_date,
            activity,
            points,
        )

        # The row is already safely written. Send the pre-computed plain-text
        # confirmation (no parse_mode to avoid Markdown/HTML injection via the
        # name). The exact wording was chosen per-activity above. A failed reply
        # must not crash the handler or undo the write, hence _safe_reply.
        await _safe_reply(message, reply_text)