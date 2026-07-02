"""Claude vision service.

Builds the prompt, calls the Anthropic API with the image, and parses/validates
the strict JSON verdict. Any parse/validation/API failure is surfaced as
``None`` so the caller can treat it as a silent ignore.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import date
from typing import Optional

import anthropic

from bot.models import VisionVerdict
from bot.utils.dates import today_in

logger = logging.getLogger(__name__)

# The system prompt is built per-request so today's date (in the configured
# timezone) can be injected, enabling correct year inference when a Garmin
# screenshot omits the year.
_SYSTEM_PROMPT_TEMPLATE = """You are an image verification assistant for a running club.
You will be shown a single screenshot. Determine whether it is a Garmin Connect
activity screenshot for a COMPLETED (not planned/scheduled) RUNNING activity,
and extract structured details.

Respond with a SINGLE valid JSON object and NOTHING else — no markdown, no code
fences, no commentary. Use exactly this schema and these keys:

{{
  "is_garmin": boolean,        // true only if this is clearly a Garmin Connect screenshot
  "activity_type": string,     // one of: "running", "cycling", "walking", "swimming", "other", "unknown"
  "is_completed": boolean,     // true if the activity is completed with real recorded data (not a planned/scheduled workout)
  "workout_date": string|null, // the activity date in ISO "YYYY-MM-DD" if visible, else null
  "distance": string|null,     // as shown, e.g. "5.02 km", else null
  "duration": string|null,     // as shown, e.g. "00:28:14", else null
  "confidence": number         // 0.0-1.0, your overall confidence in this verdict
}}

Date context and year inference:
- Today's date is {today} (timezone Europe/Nicosia).
- The workout date on Garmin screenshots may omit the year (e.g. "1 июля" /
  "July 1"). When the year is NOT shown, infer it as follows: choose the year
  that makes the workout date the most recent date that is ON OR BEFORE today
  (i.e. assume the current year; if that would make the date in the future
  relative to today, use the previous year). NEVER return a year in the future.
  NEVER default to an arbitrary past year like 2024.
- Always return workout_date in strict ISO YYYY-MM-DD.

Rules:
- If it is not a Garmin screenshot, set is_garmin=false and confidence accordingly.
- Never invent a date; if no date is visible at all, set workout_date=null.
- Do not add extra keys. Do not omit keys."""

USER_TEXT = "Analyze the attached screenshot and return the JSON verdict per the schema."


def _build_system_prompt(today: date) -> str:
    """Build the system prompt with today's date injected for year inference."""

    return _SYSTEM_PROMPT_TEMPLATE.format(today=today.isoformat())

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# Map common image signatures to media types accepted by the Anthropic API.
_SUPPORTED_MEDIA_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def _detect_media_type(data: bytes) -> str:
    """Best-effort detection of the image media type from magic bytes.

    Defaults to ``image/jpeg`` (Telegram photos are typically JPEG).
    """

    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"GIF":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # JPEG / default.
    return "image/jpeg"


def _extract_json(text: str) -> Optional[dict]:
    """Robustly extract a JSON object from Claude's text output.

    Strips markdown code fences, attempts a direct parse, then falls back to
    the first ``{ ... }`` substring.
    """

    cleaned = text.strip()
    # Strip markdown code fences if present.
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    match = _JSON_OBJECT_RE.search(cleaned)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


class ClaudeVisionService:
    """Wraps the Anthropic client to produce validated :class:`VisionVerdict`s."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timezone: str = "Europe/Nicosia",
        max_tokens: int = 512,
        max_retries: int = 1,
        temperature: Optional[float] = None,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        # The configured IANA timezone, used to compute "today" so the prompt
        # can instruct Claude on correct year inference for no-year screenshots.
        self._timezone = timezone
        self._max_tokens = max_tokens
        self._max_retries = max_retries
        # ``temperature`` is optional: when None it is OMITTED from the request
        # so the call works with models (e.g. claude-sonnet-5) that reject the
        # parameter. When set to a number it is included.
        self._temperature = temperature

    def _call_api_sync(
        self, image_b64: str, media_type: str, system_prompt: str
    ) -> str:
        """Blocking Anthropic API call. Returns the first text block's text."""

        create_kwargs: dict = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": system_prompt,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": media_type,
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": USER_TEXT},
                    ],
                }
            ],
        }
        # Only include temperature when explicitly configured; omit otherwise so
        # models that reject the parameter still work out-of-the-box.
        if self._temperature is not None:
            create_kwargs["temperature"] = self._temperature

        response = self._client.messages.create(**create_kwargs)
        for block in response.content:
            if getattr(block, "type", None) == "text":
                return block.text
        return ""

    async def analyze(self, image_bytes: bytes) -> Optional[VisionVerdict]:
        """Analyze image bytes and return a validated verdict, or ``None``.

        Returns ``None`` on any API error, empty response, parse failure, or
        schema validation failure — the caller treats ``None`` as ignore.
        """

        try:
            image_b64 = base64.b64encode(image_bytes).decode("ascii")
        except Exception:  # pragma: no cover - defensive
            logger.warning("Failed to base64-encode image bytes; ignoring.")
            return None

        media_type = _detect_media_type(image_bytes)
        if media_type not in _SUPPORTED_MEDIA_TYPES:
            media_type = "image/jpeg"

        # Compute today's date in the configured timezone and bake it into the
        # system prompt so Claude infers the correct year for no-year screenshots.
        today = today_in(self._timezone)
        system_prompt = _build_system_prompt(today)

        raw_text: Optional[str] = None
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                raw_text = await asyncio.to_thread(
                    self._call_api_sync, image_b64, media_type, system_prompt
                )
                break
            except anthropic.APIError as exc:
                logger.warning(
                    "Anthropic API error (attempt %d/%d): %s",
                    attempt,
                    attempts,
                    exc,
                )
                if attempt < attempts:
                    await asyncio.sleep(min(2 ** attempt, 8))
                    continue
                return None
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Unexpected error calling Anthropic: %s", exc)
                return None

        if not raw_text:
            logger.warning("Empty response text from vision model; ignoring.")
            return None

        parsed = _extract_json(raw_text)
        if parsed is None:
            logger.warning("Could not parse JSON from vision response; ignoring.")
            return None

        try:
            return VisionVerdict.model_validate(parsed)
        except Exception as exc:
            logger.warning("Vision verdict failed schema validation: %s", exc)
            return None