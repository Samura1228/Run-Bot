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
ActivityType = Literal[
    "running", "cycling", "walking", "strength", "swimming", "other", "unknown"
]

# Supported screenshot sources (tracker apps). ``None`` when unidentifiable.
SourceApp = Literal["garmin", "whoop"]

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
def _format_points_cell(p: float) -> str:
    """Serialize a point value for the sheet cell as a clean decimal string.

    Writes whole numbers without a decimal (``15.0`` → ``"15"``) and fractions
    with a dot (``7.5`` → ``"7.5"``), never a locale comma. Mirrors the
    display-trimming used elsewhere so the ``Log`` sheet stores exact values.
    """

    text = f"{float(p):.2f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


class VisionVerdict(BaseModel):
    """Strict verdict returned by Claude vision.

    Extra keys are forbidden so that a malformed response fails validation and
    is treated as a non-eligible (ignore) verdict.
    """

    model_config = {"extra": "forbid"}

    # True when the screenshot comes from a SUPPORTED tracker app — Garmin
    # Connect OR WHOOP. The historical name is kept (the field gates the whole
    # pipeline); ``source`` says which app it actually was.
    is_garmin: bool
    # "garmin" / "whoop" when identifiable, else None. Informational only: it
    # never gates eligibility (both apps are treated identically for scoring),
    # but it drives the WHOOP-specific duration/label normalization in
    # ``bot.services.vision``.
    source: Optional[SourceApp] = None
    # The activity title exactly as rendered on screen (e.g. "WALKING",
    # "STRENGTH TRAINER", "Бег"), used for the WHOOP label mapping. Optional.
    activity_title: Optional[str] = None
    activity_type: ActivityType
    is_completed: bool
    # True when the screenshot is a summary screen rather than one completed
    # activity: a Garmin achievements/badges/awards screen (earned badges,
    # personal records list, trophy/medal grid) OR a WHOOP daily overview
    # (day Strain / Recovery / Sleep / Health Monitor / coach card). Such
    # screens carry no single workout's metrics (duration/activity type) and
    # MUST NOT be awarded points. Defaults to False for backward compatibility.
    is_achievement: bool = False
    workout_date: Optional[str] = None
    distance: Optional[str] = None
    duration: Optional[str] = None
    # Total elapsed/moving time in whole minutes (rounded), or None if the
    # duration couldn't be read. Used to enforce per-activity minimum-duration
    # thresholds for the bonus activities (walking/cycling/strength).
    duration_minutes: Optional[int] = None
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
        """Return True if this verdict passes the shared gating pipeline.

        Shared gating (per the blueprint) requires a completed activity from a
        supported app (Garmin Connect or WHOOP) with a valid date and
        sufficient confidence. The activity_type is NOT restricted here — the
        handler branches on activity_type after this gate (running uses the
        plan-based model; walking/cycling/strength are flat bonus activities
        with their own minimum-duration thresholds).

        ``workout_date`` is expected to be non-null by this point: WHOOP
        workout screens often show only a time-of-day range, so the handler
        fills in the submission date as a fallback BEFORE calling this gate.

        A summary screenshot (``is_achievement`` — Garmin achievements/badges
        or a WHOOP daily overview) is explicitly NOT eligible — it is not a
        completed-workout summary — but the handler detects that case
        separately so it can reply with a helpful message instead of ignoring
        silently.
        """

        return (
            self.is_garmin
            and self.is_completed
            and not self.is_achievement
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
    points: float
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
            _format_points_cell(self.points),
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
    points: float

    def label(self) -> str:
        """Return the preferred display label: name, else @username, else id."""

        if self.display_name.strip():
            return self.display_name.strip()
        if self.telegram_username.strip():
            return f"@{self.telegram_username.strip()}"
        return f"user {self.telegram_user_id}"