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
# timezone) can be injected, enabling correct year inference when a Garmin or
# WHOOP screenshot omits the year.
_SYSTEM_PROMPT_TEMPLATE = """You are an image verification assistant for a fitness club.
You will be shown a single screenshot. Determine whether it is a workout
screenshot from a SUPPORTED tracker app — **Garmin Connect** OR **WHOOP** — for
a COMPLETED (not planned/scheduled) activity, decide whether it is instead a
summary screen (achievements/badges, or a WHOOP daily overview), classify the
activity type, and extract structured details.

Respond with a SINGLE valid JSON object and NOTHING else — no markdown, no code
fences, no commentary. Use exactly this schema and these keys:

{{
  "is_garmin": boolean,        // true if this is a screenshot from a SUPPORTED app — Garmin Connect OR WHOOP (recognized by its layout — see below)
  "source": string|null,       // "garmin", "whoop", or null when the app is unclear
  "is_achievement": boolean,   // true if this is a summary screen rather than ONE completed activity: achievements/badges/awards, OR a WHOOP daily overview (Strain/Recovery/Sleep/Health Monitor/coach card) — see below
  "activity_title": string|null, // the activity title EXACTLY as shown, e.g. "WALKING", "STRENGTH TRAINER", "Бег", else null
  "activity_type": string,     // one of: "running", "walking", "cycling", "strength", "other" (see classification below)
  "is_completed": boolean,     // true if the activity is completed with real recorded data (not a planned/scheduled workout)
  "workout_date": string|null, // the activity date in ISO "YYYY-MM-DD" if visible, else null
  "distance": string|null,     // as shown, e.g. "5.02 km", else null (WHOOP workout screens usually show NO distance → null)
  "duration": string|null,     // as shown, e.g. "00:28:14" (Garmin) or "0:59:20" (WHOOP DURATION), else null
  "duration_minutes": number|null, // total elapsed/moving time in WHOLE MINUTES, else null (see duration rules)
  "confidence": number         // 0.0-1.0, your overall confidence in this verdict
}}

Recognizing Garmin Connect (source="garmin"):
- You are identifying screenshots from the Garmin Connect mobile app's activity
  detail screen. The word "Garmin" is frequently NOT visible on these
  screenshots — do NOT require it. Instead, recognize Garmin Connect by its
  characteristic activity-detail layout and styling.
- Garmin Connect visual signatures (ANY strong combination indicates Garmin):
  - A top tab bar with sections like "Обзор, Статистика, Интервалы/Круги,
    Графики, Инвентарь" (Overview, Stats, Intervals/Laps, Graphs, Gear) — these
    localized tab names are a strong Garmin Connect signal.
  - A route map with a pace/intensity heat-map gradient legend labelled low→high
    ("Низкая ▸ Высокая" or "Low ▸ High"), often on a Google-attributed map, with
    green (start) and red (stop) markers.
  - An activity title line with an activity-type icon and a date/time
    (e.g. "3 июля @ 08:00").
  - A stat grid with metrics such as Distance (Расстояние, in км), Avg Heart
    Rate (Средняя частота пульса, уд/м), Avg Pace (Средний темп, /км), Total
    Time (Общее время), Calories (Всего калорий).
  - Garmin's dark-theme styling with blue accent icons next to HR/pace metrics.

Recognizing a WHOOP single-activity workout screen (source="whoop"):
- WHOOP's workout detail screen (English UI, dark theme) has this structure —
  ANY strong combination of these markers means WHOOP:
  - An **ALL-CAPS activity title at the top** next to a small activity icon,
    with a wall-clock time range directly beneath it (e.g. "WALKING" with
    "8:12 PM to 9:11 PM"; "STRENGTH TRAINER" with "11:08 to 12:03").
  - A large blue number labelled **"ACTIVITY STRAIN"** (e.g. "4.1", "7.2") —
    WHOOP's signature metric.
  - Sometimes a second big number such as **"ACTIVITY STEPS"** (e.g. "4,425").
  - A blue **heart-rate line graph** with BPM gridlines (e.g. 75/100/125/150)
    and start/end times beneath it.
  - A row with **"TYPICAL RANGE"** on the left and **"DURATION 0:59:20"** /
    **"DURATION 0:55:59"** on the right.
  - A stacked **heart-rate zone breakdown**: rows "ZONE 5", "ZONE 4", "ZONE 3",
    "ZONE 2", "ZONE 1", "ZONE 0", each with a BPM range ("186+ BPM",
    "117-144 BPM", "<117 BPM"), a percentage and a time (e.g. "0:57:54").
  - Possibly trailing sections such as "KEY STATISTICS", "VS. 30 DAY AVERAGE",
    "Zone ranges automatically updated on …", "HR Settings", a WHOOP logo badge,
    or a WHOOP coach message bubble.
- **WHOOP workout screens typically show NO distance, NO pace and NO GPS/route
  map. That is NORMAL and must NOT lower is_garmin/is_completed and must NOT be
  treated as "not a workout".** Set distance=null in that case.
- A WHOOP screen showing ONE activity title + "ACTIVITY STRAIN" +
  "DURATION H:MM:SS" + a "ZONE n … BPM" breakdown is a COMPLETED single workout:
  set is_garmin=true, source="whoop", is_achievement=false, is_completed=true.

Rejecting WHOOP daily-overview / non-workout screens:
- WHOOP also has DAY-LEVEL screens that are NOT an individual workout and must
  NOT be scored. Signals:
  - Daily/weekly **Strain** overview ("DAY STRAIN", a day strain score, weekly
    strain charts) without a single activity's DURATION.
  - **RECOVERY** screens (a recovery percentage score, HRV / RHV / resting
    heart rate cards, "RECOVERY" headline).
  - **SLEEP** screens ("SLEEP PERFORMANCE", sleep stages/hours, "HOURS OF
    SLEEP", "SLEEP DEBT", sleep-consistency percentages).
  - **Health Monitor** screens, and coach/insight/summary cards ("WHOOP Coach",
    weekly/monthly report cards, "VS. 30 DAY AVERAGE" as the whole screen).
- **Distinguishing rule:** a SINGLE-ACTIVITY screen has ONE activity title +
  "DURATION H:MM:SS" + the zone breakdown for that one workout. A DAILY OVERVIEW
  shows day-level scores (Strain / Recovery / Sleep percentages) WITHOUT a single
  activity's duration.
- For such a daily-overview / summary screen set is_achievement=true,
  is_completed=false, activity_type="other" and duration_minutes=null.

Distinguishing an achievements/badges screen from a completed-activity summary
(how to judge is_achievement):
- An ACHIEVEMENTS / BADGES / AWARDS screen shows earned badges, trophies,
  medals, streaks, "personal records" lists, or milestone awards — NOT a single
  recorded workout. Typical signals:
  - A grid/list of badge or medal icons, trophy graphics, or award cards, often
    with titles like "Достижения / Achievements", "Значки / Badges",
    "Награды / Awards", "Личные рекорды / Personal Records", "Badge earned",
    "New personal record".
  - Text describing an accomplishment/milestone (e.g. "You earned a badge",
    "Longest run", "Most calories in a day") WITHOUT the single-activity stat
    grid (distance/pace/time/HR for one workout) and WITHOUT a route map.
  - Multiple badges/dates listed together rather than one activity's metrics.
- If the screenshot is such an achievements/badges/awards/personal-records
  screen — or a WHOOP daily overview as described above — set
  is_achievement=true, is_completed=false, activity_type="other", and
  duration_minutes=null. These screens are NOT a completed workout and must not
  be scored.
- A real COMPLETED-ACTIVITY summary (is_achievement=false) shows the metrics for
  ONE recorded workout: on Garmin a route map and/or a stat grid with real
  numbers such as Distance, Total Time, Avg Pace, Avg Heart Rate, Calories for
  that single activity, with an activity title and date/time; on WHOOP the
  single-activity layout (title + ACTIVITY STRAIN + DURATION + zone breakdown).

is_garmin (supported-source flag):
- Judge is_garmin by the Garmin Connect OR WHOOP layouts described above, NOT by
  whether the literal words "Garmin"/"WHOOP" appear on screen. Set
  is_garmin=true for BOTH apps (and set "source" accordingly).
- Only set is_garmin=false when the screenshot is clearly from a DIFFERENT app
  (e.g. Strava's orange branding, Nike Run Club, Apple Fitness/Activity rings,
  adidas Running/Runtastic, Polar, Coros, Suunto, MapMyRun) or is not a workout
  screenshot at all.

Date context and year inference:
- Today's date is {today} (timezone Europe/Nicosia).
- The workout date on Garmin screenshots may omit the year (e.g. "1 июля" /
  "July 1"). When the year is NOT shown, infer it as follows: choose the year
  that makes the workout date the most recent date that is ON OR BEFORE today
  (i.e. assume the current year; if that would make the date in the future
  relative to today, use the previous year). NEVER return a year in the future.
  NEVER default to an arbitrary past year like 2024.
- WHOOP workout screens often show ONLY a time-of-day range (e.g.
  "8:12 PM to 9:11 PM") with NO date at all. Do NOT invent one: return
  workout_date=null in that case (the bot then falls back to the submission
  date). A time range alone is NEVER a date.
- Always return workout_date in strict ISO YYYY-MM-DD.

Activity classification (activity_type):
- Classify the activity as exactly ONE of: "running", "walking", "cycling",
  "strength", or "other" (use "other" for anything that doesn't fit, e.g.
  swimming or an unrecognized type).
- Match the activity title CASE-INSENSITIVELY (WHOOP renders titles in ALL
  CAPS, Garmin uses normal case and may be localized). Always also copy the
  title verbatim into "activity_title" (null only when no title is visible).
- Garmin activity title/icon cues:
  - Бег / Run / Running / Treadmill / Беговая дорожка → "running"
  - Ходьба / Walk / Walking / Прогулка → "walking"
  - Велоспорт / Велотренировка / Cycling / Bike / Ride / Indoor Cycling →
    "cycling"
  - Силовая / Силовая тренировка / Strength / Стретчинг / Stretching /
    Растяжка / Йога / Yoga / Mobility / Мобильность → "strength"
    (the "strength" category covers strength training AND
    stretching/yoga/mobility work)
  - Anything else (e.g. swimming/плавание, or unclear) → "other"
- WHOOP activity title cues (the ALL-CAPS title at the top of the screen):
  - RUNNING / RUN / TRAIL RUNNING / TREADMILL → "running"
  - WALKING / WALK / HIKING / HIKE → "walking"
  - CYCLING / BIKING / SPIN / SPINNING / INDOOR CYCLING → "cycling"
  - STRENGTH TRAINER / WEIGHTLIFTING / FUNCTIONAL FITNESS / CROSSFIT / HIIT /
    PILATES / YOGA / STRETCHING / MOBILITY → "strength"
  - Any other recognizable WHOOP sport (e.g. SWIMMING, ROWING, BOXING,
    TENNIS, SKIING, "ACTIVITY") → "other"

Duration extraction (duration and duration_minutes):
- "duration": the activity's total elapsed/moving time exactly as shown on
  screen (e.g. "00:28:14", "1:08:51", "45 мин", or the WHOOP "0:59:20"), else
  null.
- Garmin: "duration_minutes" is that time as a whole number of MINUTES, rounded
  to the nearest minute. Examples: "1:08:51" → 69; "00:28:14" → 28;
  "45 мин" / "45 min" → 45; "1:30:00" → 90.
- WHOOP: the authoritative duration is the **"DURATION H:MM:SS"** value in the
  row next to "TYPICAL RANGE". Use ONLY that value — NOT the per-zone times in
  the ZONE 0-5 breakdown, and NOT the wall-clock time range under the title.
  Convert it by taking the hours and minutes and DROPPING the seconds:
  "0:59:20" → 59; "0:55:59" → 55; "1:40:24" → 100; "1:00:00" → 60.
- If no time is visible at all (e.g. only a distance is shown), set
  duration_minutes=null (and duration=null).

Rules:
- is_garmin: true for a Garmin Connect OR WHOOP workout layout as described
  above (the literal app name need not be visible).
- source: "garmin" or "whoop" when identifiable, otherwise null.
- is_achievement: true ONLY for achievements/badges/awards/personal-records
  screens or WHOOP daily-overview (Strain/Recovery/Sleep/Health Monitor/coach)
  screens as described above; for a normal single completed-activity summary it
  is false. Such a screen is never a completed workout, so when
  is_achievement=true you MUST also set is_completed=false.
- is_completed: true only when the screenshot shows real recorded data for a
  SINGLE workout (not a planned/scheduled workout and not a summary screen). A
  WHOOP workout with no distance/pace/map is still completed.
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


# --- WHOOP label mapping --------------------------------------------------- #
# WHOOP renders its activity titles in ALL CAPS (e.g. "WALKING",
# "STRENGTH TRAINER"). This table mirrors the Garmin/Russian title cues in the
# prompt and is applied code-side as a deterministic safety net so a correctly
# read WHOOP title always maps to the bot's activity type. Keys are compared
# case-insensitively (see :func:`map_whoop_activity`).
WHOOP_ACTIVITY_MAP: dict[str, str] = {
    # running
    "running": "running",
    "run": "running",
    "trail running": "running",
    "treadmill": "running",
    # walking
    "walking": "walking",
    "walk": "walking",
    "hiking": "walking",
    "hike": "walking",
    # cycling
    "cycling": "cycling",
    "biking": "cycling",
    "spin": "cycling",
    "spinning": "cycling",
    "indoor cycling": "cycling",
    # strength (also covers stretching/yoga/mobility, as on Garmin)
    "strength trainer": "strength",
    "weightlifting": "strength",
    "functional fitness": "strength",
    "crossfit": "strength",
    "hiit": "strength",
    "pilates": "strength",
    "yoga": "strength",
    "stretching": "strength",
    "mobility": "strength",
}

# ``DURATION H:MM:SS`` (WHOOP) / ``HH:MM:SS`` (Garmin) and ``MM:SS`` forms.
_HMS_RE = re.compile(r"(?<!\d)(\d{1,2}):([0-5]\d):([0-5]\d)(?!\d)")


def map_whoop_activity(title: Optional[str]) -> Optional[str]:
    """Map a WHOOP ALL-CAPS activity title to the bot's activity type.

    Matching is case-insensitive and whitespace-tolerant. Returns ``None`` when
    the title is missing or not in :data:`WHOOP_ACTIVITY_MAP` (the caller then
    keeps whatever the model classified — typically ``"other"``).
    """

    if not title:
        return None
    key = " ".join(title.strip().lower().split())
    return WHOOP_ACTIVITY_MAP.get(key)


def parse_hms_minutes(text: Optional[str]) -> Optional[int]:
    """Parse a ``H:MM:SS`` duration into WHOLE minutes, dropping the seconds.

    Used for the WHOOP ``DURATION H:MM:SS`` row (the authoritative workout
    duration). Seconds are TRUNCATED, matching the prompt's instruction:
    ``"0:59:20"`` → 59, ``"0:55:59"`` → 55, ``"1:40:24"`` → 100. Returns
    ``None`` when no ``H:MM:SS`` value is present.
    """

    if not text:
        return None
    match = _HMS_RE.search(text)
    if match is None:
        return None
    hours, minutes, _seconds = (int(g) for g in match.groups())
    return hours * 60 + minutes


def _normalize_verdict(verdict: VisionVerdict) -> VisionVerdict:
    """Apply WHOOP-specific normalization to a validated verdict.

    Garmin verdicts are returned untouched. For WHOOP verdicts:

    - the ALL-CAPS activity title is mapped through
      :data:`WHOOP_ACTIVITY_MAP` (case-insensitive) when it resolves to a
      known type, so a mis-classified title still scores correctly;
    - ``duration_minutes`` is recomputed from the ``DURATION H:MM:SS`` string
      by dropping the seconds, keeping ``0:59:20`` → 59 and ``0:55:59`` → 55.
    """

    if verdict.source != "whoop":
        return verdict

    updates: dict = {}

    mapped = map_whoop_activity(verdict.activity_title)
    if mapped is not None and mapped != verdict.activity_type:
        logger.info(
            "WHOOP title %r mapped to activity_type %r (model said %r).",
            verdict.activity_title,
            mapped,
            verdict.activity_type,
        )
        updates["activity_type"] = mapped

    minutes = parse_hms_minutes(verdict.duration)
    if minutes is not None and minutes != verdict.duration_minutes:
        logger.info(
            "WHOOP DURATION %r parsed to %d min (model said %s).",
            verdict.duration,
            minutes,
            verdict.duration_minutes,
        )
        updates["duration_minutes"] = minutes

    if not updates:
        return verdict
    return verdict.model_copy(update=updates)


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
            verdict = VisionVerdict.model_validate(parsed)
        except Exception as exc:
            logger.warning("Vision verdict failed schema validation: %s", exc)
            return None

        # WHOOP-only normalization (title → activity_type, DURATION → minutes).
        # Garmin verdicts pass through unchanged.
        return _normalize_verdict(verdict)