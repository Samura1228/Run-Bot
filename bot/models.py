"""Typed data models used across the bot.

- :class:`VisionVerdict` — the strict JSON schema Claude must return.
- :class:`WorkoutLogRow` — a single confirmed & awarded workout row.
- :class:`LeaderboardEntry` — an aggregated per-user leaderboard entry.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Activity types Claude is allowed to return.
ActivityType = Literal["running", "cycling", "walking", "swimming", "other", "unknown"]

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class VisionVerdict(BaseModel):
    """Strict verdict returned by Claude vision.

    Extra keys are forbidden so that a malformed response fails validation and
    is treated as a non-eligible (ignore) verdict.
    """

    model_config = {"extra": "forbid"}

    is_garmin: bool
    activity_type: ActivityType
    is_completed: bool
    workout_date: Optional[str] = None
    distance: Optional[str] = None
    duration: Optional[str] = None
    confidence: float = Field(..., ge=0.0, le=1.0)

    @field_validator("workout_date")
    @classmethod
    def _validate_workout_date(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if not _ISO_DATE_RE.match(value):
            raise ValueError("workout_date must match YYYY-MM-DD")
        # Ensure it is a real calendar date.
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("workout_date is not a valid calendar date") from exc
        return value

    def is_eligible(self, min_confidence: float) -> bool:
        """Return True if this verdict is eligible for a points award.

        Eligibility (per the blueprint) requires a completed Garmin running
        activity with a valid date and sufficient confidence.
        """

        return (
            self.is_garmin
            and self.activity_type == "running"
            and self.is_completed
            and self.workout_date is not None
            and self.confidence >= min_confidence
        )


class WorkoutLogRow(BaseModel):
    """A single confirmed workout row written to the ``Log`` worksheet.

    Column order mirrors the sheet header exactly.
    """

    timestamp: str
    telegram_user_id: int
    telegram_username: str
    display_name: str
    workout_date: str
    activity_type: str
    points: int
    image_hash: str
    telegram_file_id: str
    chat_id: int
    message_id: int

    @classmethod
    def now_timestamp(cls) -> str:
        """Return the current UTC time as an ISO 8601 string (e.g. ``...Z``)."""

        return (
            datetime.now(timezone.utc)
            .replace(microsecond=0)
            .strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    def to_sheet_row(self) -> list[str]:
        """Serialize to a list of plain-text cells matching the header order.

        IDs are written as strings to avoid large-integer precision loss.
        """

        return [
            self.timestamp,
            str(self.telegram_user_id),
            self.telegram_username,
            self.display_name,
            self.workout_date,
            self.activity_type,
            str(self.points),
            self.image_hash,
            self.telegram_file_id,
            str(self.chat_id),
            str(self.message_id),
        ]


class LeaderboardEntry(BaseModel):
    """An aggregated leaderboard entry for a single user over a date range."""

    telegram_user_id: int
    display_name: str
    telegram_username: str
    points: int

    def label(self) -> str:
        """Return the preferred display label: name, else @username, else id."""

        if self.display_name.strip():
            return self.display_name.strip()
        if self.telegram_username.strip():
            return f"@{self.telegram_username.strip()}"
        return f"user {self.telegram_user_id}"