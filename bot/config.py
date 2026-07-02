"""Configuration loading and validation.

Reads all environment variables into a typed :class:`Settings` object using
Pydantic. Fails fast with a clear error at startup if any required variable is
missing or malformed.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any, Optional

from pydantic import BaseModel, Field, ValidationError, field_validator


class Settings(BaseModel):
    """Typed, validated application settings sourced from environment variables."""

    telegram_bot_token: str = Field(..., min_length=1)
    anthropic_api_key: str = Field(..., min_length=1)
    anthropic_model: str = Field(default="claude-3-5-sonnet-20241022")
    google_service_account_info: dict[str, Any]
    google_sheet_id: str = Field(..., min_length=1)
    target_chat_id: Optional[int] = None
    timezone: str = Field(default="Europe/Nicosia")
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)
    points_per_run: int = Field(default=10, ge=0)
    log_level: str = Field(default="INFO")

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

    kwargs: dict[str, Any] = {
        "telegram_bot_token": telegram_bot_token,
        "anthropic_api_key": anthropic_api_key,
        "google_service_account_info": service_account_info,
        "google_sheet_id": google_sheet_id,
        "target_chat_id": target_chat_id,
    }

    # Optional overrides.
    if os.environ.get("ANTHROPIC_MODEL"):
        kwargs["anthropic_model"] = os.environ["ANTHROPIC_MODEL"]
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

    try:
        return Settings(**kwargs)
    except ValidationError as exc:
        raise ConfigError(f"Invalid configuration: {exc}") from exc


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached :class:`Settings` singleton."""

    return load_settings()