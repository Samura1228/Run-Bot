"""Google Sheets service.

All Google Sheets I/O: dedup lookup, append row, and reading rows for
aggregation. Credentials are built from a service-account dict (no file on
disk). All blocking gspread calls are wrapped in ``asyncio.to_thread`` so they
never block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any, Optional, Sequence

import gspread
from google.oauth2.service_account import Credentials

from bot.models import WorkoutLogRow
from bot.utils.dates import in_range
from bot.utils.points import DEFAULT_PLAN

if TYPE_CHECKING:  # pragma: no cover - typing only
    from bot.config import Settings

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WORKSHEET_NAME = "Log"
PLANS_WORKSHEET_NAME = "Plans"
TEAMS_WORKSHEET_NAME = "Teams"
MEMBERS_WORKSHEET_NAME = "Members"
COMMANDS_WORKSHEET_NAME = "Commands"

# Append retry policy: up to 3 attempts with exponential backoff (1s, 2s, 4s).
_APPEND_MAX_ATTEMPTS = 3
_APPEND_BACKOFF_BASE_SECONDS = 1.0

# HTTP statuses considered transient (worth retrying).
_TRANSIENT_STATUSES = frozenset({429, 500, 502, 503, 504})


def _api_error_status(exc: gspread.exceptions.APIError) -> Optional[int]:
    """Best-effort extraction of the HTTP status code from a gspread APIError."""

    return getattr(getattr(exc, "response", None), "status_code", None)


def _is_transient_error(exc: BaseException) -> bool:
    """Classify an exception as a transient (retryable) failure.

    Transient: network hiccups, timeouts, and 5xx / 429 API errors (e.g. the
    ``502 Bad Gateway`` seen in production). Permanent client errors such as
    401/403 (permission) are NOT transient and must not be retried.
    """

    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return True
    if isinstance(exc, gspread.exceptions.APIError):
        status = _api_error_status(exc)
        return status in _TRANSIENT_STATUSES
    return False

HEADER_ROW = [
    "timestamp",
    "telegram_user_id",
    "telegram_username",
    "display_name",
    "workout_date",
    "activity_type",
    "points",
    "image_hash",
    "telegram_file_id",
    "chat_id",
    "message_id",
]

# Column indices (0-based) for reads.
_COL_USER_ID = 1
_COL_USERNAME = 2
_COL_DISPLAY_NAME = 3
_COL_WORKOUT_DATE = 4
_COL_ACTIVITY_TYPE = 5
_COL_POINTS = 6
_COL_IMAGE_HASH = 7

# Activity type of the rows written by the old weekly streak-bonus rollover.
# The feature is gone; these rows are kept in the sheet as history but are
# skipped by every aggregation read, so they award nothing.
LEGACY_STREAK_BONUS_ACTIVITY = "streak_bonus"

# --- Plans worksheet ------------------------------------------------------ #
# NOTE: the ``streak`` column is a LEFTOVER of the removed streak-bonus
# feature. It is kept in the header (and preserved on every upsert) purely so
# the existing sheet's column layout stays valid — nothing reads it to award
# anything any more.
PLANS_HEADER_ROW = [
    "telegram_user_id",
    "telegram_username",
    "plan",
    "streak",
    "updated_at",
]

# Column indices (0-based) for the Plans worksheet.
_PLAN_COL_USER_ID = 0
_PLAN_COL_USERNAME = 1
_PLAN_COL_PLAN = 2
_PLAN_COL_STREAK = 3
_PLAN_COL_UPDATED_AT = 4

# --- Members worksheet ---------------------------------------------------- #
# A hand-maintained directory mapping the NAME a coach writes in /team to a
# Telegram account. The bot creates the sheet with this header and never writes
# to it — it is filled in by hand, which is the point: the coach can add people
# and fix spellings without a deploy.
MEMBERS_HEADER_ROW = [
    "name",
    "username",
    "telegram_id",
]

# Column indices (0-based) for the Members worksheet.
_MEMBER_COL_NAME = 0
_MEMBER_COL_USERNAME = 1
_MEMBER_COL_USER_ID = 2

# --- Commands worksheet --------------------------------------------------- #
# A generated, human-readable reference of every command the bot answers, so a
# coach can see what exists (including the coach-only ones) without reading the
# README. The bot REWRITES this tab on every start, which is the whole point —
# it can never drift out of date — so hand edits here are lost. Keep the text
# in sync with the handlers registered in ``bot.main`` AND with the permission
# each handler actually enforces — a reference that lies about who may run a
# command is worse than none.
COMMANDS_HEADER_ROW = [
    "command",
    "who can use it",
    "what it does",
]

COMMANDS_REFERENCE: tuple[tuple[str, str, str], ...] = (
    (
        "(just post a screenshot)",
        "everyone",
        "The main interaction — no command needed. Post a Garmin, Strava or "
        "WHOOP workout screenshot in the group and the bot scores it. Running "
        "earns plan-based points; walking (40+ min), cycling (60+ min) and "
        "strength/stretching (15+ min) earn a flat 5. Ordinary photos, other "
        "apps and unscored sports are ignored in silence.",
    ),
    (
        "@<the bot> <question>",
        "everyone, in the club group only",
        "Ask the bot a question by MENTIONING it, e.g. '@bot how long do I "
        "have to cycle for points?'. Replying to one of its messages does not "
        "count. It answers from "
        "the club's rules and your own data (your plan, your points this week, "
        "your team). It will not answer health or injury questions, and it "
        "never invents a rule — if it does not know, it says so. Limits: one "
        "question per person every 10 seconds, 15 per hour in the chat. It "
        "It also has transcripts of the coach's voice messages and will "
        "point you at the right recording and minute when the answer is "
        "there. It "
        "stays silent in any other group; in a private chat it answers only "
        "the bot admin, which is how the feature gets tested before the club "
        "sees it.",
    ),
    (
        "/team <start> - <end>",
        "coaches + TEAM_ADMIN_IDS",
        "Set up the team competition for a date range, e.g. "
        "'/team 28/09/26 - 04/10/26' followed by a team name per line and its "
        "members underneath. Names come from the Members tab. Nothing is saved "
        "unless every name resolves and nobody is on two teams. Sending a new "
        "line-up replaces the running round. Alias: /teams.",
    ),
    (
        "/team status",
        "coaches + TEAM_ADMIN_IDS",
        "Post the current team standings into this chat right away, without "
        "waiting for the scheduled one. Aliases: /team now, /team score, or "
        "just /team on its own.",
    ),
    (
        "/team stop",
        "coaches + TEAM_ADMIN_IDS",
        "Cancel the running team round immediately. No final board is posted "
        "and teams stop being tracked. Aliases: /team cancel, /team end.",
    ),
    (
        "/setplan @user N",
        "coaches only",
        "Set a member's weekly plan (N = 2-6 workouts/week), which decides "
        "their points per run: 30 / N. Target them by @username or by replying "
        "to their message with '/setplan N'. Aliases: /setmyplan, "
        "/setuserplan, /setplans.",
    ),
    (
        "/myplan",
        "everyone (own plan)",
        "Show your own weekly plan. A coach can add @username, or reply to "
        "someone's message, to see theirs. Alias: /myplans.",
    ),
    (
        "/whoami",
        "everyone",
        "Show your Telegram ID and name. Reply to someone else's message with "
        "it to get THEIR ID — this is how you fill in the telegram_id column "
        "of the Members tab and the COACH_IDS / TEAM_ADMIN_IDS variables.",
    ),
    (
        "/chatid",
        "bot admin only (ADMIN_IDS)",
        "Show this chat's ID, type and title — the value for the "
        "TARGET_CHAT_ID variable, which decides where the boards are posted.",
    ),
    (
        "/status",
        "bot admin only (ADMIN_IDS)",
        "Health check: Telegram, the Claude vision API and Google Sheets, each "
        "reported OK or with a short reason.",
    ),
    (
        "/testsheet",
        "bot admin only (ADMIN_IDS)",
        "Test the Google Sheets connection and Editor access on its own, with "
        "a short hint when it fails (sharing, credentials or sheet ID).",
    ),
)

# Boards the scheduler posts on its own, listed under the commands so the tab
# answers "what does this bot do" in full, not just "what can I type".
SCHEDULE_REFERENCE: tuple[tuple[str, str, str], ...] = (
    (
        "(automatic) team standings",
        "—",
        "Every 3 days from the round's start date, at 09:05, while a team "
        "round is running.",
    ),
    (
        "(automatic) team final board",
        "—",
        "At 09:05 the morning AFTER the round's end date, so a round ending "
        "Sunday is reported on Monday once that Sunday has fully counted.",
    ),
    (
        "(automatic) weekly leaderboard",
        "—",
        "Every Monday 09:05: individual totals for the previous Mon-Sun week, "
        "posted right after the team board.",
    ),
    (
        "(automatic) monthly leaderboard",
        "—",
        "On the 1st of each month at 09:00: individual totals for the "
        "previous calendar month.",
    ),
)

# --- Teams worksheet ------------------------------------------------------ #
# One row per coach-created TEAM ROUND. A round covers one Mon-Sun week and is
# only counted while active; the board posts with the Monday leaderboards and
# the round is then marked ``posted`` so nothing is reported twice. Rounds are
# never deleted, so the history stays auditable.
TEAMS_HEADER_ROW = [
    "round_id",
    "start_date",
    "end_date",
    "teams",
    "status",
    "created_by",
    "created_at",
]

# Column indices (0-based) for the Teams worksheet.
_TEAMS_COL_ROUND_ID = 0
_TEAMS_COL_START_DATE = 1
_TEAMS_COL_END_DATE = 2
_TEAMS_COL_TEAMS = 3
_TEAMS_COL_STATUS = 4
_TEAMS_COL_CREATED_BY = 5
_TEAMS_COL_CREATED_AT = 6

# Team round lifecycle.
TEAMS_STATUS_ACTIVE = "active"        # counting; the board has not posted yet
TEAMS_STATUS_POSTED = "posted"        # finished and the final board was posted
TEAMS_STATUS_CANCELLED = "cancelled"  # stopped early by a coach


class SheetsService:
    """Encapsulates all Google Sheets access for the bot."""

    def __init__(
        self,
        service_account_info: dict[str, Any],
        sheet_id: str,
        season_start_date: Optional[date] = None,
        excluded_user_ids: Optional[set[int]] = None,
    ) -> None:
        self._service_account_info = service_account_info
        self._sheet_id = sheet_id
        # Season cutoff: submissions with a ``workout_date`` BEFORE this date are
        # ignored by ALL points/leaderboard aggregation reads below, so a new
        # season effectively starts everyone at zero WITHOUT deleting rows,
        # registrations, or coach-assigned plans. ``None`` disables the cutoff
        # (counts every row) — used only where a season is intentionally absent.
        self._season_start_date = season_start_date
        # Non-scoring members (the coaches): their rows are ignored by the SAME
        # aggregation reads the season cutoff guards, so they never appear on a
        # leaderboard and never count toward a pair — WITHOUT deleting
        # anything. Kept as strings because the sheet cells
        # are strings and every read compares ``row[_COL_USER_ID]`` directly.
        self._excluded_user_ids = {str(uid) for uid in (excluded_user_ids or ())}
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None
        self._plans_worksheet: Optional[gspread.Worksheet] = None
        self._teams_worksheet: Optional[gspread.Worksheet] = None
        self._members_worksheet: Optional[gspread.Worksheet] = None

    # ------------------------------------------------------------------ #
    # Initialization
    # ------------------------------------------------------------------ #
    def _init_sync(self) -> None:
        """Blocking initialization: authorize, open sheet, ensure worksheet."""

        credentials = Credentials.from_service_account_info(
            self._service_account_info, scopes=_SCOPES
        )
        self._client = gspread.authorize(credentials)
        spreadsheet = self._client.open_by_key(self._sheet_id)

        try:
            worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title=WORKSHEET_NAME, rows=1000, cols=len(HEADER_ROW)
            )
            worksheet.update(values=[HEADER_ROW], range_name="A1")
            logger.info("Created worksheet %r with header row.", WORKSHEET_NAME)
        else:
            # Ensure the header row exists / is correct.
            existing = worksheet.row_values(1)
            if existing != HEADER_ROW:
                worksheet.update(values=[HEADER_ROW], range_name="A1")
                logger.info("Reset header row on worksheet %r.", WORKSHEET_NAME)

        self._worksheet = worksheet

        # Ensure the Plans worksheet exists / has the correct header row,
        # mirroring the Log worksheet auto-create/repair behaviour above.
        try:
            plans_ws = spreadsheet.worksheet(PLANS_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            plans_ws = spreadsheet.add_worksheet(
                title=PLANS_WORKSHEET_NAME, rows=1000, cols=len(PLANS_HEADER_ROW)
            )
            plans_ws.update(values=[PLANS_HEADER_ROW], range_name="A1")
            logger.info(
                "Created worksheet %r with header row.", PLANS_WORKSHEET_NAME
            )
        else:
            existing_plans = plans_ws.row_values(1)
            if existing_plans != PLANS_HEADER_ROW:
                plans_ws.update(values=[PLANS_HEADER_ROW], range_name="A1")
                logger.info(
                    "Reset header row on worksheet %r.", PLANS_WORKSHEET_NAME
                )

        self._plans_worksheet = plans_ws

        # Ensure the Teams worksheet exists / has the correct header row,
        # using the same auto-create/repair behaviour as the Log and Plans tabs.
        try:
            teams_ws = spreadsheet.worksheet(TEAMS_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            teams_ws = spreadsheet.add_worksheet(
                title=TEAMS_WORKSHEET_NAME, rows=1000, cols=len(TEAMS_HEADER_ROW)
            )
            teams_ws.update(values=[TEAMS_HEADER_ROW], range_name="A1")
            logger.info(
                "Created worksheet %r with header row.", TEAMS_WORKSHEET_NAME
            )
        else:
            existing_teams = teams_ws.row_values(1)
            if existing_teams != TEAMS_HEADER_ROW:
                teams_ws.update(values=[TEAMS_HEADER_ROW], range_name="A1")
                logger.info(
                    "Reset header row on worksheet %r.", TEAMS_WORKSHEET_NAME
                )

        self._teams_worksheet = teams_ws

        # Ensure the Members directory exists. Created empty (header only) and
        # NEVER written to by the bot — a coach fills it in by hand so /team can
        # resolve the names they actually type. The header is repaired like the
        # others, but existing rows are left completely alone.
        try:
            members_ws = spreadsheet.worksheet(MEMBERS_WORKSHEET_NAME)
        except gspread.WorksheetNotFound:
            members_ws = spreadsheet.add_worksheet(
                title=MEMBERS_WORKSHEET_NAME,
                rows=1000,
                cols=len(MEMBERS_HEADER_ROW),
            )
            members_ws.update(values=[MEMBERS_HEADER_ROW], range_name="A1")
            logger.info(
                "Created worksheet %r with header row (fill it in by hand).",
                MEMBERS_WORKSHEET_NAME,
            )
        else:
            existing_members = members_ws.row_values(1)
            if existing_members != MEMBERS_HEADER_ROW:
                members_ws.update(values=[MEMBERS_HEADER_ROW], range_name="A1")
                logger.info(
                    "Reset header row on worksheet %r.", MEMBERS_WORKSHEET_NAME
                )

        self._members_worksheet = members_ws

        # Ensure the Commands reference exists and is CURRENT. Unlike every
        # other tab this one is fully regenerated on each start: it documents
        # the bot to whoever opens the sheet, and a stale reference is worse
        # than none. ``clear()`` first so a row removed from the code does not
        # linger. Best-effort — a failure here must never stop the bot.
        try:
            try:
                commands_ws = spreadsheet.worksheet(COMMANDS_WORKSHEET_NAME)
            except gspread.WorksheetNotFound:
                commands_ws = spreadsheet.add_worksheet(
                    title=COMMANDS_WORKSHEET_NAME,
                    rows=max(50, len(COMMANDS_REFERENCE) + len(SCHEDULE_REFERENCE) + 10),
                    cols=len(COMMANDS_HEADER_ROW),
                )
                logger.info(
                    "Created worksheet %r.", COMMANDS_WORKSHEET_NAME
                )
            values = [
                list(COMMANDS_HEADER_ROW),
                *[list(row) for row in COMMANDS_REFERENCE],
                ["", "", ""],
                *[list(row) for row in SCHEDULE_REFERENCE],
            ]
            commands_ws.clear()
            commands_ws.update(values=values, range_name="A1")
            logger.info(
                "Refreshed the %r reference (%d rows).",
                COMMANDS_WORKSHEET_NAME,
                len(values) - 1,
            )
        except Exception as exc:  # noqa: BLE001 - documentation is not critical
            logger.warning(
                "Could not refresh the %r worksheet (non-fatal): %s",
                COMMANDS_WORKSHEET_NAME,
                exc,
            )

    async def initialize(self) -> None:
        """Authorize and prepare the worksheet (creating it if missing)."""

        await asyncio.to_thread(self._init_sync)
        logger.info("SheetsService initialized for sheet %s.", self._sheet_id)

    def _require_worksheet(self) -> gspread.Worksheet:
        if self._worksheet is None:
            raise RuntimeError("SheetsService not initialized; call initialize().")
        return self._worksheet

    def _require_teams_worksheet(self) -> gspread.Worksheet:
        if self._teams_worksheet is None:
            raise RuntimeError("SheetsService not initialized; call initialize().")
        return self._teams_worksheet

    def _require_members_worksheet(self) -> gspread.Worksheet:
        if self._members_worksheet is None:
            raise RuntimeError("SheetsService not initialized; call initialize().")
        return self._members_worksheet

    def _require_plans_worksheet(self) -> gspread.Worksheet:
        if self._plans_worksheet is None:
            raise RuntimeError("SheetsService not initialized; call initialize().")
        return self._plans_worksheet

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    def _read_all_records_sync(self) -> list[list[str]]:
        """Return all rows (including header) as lists of strings."""

        worksheet = self._require_worksheet()
        return worksheet.get_all_values()

    def _before_season(self, workout_date: date) -> bool:
        """Return True if ``workout_date`` falls BEFORE the season start date.

        When a season start date is configured, submissions dated before it are
        excluded from ALL points/leaderboard aggregation (so the season restarts
        everyone at zero without deleting rows or plans). When no season start
        date is configured (``None``), nothing is excluded.
        """

        if self._season_start_date is None:
            return False
        return workout_date < self._season_start_date

    def _is_excluded(self, user_id_cell: str) -> bool:
        """Return True if the row belongs to a non-scoring member (a coach).

        Companion to :meth:`_before_season`: both are applied by every
        points/leaderboard aggregation read so the rule lives in one place.
        """

        return user_id_cell in self._excluded_user_ids

    def is_excluded_user(self, user_id: int) -> bool:
        """Public form of :meth:`_is_excluded` for callers holding an int id."""

        return str(user_id) in self._excluded_user_ids

    async def is_duplicate(self, user_id: int, image_hash: str) -> bool:
        """Return True if a row already exists for (user_id, image_hash).

        On any read error, returns ``False`` (fail-open) so a transient Sheets
        outage does not silently block a legitimate submission; the caller may
        still perform a second race-safe check before appending.
        """

        try:
            rows = await asyncio.to_thread(self._read_all_records_sync)
        except Exception as exc:
            logger.error("Sheets read failed during dedup check: %s", exc)
            return False

        user_id_str = str(user_id)
        for row in rows[1:]:  # skip header
            if len(row) <= _COL_IMAGE_HASH:
                continue
            if row[_COL_USER_ID] == user_id_str and row[_COL_IMAGE_HASH] == image_hash:
                return True
        return False

    async def read_rows_in_range(
        self, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        """Return parsed rows whose ``workout_date`` is within the range.

        Each returned dict has: ``telegram_user_id`` (int), ``telegram_username``
        (str), ``display_name`` (str), ``workout_date`` (date),
        ``activity_type`` (str) and ``points`` (float). Points are parsed as floats so fractional per-workout values
        (e.g. ``7.5``) aggregate correctly. Rows that fail parsing are skipped.
        """

        rows = await asyncio.to_thread(self._read_all_records_sync)
        parsed: list[dict[str, Any]] = []

        for row in rows[1:]:  # skip header
            if len(row) <= _COL_POINTS:
                continue
            try:
                wdate = date.fromisoformat(row[_COL_WORKOUT_DATE])
            except (ValueError, IndexError):
                continue
            # Season cutoff: ignore pre-season submissions entirely so points
            # and the leaderboard reflect only the current season.
            if self._before_season(wdate):
                continue
            # Coaches never score: drop their rows from every board and pair.
            if self._is_excluded(row[_COL_USER_ID]):
                continue
            # Streak bonuses were removed from the bot. Legacy ``streak_bonus``
            # rows stay in the sheet for the record but no longer count, so the
            # boards show only points actually earned by training.
            if row[_COL_ACTIVITY_TYPE] == LEGACY_STREAK_BONUS_ACTIVITY:
                continue
            if not in_range(wdate, start_date, end_date):
                continue
            try:
                user_id = int(row[_COL_USER_ID])
                points = float(row[_COL_POINTS])
            except (ValueError, IndexError):
                continue
            parsed.append(
                {
                    "telegram_user_id": user_id,
                    "telegram_username": row[_COL_USERNAME],
                    "display_name": row[_COL_DISPLAY_NAME],
                    "workout_date": wdate,
                    "activity_type": row[_COL_ACTIVITY_TYPE],
                    "points": points,
                }
            )
        return parsed

    async def count_user_workouts_in_week(
        self, user_id: int, week_start: date, week_end: date
    ) -> int:
        """Count a user's running workouts logged within a Mon–Sun week.

        Only rows with ``activity_type == "running"`` are counted; special rows
        such as legacy ``streak_bonus`` rows and rows for other users are
        excluded. Used by the per-workout points calculation.
        """

        if self.is_excluded_user(user_id):
            return 0
        rows = await asyncio.to_thread(self._read_all_records_sync)
        user_id_str = str(user_id)
        count = 0

        for row in rows[1:]:  # skip header
            if len(row) <= _COL_POINTS:
                continue
            if row[_COL_USER_ID] != user_id_str:
                continue
            if row[_COL_ACTIVITY_TYPE] != "running":
                continue
            try:
                wdate = date.fromisoformat(row[_COL_WORKOUT_DATE])
            except (ValueError, IndexError):
                continue
            # Season cutoff: pre-season workouts do not count toward the
            # per-workout points calculation.
            if self._before_season(wdate):
                continue
            if in_range(wdate, week_start, week_end):
                count += 1
        return count

    async def sum_user_points_in_range(
        self, user_id: int, start_date: date, end_date: date
    ) -> float:
        """Return the SUM of one user's points over an inclusive date range.

        Unlike :meth:`count_user_workouts_in_week` (running rows only, used for
        the plan-based per-workout rate) this sums EVERY row the user has in the
        range — running and the flat bonus activities — so the value matches
        what the weekly leaderboard shows for that user. Used for
        the "total week" figure appended to the success reply after a submission
        has been written.

        Rows that fail to parse are skipped, and pre-season rows are excluded by
        the same season cutoff the leaderboard uses.
        """

        if self.is_excluded_user(user_id):
            return 0.0
        rows = await asyncio.to_thread(self._read_all_records_sync)
        user_id_str = str(user_id)
        total = 0.0

        for row in rows[1:]:  # skip header
            if len(row) <= _COL_POINTS:
                continue
            if row[_COL_USER_ID] != user_id_str:
                continue
            try:
                wdate = date.fromisoformat(row[_COL_WORKOUT_DATE])
            except (ValueError, IndexError):
                continue
            if self._before_season(wdate):
                continue
            # Legacy streak bonuses no longer count (see read_rows_in_range).
            if row[_COL_ACTIVITY_TYPE] == LEGACY_STREAK_BONUS_ACTIVITY:
                continue
            if not in_range(wdate, start_date, end_date):
                continue
            try:
                total += float(row[_COL_POINTS])
            except (ValueError, IndexError):
                continue
        return round(total, 2)

    # ------------------------------------------------------------------ #
    # Plans worksheet reads/writes
    # ------------------------------------------------------------------ #
    def _read_all_plans_sync(self) -> list[list[str]]:
        """Return all Plans rows (including header) as lists of strings."""

        worksheet = self._require_plans_worksheet()
        return worksheet.get_all_values()

    @staticmethod
    def _parse_plan_cell(value: str) -> int:
        """Parse a ``plan`` cell, returning :data:`DEFAULT_PLAN` if blank/invalid."""

        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return DEFAULT_PLAN

    @staticmethod
    def _parse_streak_cell(value: str) -> int:
        """Parse a ``streak`` cell, returning 0 if blank/invalid."""

        try:
            return int(str(value).strip())
        except (ValueError, TypeError):
            return 0

    def _find_plan_row_index_sync(self, user_id: int) -> Optional[int]:
        """Return the 1-based sheet row index for a user, or ``None`` if absent.

        Reads all Plans rows and scans for a matching id. The returned index is
        suitable for ``update`` range names (header is row 1, data starts at 2).
        """

        worksheet = self._require_plans_worksheet()
        rows = worksheet.get_all_values()
        user_id_str = str(user_id)
        for offset, row in enumerate(rows[1:], start=2):  # data rows are 1-based+header
            if len(row) <= _PLAN_COL_USER_ID:
                continue
            if row[_PLAN_COL_USER_ID] == user_id_str:
                return offset
        return None

    async def get_plan_record(
        self, user_id: int
    ) -> Optional[dict[str, int]]:
        """Return ``{"plan": int, "streak": int}`` for a user, or ``None``.

        A missing/blank ``plan`` cell yields :data:`DEFAULT_PLAN`; a missing
        ``streak`` yields 0.
        """

        rows = await asyncio.to_thread(self._read_all_plans_sync)
        user_id_str = str(user_id)
        for row in rows[1:]:  # skip header
            if len(row) <= _PLAN_COL_USER_ID:
                continue
            if row[_PLAN_COL_USER_ID] != user_id_str:
                continue
            plan_cell = (
                row[_PLAN_COL_PLAN] if len(row) > _PLAN_COL_PLAN else ""
            )
            streak_cell = (
                row[_PLAN_COL_STREAK] if len(row) > _PLAN_COL_STREAK else ""
            )
            return {
                "plan": self._parse_plan_cell(plan_cell),
                "streak": self._parse_streak_cell(streak_cell),
            }
        return None

    async def get_plan(self, user_id: int) -> int:
        """Return the user's plan, or :data:`DEFAULT_PLAN` if no row exists."""

        record = await self.get_plan_record(user_id)
        if record is None:
            return DEFAULT_PLAN
        return record["plan"]

    async def list_plans(self) -> list[dict[str, Any]]:
        """Return all plan rows as dicts.

        Each dict has: ``user_id`` (int), ``username`` (str), ``plan`` (int),
        ``streak`` (int). Rows with an unparseable id are skipped.
        """

        rows = await asyncio.to_thread(self._read_all_plans_sync)
        parsed: list[dict[str, Any]] = []
        for row in rows[1:]:  # skip header
            if len(row) <= _PLAN_COL_USER_ID:
                continue
            raw_id = row[_PLAN_COL_USER_ID].strip()
            if not raw_id:
                continue
            try:
                user_id = int(raw_id)
            except ValueError:
                continue
            username = (
                row[_PLAN_COL_USERNAME] if len(row) > _PLAN_COL_USERNAME else ""
            )
            plan_cell = row[_PLAN_COL_PLAN] if len(row) > _PLAN_COL_PLAN else ""
            streak_cell = (
                row[_PLAN_COL_STREAK] if len(row) > _PLAN_COL_STREAK else ""
            )
            parsed.append(
                {
                    "user_id": user_id,
                    "username": username,
                    "plan": self._parse_plan_cell(plan_cell),
                    "streak": self._parse_streak_cell(streak_cell),
                }
            )
        return parsed

    async def find_user_id_by_username(self, username: str) -> Optional[int]:
        """Return the most recent user id matching ``username`` in ``Plans``.

        The lookup is case-insensitive and ignores a single leading ``@`` on the
        query. The ``Plans`` worksheet doubles as a username directory: rows are
        scanned newest-last (later rows override earlier ones, so the most recent
        matching id wins). Returns ``None`` if no match is found or the query is
        blank. All blocking gspread I/O runs in :func:`asyncio.to_thread`.
        """

        query = (username or "").strip().lstrip("@").lower()
        if query == "":
            return None

        rows = await asyncio.to_thread(self._read_all_plans_sync)
        found: Optional[int] = None
        for row in rows[1:]:  # skip header
            if len(row) <= _PLAN_COL_USERNAME:
                continue
            row_username = row[_PLAN_COL_USERNAME].strip().lstrip("@").lower()
            if row_username == "" or row_username != query:
                continue
            raw_id = row[_PLAN_COL_USER_ID].strip()
            if not raw_id:
                continue
            try:
                found = int(raw_id)
            except ValueError:
                continue
            # Do not break: keep scanning so the LAST (most recent) match wins.
        return found

    def _touch_user_sync(self, user_id: int, username: str) -> None:
        """Blocking upsert of ONLY the identity columns for a user.

        Creates a row with ``DEFAULT_PLAN`` / streak 0 if none exists; otherwise
        updates only the username (and ``updated_at``), preserving the existing
        plan and streak. Used to keep the username directory fresh.
        """

        worksheet = self._require_plans_worksheet()
        rows = worksheet.get_all_values()
        user_id_str = str(user_id)
        index: Optional[int] = None
        existing_plan = DEFAULT_PLAN
        existing_streak = 0
        existing_username = ""
        for offset, row in enumerate(rows[1:], start=2):  # header is row 1
            if len(row) <= _PLAN_COL_USER_ID:
                continue
            if row[_PLAN_COL_USER_ID] == user_id_str:
                index = offset
                existing_plan = self._parse_plan_cell(
                    row[_PLAN_COL_PLAN] if len(row) > _PLAN_COL_PLAN else ""
                )
                existing_streak = self._parse_streak_cell(
                    row[_PLAN_COL_STREAK] if len(row) > _PLAN_COL_STREAK else ""
                )
                existing_username = (
                    row[_PLAN_COL_USERNAME]
                    if len(row) > _PLAN_COL_USERNAME
                    else ""
                )
                break

        if index is None:
            # No row yet: create one with defaults so the directory learns the id.
            row_values = [
                user_id_str,
                username,
                str(DEFAULT_PLAN),
                str(0),
                self._now_iso(),
            ]
            worksheet.append_row(row_values, value_input_option="RAW")
            return

        # Row exists: only touch identity columns if the username actually
        # changed, preserving plan/streak. Avoids a needless write when nothing
        # changed (keeps the photo path cheap).
        if username == existing_username:
            return
        row_values = [
            user_id_str,
            username,
            str(existing_plan),
            str(existing_streak),
            self._now_iso(),
        ]
        worksheet.update(
            values=[row_values],
            range_name=f"A{index}:E{index}",
            value_input_option="RAW",
        )

    async def touch_user(
        self, user_id: int, username: str, display_name: str = ""
    ) -> None:
        """Upsert ONLY a user's identity (username), keeping the directory fresh.

        Creates a ``Plans`` row with :data:`DEFAULT_PLAN` / streak 0 if the user
        has none, WITHOUT changing an existing plan/streak. Only writes when the
        username changed or the row is absent, so it stays cheap on the photo
        path. ``display_name`` is accepted for API symmetry but not stored (the
        ``Plans`` sheet has no display-name column). Blocking gspread calls run
        in :func:`asyncio.to_thread` with the shared transient-retry policy.
        """

        await self._retry_blocking(
            self._touch_user_sync,
            user_id,
            (username or "").strip(),
            label=f"touch_user user={user_id}",
        )

    @staticmethod
    def _now_iso() -> str:
        """Return the current UTC time as an ISO 8601 string (e.g. ``...Z``)."""

        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    def _upsert_plan_sync(
        self, user_id: int, username: str, plan: int, streak: int
    ) -> None:
        """Blocking upsert of a Plans row (update if present, else append)."""

        worksheet = self._require_plans_worksheet()
        row_values = [
            str(user_id),
            username,
            str(plan),
            str(streak),
            self._now_iso(),
        ]
        index = self._find_plan_row_index_sync(user_id)
        if index is None:
            worksheet.append_row(row_values, value_input_option="RAW")
        else:
            worksheet.update(
                values=[row_values],
                range_name=f"A{index}:E{index}",
                value_input_option="RAW",
            )

    async def set_plan(self, user_id: int, username: str, plan: int) -> None:
        """Upsert a user's plan, preserving their existing streak (default 0).

        ``plan`` is assumed already clamped by the caller. All blocking gspread
        calls run in :func:`asyncio.to_thread`; transient failures are retried
        with the same backoff policy as :meth:`append_workout`.
        """

        existing = await self.get_plan_record(user_id)
        streak = existing["streak"] if existing is not None else 0
        await self._retry_blocking(
            self._upsert_plan_sync,
            user_id,
            username,
            plan,
            streak,
            label=f"set_plan user={user_id}",
        )

    # ------------------------------------------------------------------ #
    # Members directory (hand-maintained name -> Telegram account)
    # ------------------------------------------------------------------ #
    def _read_all_members_sync(self) -> list[list[str]]:
        """Return all Members rows (including header) as lists of strings."""

        return self._require_members_worksheet().get_all_values()

    @staticmethod
    def normalize_member_name(name: str) -> str:
        """Return the matching key for a member name.

        Case-insensitive and whitespace-tolerant, so "Alexey  B", "alexey b"
        and " Alexey B " all resolve to the same person. Nothing else is
        stripped: "Alexey B" and "Alexey V" must stay distinct.
        """

        return " ".join((name or "").strip().lower().split())

    async def list_members(self) -> list[dict[str, Any]]:
        """Return the Members directory as ``{name, username, user_id}`` dicts.

        Rows without a usable ``telegram_id`` are skipped with a warning: the
        sheet is hand-edited, and a half-filled row must degrade to "this name
        is unknown" (which /team reports) rather than break the command.
        """

        rows = await asyncio.to_thread(self._read_all_members_sync)
        members: list[dict[str, Any]] = []
        for row in rows[1:]:  # skip header
            if len(row) <= _MEMBER_COL_NAME:
                continue
            name = row[_MEMBER_COL_NAME].strip()
            if not name:
                continue
            raw_id = (
                row[_MEMBER_COL_USER_ID].strip()
                if len(row) > _MEMBER_COL_USER_ID
                else ""
            )
            try:
                user_id = int(raw_id)
            except ValueError:
                logger.warning(
                    "Members: row %r has no usable telegram_id (%r); skipping.",
                    name,
                    raw_id,
                )
                continue
            username = (
                row[_MEMBER_COL_USERNAME].strip().lstrip("@")
                if len(row) > _MEMBER_COL_USERNAME
                else ""
            )
            members.append(
                {"name": name, "username": username, "user_id": user_id}
            )
        return members

    async def member_directory(self) -> dict[str, dict[str, Any]]:
        """Return the Members directory keyed by :meth:`normalize_member_name`.

        Also keys each member by their ``@username`` (without the ``@``) when
        present, so a coach may write either the name or the username. A later
        duplicate key is ignored and logged rather than silently overwriting.
        """

        directory: dict[str, dict[str, Any]] = {}
        for member in await self.list_members():
            keys = [self.normalize_member_name(member["name"])]
            if member["username"]:
                keys.append(self.normalize_member_name(member["username"]))
            for key in keys:
                if key in directory and directory[key]["user_id"] != member["user_id"]:
                    logger.warning(
                        "Members: duplicate entry %r; keeping the first.", key
                    )
                    continue
                directory[key] = member
        return directory

    # ------------------------------------------------------------------ #
    # Teams worksheet reads/writes (coach-created rounds)
    # ------------------------------------------------------------------ #
    def _read_all_teams_sync(self) -> list[list[str]]:
        """Return all Teams rows (including header) as lists of strings."""

        return self._require_teams_worksheet().get_all_values()

    @staticmethod
    def serialize_teams(teams: Sequence[tuple[str, Sequence[Any]]]) -> str:
        """Serialize teams to the ``Name=id,id|Name=id,id`` cell format.

        Chosen over JSON so the cell stays readable and hand-editable in the
        sheet, the same reasoning as the old pairs format. ``|`` separates
        teams, ``=`` separates a team's name from its members, ``,`` separates
        members — so a team name may not contain ``|`` or ``=`` (the command
        rejects such names before they reach here). ``/team`` writes numeric
        ids; a row typed by hand may use NAMES instead, which
        :meth:`resolve_team_members` looks up in the ``Members`` tab.
        """

        return "|".join(
            f"{name}=" + ",".join(str(member) for member in members)
            for name, members in teams
        )

    @staticmethod
    def parse_teams(raw: str) -> list[tuple[str, list[str]]]:
        """Parse a ``Name=member,member|Name=member`` cell into RAW tokens.

        Members come back as strings, not ids, because a hand-written row may
        name people instead of pasting numeric ids — resolution happens in
        :meth:`resolve_team_members`, which needs the async ``Members`` read.

        Never raises: a hand-edited or corrupt cell degrades to the entries
        that do parse, so a typo in the sheet cannot break the scheduled board.
        """

        teams: list[tuple[str, list[str]]] = []
        for chunk in (raw or "").split("|"):
            entry = chunk.strip()
            if not entry:
                continue
            name, separator, members_raw = entry.partition("=")
            if not separator:
                logger.warning("Teams: skipping malformed entry %r.", entry)
                continue
            members = [t.strip() for t in members_raw.split(",") if t.strip()]
            if not members:
                logger.warning("Teams: entry %r has no members; skipping.", entry)
                continue
            teams.append((name.strip(), members))
        return teams

    async def resolve_team_members(
        self, teams: Sequence[tuple[str, Sequence[str]]]
    ) -> list[tuple[str, list[int]]]:
        """Resolve raw member tokens to Telegram ids.

        A purely numeric token is already an id (what ``/team`` writes). Any
        other token is a NAME, looked up in the hand-maintained ``Members`` tab
        — which is the point: a coach can type the round straight into the
        sheet using the names they already use.

        An unresolvable name is logged and SKIPPED rather than raising. This
        path runs from the scheduled board with nobody watching, so one typo
        in the sheet must cost one person, never the whole post. ``/team``
        itself is strict — it refuses the message instead.
        """

        needs_directory = any(
            not token.lstrip("-").isdigit()
            for _name, members in teams
            for token in members
        )
        directory: dict[str, dict[str, Any]] = {}
        if needs_directory:
            try:
                directory = await self.member_directory()
            except Exception as exc:  # noqa: BLE001 - board must still post
                logger.error(
                    "Teams: could not read Members to resolve names: %s", exc
                )

        resolved: list[tuple[str, list[int]]] = []
        for name, members in teams:
            ids: list[int] = []
            for token in members:
                if token.lstrip("-").isdigit():
                    ids.append(int(token))
                    continue
                member = directory.get(self.normalize_member_name(token))
                if member is None:
                    logger.warning(
                        "Teams: member %r in team %r is not in the Members "
                        "tab; skipping them.",
                        token,
                        name,
                    )
                    continue
                ids.append(member["user_id"])
            if ids:
                resolved.append((name, ids))
            else:
                logger.warning(
                    "Teams: team %r has no resolvable members; skipping it.",
                    name,
                )
        return resolved

    def _parse_teams_row(self, row: list[str]) -> Optional[dict[str, Any]]:
        """Parse one Teams row into a dict, or None when unusable."""

        if len(row) <= _TEAMS_COL_STATUS:
            return None
        try:
            start_date = date.fromisoformat(row[_TEAMS_COL_START_DATE].strip())
            end_date = date.fromisoformat(row[_TEAMS_COL_END_DATE].strip())
        except (ValueError, IndexError):
            logger.warning("Teams: skipping row with unparseable dates.")
            return None
        teams = self.parse_teams(row[_TEAMS_COL_TEAMS])
        if not teams:
            return None
        return {
            # A row typed by hand may leave round_id blank; synthesise a stable
            # one from the dates so the round can still be marked posted.
            "round_id": (
                row[_TEAMS_COL_ROUND_ID].strip()
                or f"manual-{start_date.isoformat()}-{end_date.isoformat()}"
            ),
            "start_date": start_date,
            "end_date": end_date,
            "teams": teams,
            "status": row[_TEAMS_COL_STATUS].strip().lower(),
            "created_by": (
                row[_TEAMS_COL_CREATED_BY]
                if len(row) > _TEAMS_COL_CREATED_BY
                else ""
            ),
        }

    async def list_team_rounds(self) -> list[dict[str, Any]]:
        """Return every parseable Teams round, in sheet order."""

        rows = await asyncio.to_thread(self._read_all_teams_sync)
        rounds: list[dict[str, Any]] = []
        for row in rows[1:]:  # skip header
            record = self._parse_teams_row(row)
            if record is not None:
                rounds.append(record)
        # Resolve names -> ids once per round (a no-op for id-only rows).
        for record in rounds:
            record["teams"] = await self.resolve_team_members(record["teams"])
        return [record for record in rounds if record["teams"]]

    async def get_current_team_round(self) -> Optional[dict[str, Any]]:
        """Return the ACTIVE team round, or ``None`` when there is none.

        The last active row wins, so re-running /team (which cancels the
        previous round first) always reads back the newest one.
        """

        current: Optional[dict[str, Any]] = None
        for record in await self.list_team_rounds():
            if record["status"] == TEAMS_STATUS_ACTIVE:
                current = record
        return current

    def _append_teams_round_sync(self, values: list[str]) -> None:
        self._require_teams_worksheet().append_row(
            values, value_input_option="RAW"
        )

    async def create_team_round(
        self,
        round_id: str,
        start_date: date,
        end_date: date,
        teams: Sequence[tuple[str, Sequence[int]]],
        created_by: int,
    ) -> None:
        """Append a new ACTIVE team round covering ``[start_date, end_date]``."""

        values = [
            round_id,
            start_date.isoformat(),
            end_date.isoformat(),
            self.serialize_teams(teams),
            TEAMS_STATUS_ACTIVE,
            str(created_by),
            self._now_iso(),
        ]
        await self._retry_blocking(
            self._append_teams_round_sync,
            values,
            label=f"create_team_round {round_id}",
        )

    def _set_teams_status_sync(self, round_id: str, status: str) -> None:
        worksheet = self._require_teams_worksheet()
        rows = worksheet.get_all_values()
        for offset, row in enumerate(rows[1:], start=2):  # header is row 1
            if len(row) <= _TEAMS_COL_ROUND_ID:
                continue
            if row[_TEAMS_COL_ROUND_ID].strip() != round_id:
                continue
            column = chr(ord("A") + _TEAMS_COL_STATUS)
            worksheet.update(
                values=[[status]],
                range_name=f"{column}{offset}",
                value_input_option="RAW",
            )
            return
        logger.warning("Teams: round %r not found; status not updated.", round_id)

    async def set_team_round_status(self, round_id: str, status: str) -> None:
        """Set a team round's status (``posted`` / ``cancelled``)."""

        await self._retry_blocking(
            self._set_teams_status_sync,
            round_id,
            status,
            label=f"set_team_round_status {round_id}",
        )

    async def _retry_blocking(
        self, func: Any, *args: Any, label: str = "operation"
    ) -> None:
        """Run a blocking callable in a thread with transient-retry/backoff.

        Mirrors :meth:`append_workout`'s retry policy for Plans upserts.
        """

        for attempt in range(1, _APPEND_MAX_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(func, *args)
            except Exception as exc:
                if not _is_transient_error(exc):
                    logger.error(
                        "%s failed with a permanent error: %s", label, exc
                    )
                    raise
                if attempt < _APPEND_MAX_ATTEMPTS:
                    backoff = _APPEND_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient failure for %s (attempt %d/%d): %s; "
                        "retrying in %.1fs.",
                        label,
                        attempt,
                        _APPEND_MAX_ATTEMPTS,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                logger.error(
                    "%s failed after %d attempts: %s",
                    label,
                    _APPEND_MAX_ATTEMPTS,
                    exc,
                )
                raise
            else:
                return

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def _append_row_sync(self, values: list[str]) -> None:
        worksheet = self._require_worksheet()
        # value_input_option=RAW writes strings verbatim, preserving large-int
        # IDs and hashes as plain text.
        worksheet.append_row(values, value_input_option="RAW")

    async def append_workout(self, row: WorkoutLogRow) -> bool:
        """Append a confirmed workout row to the ``Log`` worksheet.

        Retries the blocking ``append_row`` up to :data:`_APPEND_MAX_ATTEMPTS`
        times on transient failures (network errors, timeouts, 5xx/429 API
        errors) with exponential backoff (1s, 2s, 4s). Permanent client errors
        (e.g. 401/403 permission) are NOT retried and propagate immediately.

        Returns ``True`` once the row is confirmed written. Raises the
        underlying exception on final failure so the caller can avoid sending a
        success reply for an unrecorded point.
        """

        values = row.to_sheet_row()
        last_exc: Optional[BaseException] = None

        for attempt in range(1, _APPEND_MAX_ATTEMPTS + 1):
            try:
                # Blocking gspread write stays off the event loop.
                await asyncio.to_thread(self._append_row_sync, values)
            except Exception as exc:
                if not _is_transient_error(exc):
                    # Permanent failure (e.g. 401/403 permission) — do not retry.
                    logger.error(
                        "Append failed with a permanent error for user %s: %s",
                        row.telegram_user_id,
                        exc,
                    )
                    raise
                last_exc = exc
                if attempt < _APPEND_MAX_ATTEMPTS:
                    backoff = _APPEND_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    logger.warning(
                        "Transient append failure for user %s (attempt %d/%d): "
                        "%s; retrying in %.1fs.",
                        row.telegram_user_id,
                        attempt,
                        _APPEND_MAX_ATTEMPTS,
                        exc,
                        backoff,
                    )
                    await asyncio.sleep(backoff)
                    continue
                # Exhausted retries on a transient error.
                logger.error(
                    "Append failed after %d attempts for user %s: %s",
                    _APPEND_MAX_ATTEMPTS,
                    row.telegram_user_id,
                    exc,
                )
                raise
            else:
                username_label = (
                    f"@{row.telegram_username}" if row.telegram_username else "-"
                )
                logger.info(
                    "Logged workout: user=%s username=%s date=%s points=%s",
                    row.telegram_user_id,
                    username_label,
                    row.workout_date,
                    row.points,
                )
                return True

        # Unreachable in practice (loop either returns True or raises), but keep
        # a defensive failure signal for the caller.
        if last_exc is not None:  # pragma: no cover - defensive
            raise last_exc
        return False  # pragma: no cover - defensive


def _check_sheets_sync(service_account_info: dict[str, Any], sheet_id: str) -> str:
    """Blocking Sheets health check. Returns the spreadsheet title on success.

    Authorizes with the service account, opens the spreadsheet by key, ensures
    the ``Log`` worksheet exists (creating it — which itself requires Editor
    access — if missing, matching :meth:`SheetsService._init_sync`), and reads
    the header row to confirm authenticated read access. Any failure raises the
    underlying gspread/Google exception for the caller to classify.
    """

    credentials = Credentials.from_service_account_info(
        service_account_info, scopes=_SCOPES
    )
    client = gspread.authorize(credentials)
    spreadsheet = client.open_by_key(sheet_id)

    try:
        worksheet = spreadsheet.worksheet(WORKSHEET_NAME)
    except gspread.WorksheetNotFound:
        # Creating the worksheet requires Editor access; this both provisions
        # the tab and proves write permission without polluting the Log data.
        worksheet = spreadsheet.add_worksheet(
            title=WORKSHEET_NAME, rows=1000, cols=len(HEADER_ROW)
        )
        worksheet.update(values=[HEADER_ROW], range_name="A1")

    # Confirm authenticated read access to the worksheet.
    worksheet.row_values(1)
    return spreadsheet.title


async def check_sheets(settings: "Settings") -> tuple[bool, str]:
    """Verify Google Sheets connectivity, access, and the ``Log`` worksheet.

    Reusable by both ``/testsheet`` and ``/status``. Performs a real
    authorize → open → ensure-worksheet → read-header check (see
    :func:`_check_sheets_sync`); creating the ``Log`` tab when absent also
    proves Editor access without appending junk to the real ``Log`` data.

    All blocking gspread calls run in :func:`asyncio.to_thread`. Full error
    detail is logged; only a concise, secret-free reason is returned.

    Returns:
        A ``(ok, message)`` tuple. On success, ``message`` is a user-friendly
        line naming the spreadsheet title. On failure, ``message`` is a short,
        user-facing reason (never containing key material).
    """

    if not settings.google_sheet_id:
        return False, "GOOGLE_SHEET_ID is not set — set it in your environment."

    try:
        title = await asyncio.to_thread(
            _check_sheets_sync,
            settings.google_service_account_info,
            settings.google_sheet_id,
        )
    except gspread.SpreadsheetNotFound as exc:
        logger.error("Sheets health check failed (spreadsheet not found): %s", exc)
        return (
            False,
            "spreadsheet not found — check GOOGLE_SHEET_ID and that the sheet "
            "is shared with the service-account email.",
        )
    except gspread.exceptions.APIError as exc:
        logger.error("Sheets health check failed (API error): %s", exc)
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (401, 403):
            client_email = settings.google_service_account_info.get(
                "client_email", "the service account"
            )
            return (
                False,
                "permission denied — share the sheet (Editor) with "
                f"{client_email}.",
            )
        return False, "Google API error — see logs for detail."
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Sheets health check failed (unexpected): %s", exc)
        return (
            False,
            "connection failed — check GOOGLE_SERVICE_ACCOUNT_JSON and network.",
        )

    return True, f'Spreadsheet "{title}" reachable, "{WORKSHEET_NAME}" worksheet OK.'