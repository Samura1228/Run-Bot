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
from typing import TYPE_CHECKING, Any, Optional

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

# --- Plans worksheet ------------------------------------------------------ #
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


class SheetsService:
    """Encapsulates all Google Sheets access for the bot."""

    def __init__(self, service_account_info: dict[str, Any], sheet_id: str) -> None:
        self._service_account_info = service_account_info
        self._sheet_id = sheet_id
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None
        self._plans_worksheet: Optional[gspread.Worksheet] = None

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

    async def initialize(self) -> None:
        """Authorize and prepare the worksheet (creating it if missing)."""

        await asyncio.to_thread(self._init_sync)
        logger.info("SheetsService initialized for sheet %s.", self._sheet_id)

    def _require_worksheet(self) -> gspread.Worksheet:
        if self._worksheet is None:
            raise RuntimeError("SheetsService not initialized; call initialize().")
        return self._worksheet

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
        (str), ``display_name`` (str), ``workout_date`` (date), and ``points``
        (int). Rows that fail parsing are skipped.
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
            if not in_range(wdate, start_date, end_date):
                continue
            try:
                user_id = int(row[_COL_USER_ID])
                points = int(row[_COL_POINTS])
            except (ValueError, IndexError):
                continue
            parsed.append(
                {
                    "telegram_user_id": user_id,
                    "telegram_username": row[_COL_USERNAME],
                    "display_name": row[_COL_DISPLAY_NAME],
                    "workout_date": wdate,
                    "points": points,
                }
            )
        return parsed

    async def count_user_workouts_in_week(
        self, user_id: int, week_start: date, week_end: date
    ) -> int:
        """Count a user's running workouts logged within a Mon–Sun week.

        Only rows with ``activity_type == "running"`` are counted; special rows
        such as ``streak_bonus`` and rows for other users are excluded. Used by
        the per-workout points calculation and the weekly streak rollover.
        """

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
            if in_range(wdate, week_start, week_end):
                count += 1
        return count

    async def has_streak_bonus_for_date(
        self, user_id: int, bonus_date: date
    ) -> bool:
        """Return True if a ``streak_bonus`` row already exists for the user/date.

        Used by the weekly rollover to avoid double-awarding a streak bonus if
        the scheduler misfires/coalesces and re-runs the same Monday.
        """

        rows = await asyncio.to_thread(self._read_all_records_sync)
        user_id_str = str(user_id)
        bonus_date_str = bonus_date.isoformat()

        for row in rows[1:]:  # skip header
            if len(row) <= _COL_POINTS:
                continue
            if row[_COL_USER_ID] != user_id_str:
                continue
            if row[_COL_ACTIVITY_TYPE] != "streak_bonus":
                continue
            if row[_COL_WORKOUT_DATE] == bonus_date_str:
                return True
        return False

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

    async def get_streak(self, user_id: int) -> int:
        """Return the user's streak, or 0 if no row exists."""

        record = await self.get_plan_record(user_id)
        if record is None:
            return 0
        return record["streak"]

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

    async def set_streak(
        self,
        user_id: int,
        streak: int,
        username: Optional[str] = None,
        plan: Optional[int] = None,
    ) -> None:
        """Upsert a user's streak, preserving their plan/username by default.

        When ``username``/``plan`` are provided they override the stored values
        (used by the weekly rollover for users with no prior plan row). Blocking
        calls run in :func:`asyncio.to_thread` with transient-retry.
        """

        existing = await self.get_plan_record(user_id)
        resolved_plan = plan if plan is not None else (
            existing["plan"] if existing is not None else DEFAULT_PLAN
        )
        # Preserve existing username unless an override is supplied. We must read
        # the raw username cell for that user; get_plan_record doesn't return it,
        # so fetch from list_plans lazily only when needed.
        resolved_username = username
        if resolved_username is None:
            resolved_username = ""
            for entry in await self.list_plans():
                if entry["user_id"] == user_id:
                    resolved_username = entry["username"]
                    break
        await self._retry_blocking(
            self._upsert_plan_sync,
            user_id,
            resolved_username,
            resolved_plan,
            streak,
            label=f"set_streak user={user_id}",
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