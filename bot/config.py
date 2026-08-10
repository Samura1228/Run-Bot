"""Configuration loading and validation.

Reads all environment variables into a typed :class:`Settings` object using
Pydantic. Fails fast with a clear error at startup if any required variable is
missing or malformed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date
from functools import lru_cache
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator

logger = logging.getLogger(__name__)

# Coach-configured competition pairs (Telegram user IDs), used as the default
# when the ``PAIRS`` env var is unset. Each tuple is ``(member_a, member_b)``
# and the order is preserved in the rendered pairs leaderboard label.
DEFAULT_PAIRS: tuple[tuple[int, int], ...] = (
    (5025515480, 572559211),      # ArtLike_ (@artquite)  + MY (@MaksYezhovv)
    (6599040404, 6108222286),     # AB (@Mak1225)         + Elena (no username)
    (1406051646, 6572975237),     # . (@amgborz)          + Anastasia S (@asn_nova)
    (1274840834, 871410038),      # Матвѣй (@shut_obychniy) + Marfa Sh (@FundersVC)
)


class Settings(BaseModel):
    """Typed, validated application settings sourced from environment variables."""

    telegram_bot_token: str = Field(..., min_length=1)
    anthropic_api_key: str = Field(..., min_length=1)
    anthropic_model: str = Field(default="claude-3-5-sonnet-20241022")
    # Optional sampling temperature for Anthropic calls. Leave unset (None) for
    # newer models (e.g. claude-sonnet-5) that REJECT the ``temperature``
    # parameter — when None, ``temperature`` is omitted entirely from requests.
    # Set to a number (e.g. 0) only for older models that support/require it.
    anthropic_temperature: Optional[float] = None
    google_service_account_info: dict[str, Any]
    google_sheet_id: str = Field(..., min_length=1)
    target_chat_id: Optional[int] = None
    timezone: str = Field(default="Europe/Nicosia")
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    points_per_run: int = Field(default=10, ge=0)
    log_level: str = Field(default="INFO")
    # Season start date: points and the leaderboard count ONLY workout
    # submissions whose ``workout_date`` is on or after this date. Submissions
    # before it are ignored entirely (a fresh season "resets" everyone to zero
    # without deleting registrations or coach-assigned plans). Sourced from the
    # optional ``SEASON_START_DATE`` env var (``YYYY-MM-DD``); defaults to the
    # 2026-07-12 season restart.
    season_start_date: date = Field(default=date(2026, 7, 12))
    # Late-submission grace period: on MONDAY, until this hour (local time in
    # ``timezone``), a workout dated in the immediately-preceding Mon–Sun week is
    # still accepted and scored — because the weekly boards do not post until
    # Mon 09:00 (pairs) / 09:05 (individual), so those points can still
    # legitimately count. Sourced from the optional
    # ``LATE_SUBMISSION_GRACE_UNTIL_HOUR`` env var; defaults to 9 to match the
    # 09:00 board. Set to ``0`` to DISABLE the grace period (strict
    # current-week-only behaviour).
    late_submission_grace_until_hour: int = Field(default=9, ge=0, le=23)
    # Telegram user IDs allowed to set/view OTHER users' plans (coaches).
    # Sourced from the optional ``COACH_IDS`` env var (comma-separated). Empty
    # set means no coaches configured (self-service still works for everyone).
    coach_ids: set[int] = Field(default_factory=set)
    # Competition pairs for the weekly PAIRS leaderboard: a list of
    # ``(member_a_id, member_b_id)`` tuples in the coach's configured order.
    # Sourced from the optional ``PAIRS`` env var (``id+id,id+id``); defaults to
    # :data:`DEFAULT_PAIRS`. An EMPTY list disables the pairs board entirely
    # (nothing is posted) — set ``PAIRS=`` (blank) to opt out.
    pairs: list[tuple[int, int]] = Field(
        default_factory=lambda: [tuple(pair) for pair in DEFAULT_PAIRS]
    )

    def is_coach(self, user_id: int) -> bool:
        """Return True if the given Telegram user id is a configured coach."""

        return user_id in self.coach_ids

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        return value.upper()

    @field_validator("google_service_account_info")
    @classmethod
    def _validate_service_account(cls, value: dict[str, Any]) -> dict[str, Any]:
        if "client_email" not in value or "private_key" not in value:
            raise ValueError(
                "GOOGLE_SERVICE_ACCOUNT_JSON must contain 'client_email' and "
                "'private_key' fields"
            )
        return value


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if value is None or value.strip() == "":
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def _parse_season_start_date(raw: Optional[str]) -> Optional[date]:
    """Parse the ``SEASON_START_DATE`` env var into a :class:`datetime.date`.

    The value must be an ISO ``YYYY-MM-DD`` string (e.g. ``2026-07-12``). A
    blank/unset value yields ``None`` so the :class:`Settings` default (the
    2026-07-12 season restart) applies. An invalid value raises
    :class:`ConfigError` so a typo fails fast at startup rather than silently
    counting the wrong submissions.
    """

    if raw is None or raw.strip() == "":
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            "SEASON_START_DATE must be an ISO date (YYYY-MM-DD), "
            f"got: {raw!r}"
        ) from exc


def _parse_late_submission_grace_until_hour(raw: Optional[str]) -> Optional[int]:
    """Parse ``LATE_SUBMISSION_GRACE_UNTIL_HOUR`` into an hour-of-day int.

    The value is the Monday hour (local time, 0–23) until which a
    previous-week workout is still accepted; ``0`` disables the grace period
    entirely (strict current-week-only). A blank/unset value yields ``None`` so
    the :class:`Settings` default (``9``, matching the Mon 09:00 leaderboard)
    applies. Like ``SEASON_START_DATE``, a malformed value raises
    :class:`ConfigError` so a typo fails fast at startup rather than silently
    changing which submissions are counted.
    """

    if raw is None or raw.strip() == "":
        return None
    try:
        hour = int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            "LATE_SUBMISSION_GRACE_UNTIL_HOUR must be an integer hour 0–23 "
            f"(0 disables the grace period), got: {raw!r}"
        ) from exc
    if not 0 <= hour <= 23:
        raise ConfigError(
            "LATE_SUBMISSION_GRACE_UNTIL_HOUR must be an integer hour 0–23 "
            f"(0 disables the grace period), got: {hour}"
        )
    return hour


def _parse_pairs(raw: Optional[str]) -> Optional[list[tuple[int, int]]]:
    """Parse the ``PAIRS`` env var into a list of ``(member_a, member_b)`` ids.

    Format: pairs separated by ``,``, the two Telegram user IDs within a pair
    separated by ``+`` — e.g. ``123+456,789+1011``. Whitespace around any token
    is ignored, as are blank entries.

    Returns:
        ``None`` when the variable is UNSET (so the :class:`Settings` default,
        :data:`DEFAULT_PAIRS`, applies), an EMPTY list when it is explicitly set
        to a blank string (pairs feature disabled — nothing is posted), or the
        parsed list otherwise.

    Raises:
        ConfigError: If an entry is malformed (not exactly two ``+``-separated
            tokens, or a token that is not an integer). Like
            ``SEASON_START_DATE``, a typo fails fast at startup rather than
            silently pairing the wrong people.
    """

    if raw is None:
        return None
    if raw.strip() == "":
        return []

    pairs: list[tuple[int, int]] = []
    for token in raw.split(","):
        entry = token.strip()
        if entry == "":
            continue
        members = [member.strip() for member in entry.split("+")]
        if len(members) != 2 or any(member == "" for member in members):
            raise ConfigError(
                "PAIRS entries must be exactly two Telegram user IDs joined by "
                f"'+' (e.g. 123+456), got: {entry!r}"
            )
        try:
            member_a, member_b = (int(member) for member in members)
        except ValueError as exc:
            raise ConfigError(
                "PAIRS member IDs must be integers (e.g. 123+456), "
                f"got: {entry!r}"
            ) from exc
        pairs.append((member_a, member_b))
    return pairs


def _parse_coach_ids(raw: Optional[str]) -> set[int]:
    """Parse the ``COACH_IDS`` env var into a set of integer user IDs.

    The value is a comma-separated list of Telegram user IDs (e.g. ``123,456``).
    Whitespace and blank entries are ignored. Non-integer entries are skipped
    with a logged warning (rather than raising) so a typo never blocks boot.
    A blank/unset value yields an empty set.
    """

    if raw is None or raw.strip() == "":
        return set()

    coach_ids: set[int] = set()
    for token in raw.split(","):
        entry = token.strip()
        if entry == "":
            continue
        try:
            coach_ids.add(int(entry))
        except ValueError:
            logger.warning(
                "Ignoring invalid COACH_IDS entry %r (not an integer).", entry
            )
    return coach_ids


def _parse_service_account_json(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:  # pragma: no cover - defensive
        raise ConfigError(
            "GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON: " f"{exc}"
        ) from exc
    if not isinstance(parsed, dict):
        raise ConfigError("GOOGLE_SERVICE_ACCOUNT_JSON must be a JSON object")
    return parsed


def load_settings() -> Settings:
    """Load and validate all settings from the environment.

    Raises:
        ConfigError: If a required variable is missing or malformed.
    """

    telegram_bot_token = _require("TELEGRAM_BOT_TOKEN")
    anthropic_api_key = _require("ANTHROPIC_API_KEY")
    google_sheet_id = _require("GOOGLE_SHEET_ID")
    service_account_raw = _require("GOOGLE_SERVICE_ACCOUNT_JSON")

    # TARGET_CHAT_ID is optional at startup: the bot can boot without it so the
    # operator can run /chatid to discover the ID, then set it and redeploy.
    target_chat_id: Optional[int] = None
    target_chat_id_raw = os.environ.get("TARGET_CHAT_ID")
    if target_chat_id_raw is not None and target_chat_id_raw.strip() != "":
        try:
            target_chat_id = int(target_chat_id_raw)
        except ValueError as exc:
            raise ConfigError(
                f"TARGET_CHAT_ID must be an integer, got: {target_chat_id_raw!r}"
            ) from exc

    service_account_info = _parse_service_account_json(service_account_raw)

    # COACH_IDS is optional: blank/unset → no coaches. Non-integer entries are
    # skipped with a warning (never a boot failure).
    coach_ids = _parse_coach_ids(os.environ.get("COACH_IDS"))

    kwargs: dict[str, Any] = {
        "telegram_bot_token": telegram_bot_token,
        "anthropic_api_key": anthropic_api_key,
        "google_service_account_info": service_account_info,
        "google_sheet_id": google_sheet_id,
        "target_chat_id": target_chat_id,
        "coach_ids": coach_ids,
    }

    # Optional overrides.
    if os.environ.get("ANTHROPIC_MODEL"):
        kwargs["anthropic_model"] = os.environ["ANTHROPIC_MODEL"]
    # ANTHROPIC_TEMPERATURE is optional. Blank/unset → None → temperature is
    # omitted from requests (required for models like claude-sonnet-5 that
    # reject it). When set to a number, it is included in the API call.
    anthropic_temperature_raw = os.environ.get("ANTHROPIC_TEMPERATURE")
    if anthropic_temperature_raw is not None and anthropic_temperature_raw.strip() != "":
        try:
            kwargs["anthropic_temperature"] = float(anthropic_temperature_raw)
        except ValueError as exc:
            raise ConfigError("ANTHROPIC_TEMPERATURE must be a float") from exc
    if os.environ.get("TIMEZONE"):
        kwargs["timezone"] = os.environ["TIMEZONE"]
    if os.environ.get("MIN_CONFIDENCE"):
        try:
            kwargs["min_confidence"] = float(os.environ["MIN_CONFIDENCE"])
        except ValueError as exc:
            raise ConfigError("MIN_CONFIDENCE must be a float") from exc
    if os.environ.get("POINTS_PER_RUN"):
        try:
            kwargs["points_per_run"] = int(os.environ["POINTS_PER_RUN"])
        except ValueError as exc:
            raise ConfigError("POINTS_PER_RUN must be an integer") from exc
    if os.environ.get("LOG_LEVEL"):
        kwargs["log_level"] = os.environ["LOG_LEVEL"]
    # SEASON_START_DATE is optional: blank/unset → the Settings default
    # (2026-07-12) applies. An invalid value fails fast via ConfigError.
    season_start_date = _parse_season_start_date(
        os.environ.get("SEASON_START_DATE")
    )
    if season_start_date is not None:
        kwargs["season_start_date"] = season_start_date
    # LATE_SUBMISSION_GRACE_UNTIL_HOUR is optional: blank/unset → the Settings
    # default (9, matching the Mon 09:00 board); 0 disables the grace period;
    # malformed → ConfigError.
    grace_until_hour = _parse_late_submission_grace_until_hour(
        os.environ.get("LATE_SUBMISSION_GRACE_UNTIL_HOUR")
    )
    if grace_until_hour is not None:
        kwargs["late_submission_grace_until_hour"] = grace_until_hour
        if grace_until_hour == 0:
            logger.info(
                "LATE_SUBMISSION_GRACE_UNTIL_HOUR is 0 — previous-week "
                "submissions are rejected as soon as the week rolls over."
            )
    # PAIRS is optional: unset → the DEFAULT_PAIRS default applies; explicitly
    # blank → an empty list (pairs board disabled); malformed → ConfigError.
    pairs = _parse_pairs(os.environ.get("PAIRS"))
    if pairs is not None:
        kwargs["pairs"] = pairs
        if not pairs:
            logger.info("PAIRS is empty — the weekly pairs leaderboard is disabled.")

    try:
        return Settings(**kwargs)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` singleton."""

    return load_settings()