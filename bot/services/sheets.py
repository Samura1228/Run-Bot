"""Google Sheets service.

All Google Sheets I/O: dedup lookup, append row, and reading rows for
aggregation. Credentials are built from a service-account dict (no file on
disk). All blocking gspread calls are wrapped in ``asyncio.to_thread`` so they
never block the event loop.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date
from typing import Any, Optional

import gspread
from google.oauth2.service_account import Credentials

from bot.models import WorkoutLogRow
from bot.utils.dates import in_range

logger = logging.getLogger(__name__)

_SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

WORKSHEET_NAME = "Log"

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
_COL_POINTS = 6
_COL_IMAGE_HASH = 7


class SheetsService:
    """Encapsulates all Google Sheets access for the bot."""

    def __init__(self, service_account_info: dict[str, Any], sheet_id: str) -> None:
        self._service_account_info = service_account_info
        self._sheet_id = sheet_id
        self._client: Optional[gspread.Client] = None
        self._worksheet: Optional[gspread.Worksheet] = None

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

    async def initialize(self) -> None:
        """Authorize and prepare the worksheet (creating it if missing)."""

        await asyncio.to_thread(self._init_sync)
        logger.info("SheetsService initialized for sheet %s.", self._sheet_id)

    def _require_worksheet(self) -> gspread.Worksheet:
        if self._worksheet is None:
            raise RuntimeError("SheetsService not initialized; call initialize().")
        return self._worksheet

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

    # ------------------------------------------------------------------ #
    # Writes
    # ------------------------------------------------------------------ #
    def _append_row_sync(self, values: list[str]) -> None:
        worksheet = self._require_worksheet()
        # value_input_option=RAW writes strings verbatim, preserving large-int
        # IDs and hashes as plain text.
        worksheet.append_row(values, value_input_option="RAW")

    async def append_workout(self, row: WorkoutLogRow) -> None:
        """Append a confirmed workout row to the ``Log`` worksheet.

        Raises the underlying exception on failure so the caller can avoid
        sending a success reply for an unrecorded point.
        """

        await asyncio.to_thread(self._append_row_sync, row.to_sheet_row())
        logger.info(
            "Appended workout for user %s (%s), %s pts on %s.",
            row.telegram_user_id,
            row.display_name,
            row.points,
            row.workout_date,
        )