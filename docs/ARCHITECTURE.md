# Run Bot — Architecture & Blueprint

> **Status:** Design blueprint (implementation-ready). No application code is included here.
> **Purpose:** A Python Telegram bot that passively monitors a running group, validates **Garmin Connect *and* WHOOP** workout screenshots via Claude vision, awards points (plan-based for running; flat bonus points for walking/cycling/strength), logs to Google Sheets, and posts weekly/monthly leaderboards.
>
> **Supported screenshot sources:** **Garmin Connect** (English **and** Russian UI, with distance/pace/route map) and **WHOOP** (English UI, `ACTIVITY STRAIN` + `DURATION H:MM:SS` + HR-zone breakdown, typically with **no** distance/pace/map). Both are scored **identically** — there is no separate WHOOP scoring path. WHOOP **daily overview** screens (Strain / Recovery / Sleep / Health Monitor / coach cards) are **rejected** like Garmin achievements screens. See Section 4.
>
> **Supported activities:** `running` (plan-based fractional points — the core loop), plus three **bonus** activities that award a flat **5 points** each once a minimum duration is met: **walking** (≥ **40 min**), **cycling** (≥ **60 min**), and **strength** (strength training / stretching / yoga / mobility, ≥ **15 min**). Bonus activities go through the **same** gating pipeline as running (Garmin, completed, current-week, dedup) but are **separate**: they do **not** count toward the running plan, streak, or overachievement. They **do** count in the weekly/monthly leaderboards.

---

## 1. High-Level Overview

**Run Bot** is deployed as a **Railway worker service** running a single long-lived async Python process. It:

1. Joins a Telegram group and passively listens to all messages.
2. On any **photo** message, downloads the image, hashes the raw bytes (dedup), and sends it to **Claude vision**.
3. Claude returns a **strict JSON verdict** (is it a supported — Garmin **or** WHOOP — workout screenshot, completed, with a workout date, etc.).
4. The bot applies **points/date-window logic**: if the workout date falls in the **accepted window** — the **current Mon–Sun week** (Europe/Nicosia), extended over the **just-finished** week while it is Monday before the `LATE_SUBMISSION_GRACE_UNTIL_HOUR` cutoff (default **09:00**, i.e. before the weekly boards post) → award points and log to Google Sheets (row written first, then an INFO log line), then reply in chat. A late-but-accepted row keeps its **real `workout_date`**, so it counts toward the week being reported. **Running** uses the user's **weekly plan** (see Section 5) and replies `✅ Nice run, {name}! +{points} points.`. **Walking/cycling/strength** award a flat **5 points** once their minimum duration is met and reply `✅ Nice {walk|ride|strength session}, {name}! +5 points.`; below their minimum duration the bot replies with a short warning and awards nothing. A photo that is **NOT a Garmin/WHOOP screenshot at all** (nature photo, meme, another app) is ignored in **complete silence** so the group is never spammed; a photo that IS a tracker screenshot but earns nothing still gets a short explanatory reply (see Section 5).
5. An **APScheduler** (AsyncIOScheduler, timezone `Europe/Nicosia`) runs on the same event loop and posts a **weekly pairs leaderboard** (Monday 09:00), a **weekly individual leaderboard** (Monday 09:05) and a **monthly leaderboard** (1st of month 09:00). Both weekly jobs perform the (idempotent) **streak rollover** first, so streak bonuses are included in whichever board posts first.
6. **Google Sheets is the single source of truth.** Leaderboards are computed by reading and aggregating the sheet, so restarts lose no data.

### Component Diagram

```mermaid
flowchart TD
    TG[Telegram Group] -->|photo message| PTB[python-telegram-bot<br/>long-polling]
    PTB --> H[PhotoHandler]
    H --> DL[Download image bytes]
    DL --> HASH[Compute image_hash<br/>SHA-256 of bytes]
    HASH --> DEDUP{Dedup check<br/>user+hash in Sheet?}
    DEDUP -->|duplicate| IGN1[Silently ignore]
    DEDUP -->|new| VIS[Claude Vision Service]
    VIS --> VER[Parse & validate JSON verdict]
    VER --> SRC{Garmin/WHOOP<br/>screenshot at all?}
    SRC -->|no / low confidence / parse fail| IGN2[Silently ignore<br/>ordinary photos]
    SRC -->|yes| DEC{Decision:<br/>running/walking/cycling/strength +<br/>completed + in accepted window?<br/>(current week, or previous week<br/>on Mon before 09:00)}
    DEC -->|no| REJ[No points +<br/>explanatory reply]
    DEC -->|yes| AWARD[Award plan-based points]
    AWARD --> SHEET[(Google Sheet<br/>source of truth)]
    AWARD --> LOG[INFO log + chat reply<br/>after Sheet write]

    SCHED[APScheduler<br/>AsyncIOScheduler<br/>Europe/Nicosia] -->|mon 09:00| WEEK[Weekly leaderboard job]
    SCHED -->|day=1 09:00| MONTH[Monthly leaderboard job]
    WEEK --> READ1[Read & aggregate Sheet]
    MONTH --> READ2[Read & aggregate Sheet]
    READ1 --> SHEET
    READ2 --> SHEET
    WEEK --> POST1[Post ranked totals to group]
    MONTH --> POST2[Post ranked totals to group]

    subgraph Railway Worker Process (single asyncio loop)
        PTB
        H
        VIS
        SCHED
    end
```

### Runtime Concurrency Model

- Single OS process, single asyncio event loop.
- `python-telegram-bot` v21 runs long-polling on that loop.
- `APScheduler`'s `AsyncIOScheduler` is attached to the **same** loop.
- Google Sheets and Claude are network I/O; blocking SDK calls are wrapped in `asyncio.to_thread(...)` to avoid blocking the loop.

---

## 2. File / Module Structure

```
run-bot/
├── docs/
│   └── ARCHITECTURE.md          # This document
├── bot/
│   ├── __init__.py
│   ├── main.py                  # Entry point: build Application, register handlers, start scheduler, run polling
│   ├── config.py                # Loads & validates env vars into a typed Settings object
│   ├── models.py                # Dataclasses/Pydantic models: VisionVerdict, WorkoutLogRow, LeaderboardEntry, PairEntry
│   ├── handlers/
│   │   ├── __init__.py
│   │   └── photo.py             # PhotoHandler: orchestrates download → hash → dedup → vision → decision → log/reply
│   ├── services/
│   │   ├── __init__.py
│   │   ├── vision.py            # ClaudeVisionService: build prompt, call Anthropic, parse/validate strict JSON
│   │   ├── sheets.py            # SheetsService: gspread client, append_row, read rows, dedup lookup, aggregation
│   │   ├── scheduler.py         # SchedulerService: configure AsyncIOScheduler cron jobs (weekly/monthly)
│   │   └── leaderboard.py       # LeaderboardService: date-range aggregation + message formatting
│   └── utils/
│       ├── __init__.py
│       ├── dates.py             # Timezone-aware week/month boundary helpers (Europe/Nicosia)
│       ├── hashing.py           # compute_image_hash(bytes) -> str (SHA-256 hex)
│       └── points.py            # Points rules; ACTIVITY_POINTS mapping (extensible; running active only)
├── requirements.txt             # python-telegram-bot>=21, APScheduler, gspread, google-auth, anthropic, pydantic, tzdata
├── railway.toml                 # Railway worker service config (start command)
├── Procfile                     # worker: python -m bot.main  (fallback if railway.toml not used)
├── .env.example                 # Documented example of all env vars (no secrets)
└── README.md                    # Run/deploy instructions
```

### Responsibilities (one line each)

| Module | Responsibility |
|---|---|
| [`bot/main.py`](bot/main.py) | Application entry point; wires config, services, handlers, scheduler; starts long-polling. |
| [`bot/config.py`](bot/config.py) | Read & validate all environment variables; expose a typed `Settings` singleton. |
| [`bot/models.py`](bot/models.py) | Typed data models: `VisionVerdict`, `WorkoutLogRow`, `LeaderboardEntry`, `PairEntry` (a pair's combined total + both member labels). |
| [`bot/handlers/photo.py`](bot/handlers/photo.py) | End-to-end photo pipeline orchestration; writes to the Sheet first, then replies `✅ Nice run, {name}! +{points} points.`. |
| [`bot/services/vision.py`](bot/services/vision.py) | Call Claude vision; enforce strict JSON schema; return validated `VisionVerdict`. |
| [`bot/services/sheets.py`](bot/services/sheets.py) | All Google Sheets I/O: dedup lookup, append row, read range for aggregation. |
| [`bot/services/scheduler.py`](bot/services/scheduler.py) | Configure & start `AsyncIOScheduler` cron triggers on the PTB loop. |
| [`bot/services/leaderboard.py`](bot/services/leaderboard.py) | Aggregate points per user for a date range; format weekly/monthly messages. |
| [`bot/utils/dates.py`](bot/utils/dates.py) | Compute current/previous Mon–Sun week and previous calendar month in Europe/Nicosia, plus the accepted-submission window (`accepted_workout_window()`) and the week containing a given date (`week_bounds_containing()`). |
| [`bot/utils/hashing.py`](bot/utils/hashing.py) | Deterministic image byte hashing for dedup. |
| [`bot/utils/points.py`](bot/utils/points.py) | Plan-based points model: constants, `workout_points()` (base + overachievement) and `streak_bonus()`; the `ACTIVITY_POINTS` mapping now only gates awardable activity types (running). |

---

## 3. Google Sheet Schema

**Spreadsheet:** identified by env `GOOGLE_SHEET_ID`.

### Worksheet: `Log` (the single source of truth)

Row 1 is a fixed header row. All subsequent rows are one confirmed & awarded workout each.

| Col | Header | Type | Notes / Format |
|-----|--------|------|----------------|
| A | `timestamp` | string (ISO 8601) | UTC time the row was written, e.g. `2026-07-01T18:37:53Z`. |
| B | `telegram_user_id` | integer (stored as string) | From `message.from_user.id`. Stable per user. |
| C | `telegram_username` | string | `@username` without `@`, or empty if none. |
| D | `display_name` | string | Full name: `first_name` + `last_name` (trimmed). |
| E | `workout_date` | string (ISO date) | `YYYY-MM-DD` extracted by Claude (the activity date). |
| F | `activity_type` | string | Lowercase enum: `running` (plan-based workout), `walking`/`cycling`/`strength` (flat 5-point bonus activities), or `streak_bonus` for weekly streak-bonus rows. |
| G | `points` | number | Points awarded: for `running`, the plan-based per-workout value (an exact fraction like `7.5`); for `walking`/`cycling`/`strength`, a flat `5`; for `streak_bonus`, the streak bonus. Written with a dot decimal (never a locale comma) and trimmed of trailing zeros (`15`, `7.5`, `5`). |
| H | `image_hash` | string | SHA-256 hex of downloaded image bytes (dedup key). |
| I | `telegram_file_id` | string | Telegram `file_id` of the largest photo size. |
| J | `chat_id` | integer (stored as string) | `message.chat.id`. |
| K | `message_id` | integer (stored as string) | `message.message_id`. |

**Dedup key:** the pair (`telegram_user_id`, `image_hash`). A new submission is rejected if a row already exists with the same user id **and** image hash.

**Example header row (A1:K1):**
```
timestamp | telegram_user_id | telegram_username | display_name | workout_date | activity_type | points | image_hash | telegram_file_id | chat_id | message_id
```

**Example data row:**
```
2026-07-01T18:37:53Z | 123456789 | jrunner | Jane Runner | 2026-06-30 | running | 10 | 9f2c1a...e4 | AgACAgQAAx... | -1001234567890 | 4521
```

**Example `streak_bonus` row** (written by the Monday rollover; dated to the previous week's Sunday, with placeholder hash/file id):
```
2026-07-06T06:00:03Z | 123456789 | jrunner |  | 2026-07-05 | streak_bonus | 5 | - | - | 0 | 0
```

> **Note on storage types:** Google Sheets stores everything as cells; the "type" column indicates the logical type. IDs are written as **plain text** (leading apostrophe or explicitly value-input as string) to avoid precision loss on large integers.

### Worksheet: `Plans` (per-user weekly plans & streaks)

Auto-created (with its header row) on first run alongside the `Log` worksheet. One row per user; IDs stored as plain text (RAW).

| Col | Header | Type | Notes / Format |
|-----|--------|------|----------------|
| A | `telegram_user_id` | integer (stored as string) | The user's Telegram id (upsert key). |
| B | `telegram_username` | string | `@username` without `@`, or empty. |
| C | `plan` | integer | Workouts/week target, clamped to `[2, 6]`. Blank/invalid → default `3`. |
| D | `streak` | integer | Consecutive completed weeks. Blank/invalid → `0`. |
| E | `updated_at` | string (ISO 8601) | UTC time the row was last written. |

**Upsert key:** `telegram_user_id`. `/setplan` updates the row if present (preserving `streak`), else appends a new one. The Monday rollover updates `streak` (preserving `plan`/`username`).

**Username directory:** the `Plans` worksheet doubles as an `@username → id` directory. Because Telegram does **not** expose a numeric id from plain `@username` text, the bot learns ids opportunistically: `SheetsService.touch_user(user_id, username, display_name)` upserts **only** the identity columns (creating a row with `DEFAULT_PLAN`/streak 0 if absent, otherwise updating just the username + `updated_at` when it changed — never touching an existing `plan`/`streak`). It is called best-effort for the poster in the photo handler (wrapped so a failure never blocks/undoes workout logging) and from `/setplan`/`/myplan`/`/whoami`. `SheetsService.find_user_id_by_username(username)` scans this sheet case-insensitively (ignoring a leading `@`) and returns the **most recent** matching id, or `None`. This backs coach commands that target `@username` for anyone the bot has already seen.

---

## 4. Claude Vision Contract

### Approach

- Use the `anthropic` SDK, model configured via env `ANTHROPIC_MODEL` (e.g. `claude-3-5-sonnet-latest` or newer vision-capable model).
- Send **one user message** containing:
  1. An `image` content block (base64 of the downloaded bytes, correct `media_type`).
  2. A `text` content block with the instruction to analyze and return **only** JSON.
- Use a **system prompt** that pins the role, the strict JSON schema, and the "return JSON only, no prose" rule. The prompt classifies `activity_type` into `running`/`walking`/`cycling`/`strength`/`other` (Garmin title/icon cues: Бег/Run→running, Ходьба/Walk→walking, Велоспорт/Cycling/Ride→cycling, Силовая/Strength/Стретчинг/Stretching/Йога/Yoga→strength) and extracts `duration_minutes` (whole minutes) so the bot can enforce per-activity duration thresholds numerically.
- Set a low `temperature` (e.g. `0`) and a modest `max_tokens`.

### Supported screenshot sources: **Garmin Connect** and **WHOOP**

Both apps are first-class, go through the **same** pipeline and score
**identically** (there is no separate WHOOP scoring path). The verdict field
`is_garmin` means "from a **supported** app" (the historical name is kept), and
`source` records which one (`"garmin"` / `"whoop"` / `null`).

**WHOOP single-activity markers the prompt teaches (English UI, dark theme).** A
screen is accepted as a completed WHOOP workout when it shows:

- an **ALL-CAPS activity title** at the top next to a small activity icon, with a
  wall-clock time range beneath it (e.g. `WALKING` / `8:12 PM to 9:11 PM`,
  `STRENGTH TRAINER` / `11:08 to 12:03`);
- a large blue **`ACTIVITY STRAIN`** number (e.g. `4.1`, `7.2`) — WHOOP's
  signature metric — and sometimes a second big number such as
  **`ACTIVITY STEPS`** (`4,425`);
- a blue **heart-rate line graph** with BPM gridlines (75/100/125/150) and
  start/end times;
- a row with **`TYPICAL RANGE`** on the left and **`DURATION H:MM:SS`** on the
  right (e.g. `DURATION 0:59:20`);
- a stacked **HR zone breakdown** (`ZONE 5` … `ZONE 0`, each with a BPM range
  like `186+ BPM` / `117-144 BPM` / `<117 BPM`, a percentage and a time);
- optional trailing sections (`KEY STATISTICS`, `VS. 30 DAY AVERAGE`,
  `Zone ranges automatically updated on …`, `HR Settings`, WHOOP logo badge, a
  WHOOP coach message bubble).

> **WHOOP workout screens show NO distance, NO pace and NO GPS/route map.** That
> is normal and must never cause a rejection — the prompt states this explicitly
> so the Garmin-oriented "route map / stat grid" cues don't gate WHOOP.

**WHOOP activity-name mapping** (matched **case-insensitively**; also applied
code-side as a deterministic safety net via `WHOOP_ACTIVITY_MAP` /
`map_whoop_activity()` in [`bot/services/vision.py`](bot/services/vision.py),
using the verbatim `activity_title` returned by the model):

| WHOOP title | Activity type |
|---|---|
| `RUNNING`, `RUN`, `TRAIL RUNNING`, `TREADMILL` | `running` |
| `WALKING`, `WALK`, `HIKING`, `HIKE` | `walking` |
| `CYCLING`, `BIKING`, `SPIN`, `SPINNING`, `INDOOR CYCLING` | `cycling` |
| `STRENGTH TRAINER`, `WEIGHTLIFTING`, `FUNCTIONAL FITNESS`, `CROSSFIT`, `HIIT`, `PILATES`, `YOGA`, `STRETCHING`, `MOBILITY` | `strength` |
| anything else (e.g. `SWIMMING`, `ROWING`, `BOXING`) | `other` → not awardable (existing handling) |

**WHOOP duration.** The authoritative value is the **`DURATION H:MM:SS`** field
in the `TYPICAL RANGE` row — **not** the per-zone times and **not** the
wall-clock range under the title. It is converted to `duration_minutes` by
taking hours×60 + minutes and **dropping the seconds**:
`0:59:20` → **59**, `0:55:59` → **55**, `1:40:24` → **100**, `1:00:00` → **60**.
`parse_hms_minutes()` re-derives this from the raw `duration` string so an
off-by-one model rounding can never push a workout under its minimum.

**WHOOP daily-overview screens are rejected** (no points), reusing the existing
`is_achievement` flag so they hit the same rejection path as Garmin
achievements/badges: day/weekly **Strain** (`DAY STRAIN`), **Recovery**
(`RECOVERY`, recovery %, HRV/RHR cards), **Sleep** (`SLEEP PERFORMANCE`, sleep
stages/hours), **Health Monitor**, and coach/insight summary cards. The
distinguishing rule encoded in the prompt: a **single-activity** screen has ONE
activity title + `DURATION` + a zone breakdown for that one workout, while a
**daily overview** shows day-level scores (Strain/Recovery/Sleep percentages)
without a single activity's duration. Such screens must return
`is_achievement=true`, `is_completed=false`, `activity_type="other"`,
`duration_minutes=null`, and the bot replies:

```
⚠️ This looks like a summary/achievements screen, not a completed workout. Please send the workout summary screenshot from Garmin or WHOOP.
```

**Missing date (WHOOP).** WHOOP workout screens usually show only a time-of-day
range and no date, so the prompt forbids inventing one (`workout_date=null`) and
[`bot/handlers/photo.py`](bot/handlers/photo.py) falls back to the
**submission/message date** (the Telegram message timestamp converted to
`TIMEZONE`) before the eligibility gate. Nothing is rejected and no empty date is
ever written. Garmin screenshots keep whatever date the model read.

### System Prompt (verbatim intent)

```
You are an image verification assistant for a fitness club.
You will be shown a single screenshot. Determine whether it is a workout
screenshot from a SUPPORTED tracker app — Garmin Connect OR WHOOP — for a
COMPLETED (not planned/scheduled) activity, decide whether it is instead a
summary screen (achievements/badges, or a WHOOP daily overview), classify the
activity type, and extract structured details.

Respond with a SINGLE valid JSON object and NOTHING else — no markdown, no code
fences, no commentary. Use exactly this schema and these keys:

{
  "is_garmin": boolean,          // true if from a SUPPORTED app: Garmin Connect OR WHOOP
  "source": string|null,         // "garmin", "whoop", or null when unclear
  "is_achievement": boolean,     // true for achievements/badges OR a WHOOP daily overview (Strain/Recovery/Sleep/Health Monitor/coach card)
  "activity_title": string|null, // the title verbatim, e.g. "WALKING", "STRENGTH TRAINER", "Бег"
  "activity_type": string,       // one of: "running", "walking", "cycling", "strength", "other"
  "is_completed": boolean,       // true if the activity is completed with real recorded data (not a planned/scheduled workout)
  "workout_date": string|null,   // the activity date in ISO "YYYY-MM-DD" if visible, else null
  "distance": string|null,       // as shown, e.g. "5.02 km", else null (WHOOP workouts: null)
  "duration": string|null,       // as shown, e.g. "00:28:14" (Garmin) or "0:59:20" (WHOOP DURATION), else null
  "duration_minutes": number|null, // total elapsed/moving time in WHOLE MINUTES, else null
  "confidence": number           // 0.0–1.0, your overall confidence in this verdict
}

Garmin Connect layout cues: localized tab bar (Обзор/Статистика/...), a route
map with a low→high pace heat-map legend, an activity title + date/time, and a
stat grid (Distance/Расстояние, Avg Pace/Средний темп, Total Time/Общее время,
HR, Calories).

WHOOP single-activity layout cues: an ALL-CAPS activity title + a wall-clock
time range, a large "ACTIVITY STRAIN" number (sometimes "ACTIVITY STEPS"), a
blue HR graph with BPM gridlines, a "TYPICAL RANGE … DURATION H:MM:SS" row, and
a "ZONE 5 … ZONE 0" BPM/percentage/time breakdown. WHOOP workouts show NO
distance, NO pace and NO map — that is normal and must NOT cause a rejection.

WHOOP daily overviews are NOT workouts (is_achievement=true, is_completed=false,
activity_type="other", duration_minutes=null): DAY STRAIN, RECOVERY (recovery %,
HRV/RHR), SLEEP PERFORMANCE / sleep stages, Health Monitor, coach/insight cards.
Rule: a single-activity screen has ONE title + DURATION + zone breakdown; a
daily overview shows day-level scores without a single activity's duration.

Activity classification cues (title/icon, matched CASE-INSENSITIVELY):
- Garmin: Бег/Run/Running/Treadmill → "running"; Ходьба/Walk/Walking →
  "walking"; Велоспорт/Cycling/Bike/Ride → "cycling"; Силовая/Strength/
  Стретчинг/Stretching/Йога/Yoga/Mobility → "strength"; anything else → "other".
- WHOOP: RUNNING/RUN/TRAIL RUNNING/TREADMILL → "running"; WALKING/WALK/HIKING/
  HIKE → "walking"; CYCLING/BIKING/SPIN/SPINNING/INDOOR CYCLING → "cycling";
  STRENGTH TRAINER/WEIGHTLIFTING/FUNCTIONAL FITNESS/CROSSFIT/HIIT/PILATES/YOGA/
  STRETCHING/MOBILITY → "strength"; anything else (SWIMMING, ROWING…) → "other".

Duration:
- Garmin: "duration_minutes" = elapsed/moving time in whole minutes, rounded to
  nearest. "1:08:51" → 69; "00:28:14" → 28; "45 мин" → 45.
- WHOOP: use ONLY the "DURATION H:MM:SS" value next to "TYPICAL RANGE" (never
  the zone times, never the wall-clock range) and DROP the seconds:
  "0:59:20" → 59; "0:55:59" → 55; "1:40:24" → 100.
- If no time is visible at all, set duration_minutes=null.

Rules:
- If it is not a Garmin/WHOOP screenshot, set is_garmin=false and confidence accordingly.
- Never invent a date; if the date is not clearly visible (WHOOP usually shows
  only a time range), set workout_date=null — the bot falls back to the
  submission date.
- Do not add extra keys. Do not omit keys.
```

### User Message (text block)

```
Analyze the attached screenshot and return the JSON verdict per the schema.
```

### JSON Schema (canonical, for validation)

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `is_garmin` | boolean | yes | "from a **supported** app" — Garmin Connect **or** WHOOP (name kept for compatibility). |
| `source` | string \| null | no (defaults `null`) | enum: `garmin`, `whoop`, or `null`. Informational; drives the WHOOP-only title/duration normalization. |
| `activity_title` | string \| null | no (defaults `null`) | The title verbatim (e.g. `WALKING`, `STRENGTH TRAINER`, `Бег`); used by the WHOOP label mapping. |
| `activity_type` | string | yes | enum: `running`, `walking`, `cycling`, `strength`, `other` (the model also tolerates legacy `swimming`/`unknown`, treated as non-awardable) |
| `is_completed` | boolean | yes | — |
| `workout_date` | string \| null | yes | ISO `YYYY-MM-DD` when non-null |
| `distance` | string \| null | yes | free text or null |
| `duration` | string \| null | yes | free text or null |
| `duration_minutes` | number \| null | no (defaults `null`) | whole minutes of elapsed/moving time; used to enforce bonus-activity thresholds. A missing/`null` value never crashes parsing. |
| `confidence` | number | yes | 0.0 ≤ x ≤ 1.0 |

### Parsing & Validation (in [`bot/services/vision.py`](bot/services/vision.py))

1. Extract the text from the first `text` content block of the response.
2. **Robust JSON extraction:** attempt `json.loads` on the trimmed text; if it fails, extract the first `{ ... }` substring and retry.
3. Validate against the schema (Pydantic model `VisionVerdict`), coercing types where safe.
4. Validate `workout_date` matches `^\d{4}-\d{2}-\d{2}$` and is a real date when non-null.
5. **Failure handling → treat as IGNORE (no log, no reply):**
   - JSON parse failure after fallback.
   - Schema validation failure (missing/extra keys, wrong types).
   - `confidence < MIN_CONFIDENCE` (env `MIN_CONFIDENCE`, default `0.6`).
6. On any **API error** (network, rate limit, timeout) → log a warning and IGNORE (do not reply, do not log to Sheet). Optionally retry once with backoff before ignoring.

### Verdict → Award Eligibility

A verdict passes the **shared gating pipeline** only if **all** are true:
- `is_garmin == true` (Garmin **or** WHOOP)
- `is_completed == true`
- `is_achievement == false` (not an achievements/badges screen and not a WHOOP daily overview)
- `workout_date` is a valid, non-null ISO date — when the screenshot showed no date at all (typical for WHOOP) the handler has already substituted the **submission/message date** before this gate
- `confidence >= MIN_CONFIDENCE`

`VisionVerdict.is_eligible()` intentionally does **not** restrict the activity
type — the handler branches on `activity_type` after this gate:
- `activity_type == "running"` → plan-based points (Section 5).
- `activity_type in {"walking", "cycling", "strength"}` → flat 5-point bonus,
  subject to the per-activity minimum duration (Section 5).
- anything else (`other`/legacy) → **no points**. If the image was a Garmin/WHOOP screenshot the bot replies `⚠️ This activity type doesn't earn points. Points are awarded for running, walking, cycling and strength workouts.`; if it was not a tracker screenshot at all (`is_garmin=false`) it is ignored silently — see Section 5.

If gated-in, proceed to the date-window/points decision (Section 5). Otherwise IGNORE.

---

## 5. Points & Date-Window Logic

### Definition of "current Mon–Sun week" (Europe/Nicosia)

- All boundary math is done in the **Europe/Nicosia** timezone (`ZoneInfo("Europe/Nicosia")`), then reduced to plain calendar **dates**.
- "Now" = current datetime in Europe/Nicosia. Its **date** is `today`.
- **Week start (Monday):** `week_start = today - timedelta(days=today.weekday())` where `weekday()` is 0=Monday … 6=Sunday.
- **Week end (Sunday):** `week_end = week_start + timedelta(days=6)`.
- A `workout_date` is **in the current week** iff `week_start <= workout_date <= week_end` (inclusive, date comparison).

> Because comparison is date-based (not datetime), there is no ambiguity around midnight or DST for the eligibility check; the Europe/Nicosia timezone is only used to determine what "today" is.

### Late-submission grace period (accepted window ⊇ current week)

The weekly boards do not post until **Mon 09:00** (pairs) / **09:05** (individual), so a workout finished on Sunday but posted a few minutes after midnight can still legitimately count toward the week being reported. Previously the handler compared the workout date against `current_week_bounds()` alone and rejected such a submission the instant the week rolled over — **silently**, producing the production log line `Workout date 2026-08-09 is outside current week (2026-08-10—2026-08-16); ignoring.`

The date check therefore uses an **accepted window** rather than the bare current week, computed by `accepted_workout_window(tz, grace_until_hour, now=<message timestamp>)` in [`bot/utils/dates.py`](bot/utils/dates.py):

- Normally the window is exactly the current Mon–Sun week.
- When the submission moment is **Monday** and its **local hour < `grace_until_hour`**, the window is extended **backwards by 7 days** to `(current_week_start - 7d, current_week_end)`, so the whole just-finished week is accepted.
- `grace_until_hour == 0` disables the extension entirely (strict current-week-only).
- The submission moment is the **message's own timestamp** (converted to the configured timezone), not wall-clock "now", so the decision is deterministic and testable.

**The real `workout_date` is preserved.** An accepted late row is written with its actual date (e.g. `2026-08-09`) and is **never** shifted into the new week. Consequences:

- **Aggregation:** `read_rows_in_range(prev_start, prev_end)` for the previous Mon–Sun window includes the row, so it counts on the 09:00/09:05 boards for the week it belongs to.
- **Running weekly count:** the handler derives the scoring week with `week_bounds_containing(wdate)` — the week the workout's **own date** falls in — and passes it to `count_user_workouts_in_week()`. A late run is therefore counted against the **previous** week, keeping the plan-based `30/plan` rate and the `OVERACHIEVEMENT_RATE` halving correct rather than mis-scoring it as the new week's first workout.
- **Streak rollover:** unchanged — it reads the same previous-week window, so a late row also feeds the streak evaluation when the rollover runs.

The `SEASON_START_DATE` cutoff and duplicate-image detection stay **fully in force** for late submissions.

Configured by `LATE_SUBMISSION_GRACE_UNTIL_HOUR` → `Settings.late_submission_grace_until_hour` (default **9**). A malformed value (non-integer or outside 0–23) raises `ConfigError` at startup, matching the fail-fast style of `SEASON_START_DATE`.

### Season start date (points/leaderboard reset cutoff)

A configurable **season start date** (`SEASON_START_DATE`, default **2026-07-12**, exposed as `Settings.season_start_date`) defines a hard cutoff for **all** points/leaderboard aggregation. Any submission whose `workout_date` is **before** the season start date is ignored entirely, so a new season effectively restarts everyone at **zero**.

The cutoff is applied at the single source of truth — the date-filtered read methods in [`bot/services/sheets.py`](bot/services/sheets.py): `read_rows_in_range()` (used by the leaderboard and the streak rollover) and `count_user_workouts_in_week()` (used by the per-workout points calculation and the streak rollover) skip any row with `workout_date < season_start_date` via the shared `_before_season()` helper. Because points are stored **per-row** (not as a running cumulative total), every user total is recomputed from qualifying rows on each read — the reset is fully effective without editing or deleting any data.

**What is NOT affected:** the reset is purely a read-time cutoff. Old `Log` rows are left untouched (nothing is deleted). User registrations and coach-assigned plans/streaks live in the separate `Plans` worksheet and are **never** filtered by this cutoff — they remain fully intact. Dedup (`is_duplicate()`) also ignores the cutoff so a pre-season screenshot can never be re-submitted.

### Plan-Based Points Model

Each user has a weekly **plan** — the number of workouts/week they aim for — stored in the `Plans` worksheet. Plans are set **by a coach only** with `/setplan @user N` (or by replying to the member) and clamped to `[MIN_PLAN, MAX_PLAN]` = **2–6** (five plans: 2, 3, 4, 5, 6); the default is **3** for users whose coach never set one. Regular users cannot set up their own plan (`/setplan` rejects non-coaches with `Only your coach can set up workouts for you.`).

Constants live in [`bot/utils/points.py`](bot/utils/points.py):

| Constant | Value | Meaning |
|----------|-------|---------|
| `STANDARD_WORKOUTS_PER_WEEK` | 3 | Reference plan. |
| `STANDARD_POINTS_PER_WEEK` | 30 | Points for completing the plan. |
| `MIN_PLAN` / `MAX_PLAN` | 2 / 6 | Allowed plan range. |
| `DEFAULT_PLAN` | 3 | Plan used when a user has no `Plans` row. |
| `OVERACHIEVEMENT_RATE` | 0.5 | Multiplier for workouts logged **beyond** the plan. |
| `STREAK_BONUS_PER_WEEK` | `[0,0,0,5,10,15,20]` | Bonus by consecutive completed weeks (capped at last index). |

**Per-workout points** (`workout_points(plan, workouts_this_week_so_far)`):

```python
base_rate = STANDARD_POINTS_PER_WEEK / plan
if workouts_this_week_so_far < plan:   # within plan
    pts = base_rate
else:                                   # overachievement
    pts = base_rate * OVERACHIEVEMENT_RATE
return round(pts, 2)                     # EXACT fraction (2-dp), NOT rounded to int
```

`workouts_this_week_so_far` is how many **running** rows the user already has in the current week BEFORE the new one (streak_bonus rows and other users are excluded). Completing exactly the plan yields exactly `STANDARD_POINTS_PER_WEEK` (30) points for the week; extra workouts earn 50% of the base rate. Points are **exact fractions** (no integer rounding): e.g. plan 4 → `7.5` per workout. Plan changes apply **going forward only** — already-logged rows keep their points.

**Display formatting:** a helper `format_points(p)` trims trailing zeros / the decimal point so whole numbers show as `15` (not `15.0`) and fractions as `7.5`/`3.75`. It's used in the `✅` reply, `/setplan`, and the leaderboard totals.

**Sample per-workout values** (exact fractions):

| Plan | Within-plan | Overachievement (½) |
|------|-------------|---------------------|
| 2 | 15 | 7.5 |
| 3 | 10 | 5 |
| 4 | 7.5 | 3.75 |
| 5 | 6 | 3 |
| 6 | 5 | 2.5 |

### Bonus (non-running) activities — walking / cycling / strength

Three **bonus** activity types award a flat **5 points** each, independently of
the running plan. They go through the **same** gating pipeline as running
(Garmin + completed + valid current-week date + confidence ≥ `MIN_CONFIDENCE` +
dedup), but the points are fixed and **do not** affect the plan/streak/
overachievement (the per-user weekly running **count** used for those still
counts `activity_type == "running"` rows only).

Constants live in [`bot/utils/points.py`](bot/utils/points.py):

| Constant | Value | Meaning |
|----------|-------|---------|
| `BONUS_ACTIVITY_POINTS` | 5 | Flat points per qualifying bonus activity. |
| `ACTIVITY_MIN_MINUTES` | `{"walking": 40, "cycling": 60, "strength": 15}` | Minimum `duration_minutes` to qualify. |
| `BONUS_ACTIVITIES` | `{"walking", "cycling", "strength"}` | The set of bonus activity types. |

Per-activity minimum duration & reply behaviour (uses the vision
`duration_minutes` field):

| Activity | Minimum | On/above minimum (flat 5) | Below minimum (no log, no points) | `activity_label` |
|----------|:-------:|---------------------------|-----------------------------------|------------------|
| walking | 40 min | `✅ Nice walk, {name}! +5 points.` | `⚠️ Walk is {dur} min — minimum is 40 min to earn points.` | `walk` |
| cycling | 60 min | `✅ Nice ride, {name}! +5 points.` | `⚠️ Ride is {dur} min — minimum is 60 min to earn points.` | `ride` |
| strength | 15 min | `✅ Nice strength session, {name}! +5 points.` | `⚠️ Strength/stretch is {dur} min — minimum is 15 min to earn points.` | `strength session` |

- If `duration_minutes` is `null` (couldn't be read) for a bonus activity, the
  bot replies `⚠️ Couldn't read the duration — no points awarded.` and does
  **not** log/award.
- The below-minimum and no-duration cases reply while NOT logging. Every other
  non-eligible path (outside the accepted window, non-Garmin, not completed,
  `other`) **also replies now**, provided the image really is a Garmin/WHOOP
  screenshot — see
  [Reply policy](#reply-policy-silent-on-non-tracker-photos-explain-real-rejections).
  Non-tracker photos and duplicates stay silent.
- Qualifying bonus rows are written to `Log` (with the correct `activity_type`
  and `points = 5`) via the same write-first + retry + dedup-recheck path as
  running, and **count in the weekly/monthly leaderboards** (the aggregation
  sums all rows in range regardless of `activity_type`).

### Reply policy (silent on non-tracker photos, explain real rejections)

The group shares ordinary photos constantly, so replying to *every* rejected
photo turned the bot into a spam source (three holiday snaps → three
"couldn't confirm a workout" warnings). Replies are therefore gated on the
vision contract's supported-source flag **`verdict.is_garmin`** (true for Garmin
Connect OR WHOOP; false when the image is from a different app or is not a
workout screenshot at all — see Section 4).

**The `is_garmin` gate runs immediately after the vision call**, before any other
rejection path, so every reply below it is guaranteed to reach someone who really
did post a tracker screenshot.

Replies go through the module-local `_safe_reply()` helper (same contract as the
one in [`bot/handlers/commands.py`](bot/handlers/commands.py): it swallows and
logs `TelegramError`, so a failed send can never crash the handler or undo a
confirmed Sheet write).

**Silent (log only) — we cannot be sure the poster was even trying to log a workout:**

| Path | Rationale |
|------|-----------|
| `verdict.is_garmin == false` — nature photo, meme, selfie, or another app (Strava, Nike Run Club, Apple Fitness…) | Not a submission at all; warning would be noise. |
| Vision returned no usable verdict (parse/API failure) | We can't tell whether it was a tracker screenshot, so assume it wasn't. |
| `confidence < MIN_CONFIDENCE` | "Barely recognizable" is not a reliable basis for telling someone their screenshot is wrong. |
| **Duplicate** re-submission | Intentionally silent (pre-existing behaviour). |

**Replies — the image IS a Garmin/WHOOP screenshot, so the poster deserves to know why it scored nothing:**

| Path | Reply |
|------|-------|
| Workout date outside the accepted window | `⚠️ This workout is dated {date}, which is outside the week we're currently counting. Points can only be added for the current week.` |
| Not eligible for another reason (not completed) at or above the confidence threshold | `⚠️ Couldn't confirm a completed workout in this screenshot — no points awarded. Please send the workout summary screenshot from Garmin or WHOOP.` |
| Activity type not awardable (swimming, `other`) | `⚠️ This activity type doesn't earn points. Points are awarded for running, walking, cycling and strength workouts.` |
| `workout_date` unparseable after validation | `⚠️ Couldn't read the workout date — no points awarded.` |
| Sheet append failed after retries | `⚠️ Couldn't save this workout just now — please send the screenshot again in a few minutes.` |
| Summary/achievements screen (**unchanged text**) | `⚠️ This looks like a summary/achievements screen, not a completed workout. Please send the workout summary screenshot from Garmin or WHOOP.` |
| Bonus activity, unreadable duration (**unchanged text**) | `⚠️ Couldn't read the duration — no points awarded.` |
| Bonus activity below minimum (**unchanged text**) | `⚠️ {Noun} is {dur} min — minimum is {minimum} min to earn points.` |

Every decision — silent or not — is still logged at INFO, so an ignored photo
remains fully observable in the Railway logs.

### Decision Pseudocode

```
function decide_and_process(message, verdict, image_hash):
    # SOURCE GATE FIRST: is this a Garmin/WHOOP screenshot at all? A nature
    # photo, meme or another app's screenshot is ignored in SILENCE so the group
    # is never spammed. Every reply below is therefore only ever sent to someone
    # who really did post a tracker screenshot.
    if not verdict.is_garmin:
        return IGNORE   # silent, log only

    # A barely-recognizable image is also not a reliable basis for a warning.
    if verdict.confidence < MIN_CONFIDENCE:
        return IGNORE   # silent, log only

    # shared gating (Section 4): completed + valid date
    if not (verdict.is_completed and verdict.workout_date is not None):
        reply("⚠️ Couldn't confirm a completed workout ...")
        return REJECT

    activity = verdict.activity_type
    if activity != "running" and activity not in BONUS_ACTIVITIES:
        reply("⚠️ This activity type doesn't earn points. ...")
        return REJECT

    wdate = date.fromisoformat(verdict.workout_date)

    # Accepted window = current Mon–Sun week, extended over the PREVIOUS week
    # while it is Monday before LATE_SUBMISSION_GRACE_UNTIL_HOUR (default 9).
    accepted_start, accepted_end = accepted_workout_window(
        TIMEZONE, LATE_SUBMISSION_GRACE_UNTIL_HOUR, now=message.date)

    if not (accepted_start <= wdate <= accepted_end):
        reply(f"⚠️ This workout is dated {wdate}, which is outside the week "
              "we're currently counting. ...")
        return REJECT

    # Score against the week the workout's OWN date belongs to — for an accepted
    # late submission that is the PREVIOUS week, not the new one.
    week_start, week_end = week_bounds_containing(wdate)

    # dedup (Section 10) already checked BEFORE calling vision, but re-check race:
    if sheets.exists(user_id=message.from_user.id, image_hash=image_hash):
        return IGNORE   # silent (duplicate — the only silent path)

    if activity == "running":
        plan = sheets.get_plan(user_id)                    # default 3
        so_far = sheets.count_user_workouts_in_week(user_id, week_start, week_end)
        points = workout_points(plan, so_far)              # plan-based value
        reply = f"✅ Nice run, {name}! +{format_points(points)} points."
    else:   # walking / cycling / strength — flat bonus, separate from the plan
        dur = verdict.duration_minutes
        if dur is None:
            message.reply_text("⚠️ Couldn't read the duration — no points awarded.")
            return NO_LOG
        if dur < ACTIVITY_MIN_MINUTES[activity]:
            message.reply_text(below_minimum_message(activity, dur))
            return NO_LOG        # no log, no points
        points = BONUS_ACTIVITY_POINTS                     # flat 5
        reply = f"✅ Nice {activity_label(activity)}, {name}! +5 points."

    # The row keeps verdict.workout_date VERBATIM — a late submission is never
    # shifted into the new week.
    row = build_log_row(message, verdict, points, image_hash)
    sheets.append_row(row)                     # real-time log (write-first)
    logger.info("Logged workout: user=%s date=%s activity=%s points=%s", ...)
    message.reply_text(reply)                  # after confirmed write
    return AWARDED
```


**Write-first, then reply:** on success the row is written to the Sheet and an INFO log line `Logged workout: user=... date=... points=<computed>` is emitted; **only after** the confirmed write does the bot reply in chat with `✅ Nice run, {name}! +{points} points.`. A failed reply is logged but never undoes the saved row. Rejected Garmin/WHOOP screenshots get an explanatory reply, while non-tracker photos, unreadable/low-confidence images and duplicates stay silent (see [Reply policy](#reply-policy-silent-on-non-tracker-photos-explain-real-rejections)). (Weekly/monthly leaderboards are still posted to the group.)

### Streak Bonus (weekly rollover)

At the Monday 09:00 weekly job — **before** the leaderboard is aggregated/posted so it's reflected in that week's board — the bot evaluates the **previous** Mon–Sun week:

1. For each user in `Plans` (plus any user who logged running workouts last week but has no plan row → treated as `DEFAULT_PLAN`), count their completed running workouts.
2. If `completed >= plan` → `streak += 1`; else `streak = 0`. The new streak is persisted to `Plans`.
3. If `streak >= 1` and `STREAK_BONUS_PER_WEEK[min(streak, len-1)] > 0`, a `streak_bonus` row is appended to `Log` with `points = bonus`, `workout_date` = the previous week's **Sunday** (so it counts in that week), and placeholder hash/file id (`-`).
4. **Idempotency:** before awarding, the bot checks `Log` for an existing `streak_bonus` row for that user dated to the same previous-week Sunday and skips if found, preventing double-awarding on scheduler misfire/coalesce.

Each evaluation logs `Streak: user=<id> completed=<n>/<plan> streak=<new> bonus=<b>`. Because the leaderboard sums **all** `Log` rows in range regardless of `activity_type`, `streak_bonus` points are automatically included in the totals — while the per-user workout **count** used for streak/overachievement still counts **running** rows only (the two concerns are kept separate).
---

## 6. Scheduling Design

### Coexistence with the PTB event loop

- Build the PTB `Application` and obtain its running asyncio loop.
- Create `AsyncIOScheduler(timezone=ZoneInfo("Europe/Nicosia"))`.
- Register cron jobs, then start the scheduler inside a PTB **post-init** hook (`Application.post_init`) so it attaches to the already-running loop. Shut it down in a **post-shutdown** hook.
- All scheduled job callbacks are `async` and use the same PTB `bot` instance to send messages, and the same `SheetsService` for reads.

### Cron Triggers (timezone Europe/Nicosia)

| Job | Trigger | Fires | Action |
|-----|---------|-------|--------|
| Weekly **pairs** leaderboard | `CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=tz)` | Monday 09:00 | Run the **streak rollover**, then post ranked **combined** totals per configured pair for the **previous** Mon–Sun week. Not registered when `PAIRS` is empty. |
| Weekly individual leaderboard | `CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=tz)` | Monday 09:05 | Run the **streak rollover** (award `streak_bonus` rows), then post ranked individual totals for the **previous** Mon–Sun week. |
| Monthly leaderboard | `CronTrigger(day=1, hour=9, minute=0, timezone=tz)` | 1st of month 09:00 | Post ranked totals for the **previous** full calendar month. |

**Streak-rollover ordering.** The rollover is **not** a standalone cron job — it runs inline at the START of each weekly job (`evaluate_weekly_streaks()`), writing `streak_bonus` rows dated the **previous Sunday**, i.e. inside the week being reported. Because the pairs job now fires at 09:00 — five minutes *before* the individual job at 09:05 — the pairs job performs the rollover itself before aggregating. The rollover is **idempotent** (`SheetsService.has_streak_bonus_for_date()` skips a user who already has a bonus row for that Sunday), so whichever job runs first records the bonuses and the other simply skips them. Both boards therefore always include that week's streak bonuses, in either order.

**Failure isolation.** The two weekly jobs are registered **independently** and each swallows/logs its own exceptions, so a failure in the pairs board can never prevent the individual board from posting, and vice versa.

### Setup Pseudocode ([`bot/services/scheduler.py`](bot/services/scheduler.py))

```
def build_scheduler(bot, sheets, leaderboard, target_chat_id, tz, pairs):
    scheduler = AsyncIOScheduler(timezone=tz)

    if pairs:   # empty PAIRS → pairs board disabled, job not registered
        scheduler.add_job(
            run_weekly_pairs_leaderboard,
            CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=tz),
            args=[bot, leaderboard, sheets, list(pairs), target_chat_id, tz],
            id="weekly_pairs_leaderboard", misfire_grace_time=3600, coalesce=True,
        )
    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=tz),
        args=[bot, sheets, leaderboard, target_chat_id],
        id="weekly_leaderboard", misfire_grace_time=3600, coalesce=True,
    )
    scheduler.add_job(
        run_monthly_leaderboard,
        CronTrigger(day=1, hour=9, minute=0, timezone=tz),
        args=[bot, sheets, leaderboard, target_chat_id],
        id="monthly_leaderboard", misfire_grace_time=3600, coalesce=True,
    )
    return scheduler
```

- `misfire_grace_time` and `coalesce=True` protect against restarts near the fire time.
- Scheduler is **not** the source of truth; all data is re-read from the Sheet at fire time, so a missed run only delays a post, never loses data.

---

## 7. Leaderboard Computation

### Aggregation ([`bot/services/leaderboard.py`](bot/services/leaderboard.py))

1. Read all rows from worksheet `Log` (via `SheetsService.read_rows_in_range()`), skipping the header.
2. Filter rows where `workout_date` (column E) falls in `[range_start, range_end]` (inclusive dates) **and** is on or after `SEASON_START_DATE` (pre-season rows are excluded so the leaderboard reflects only the current season — see Section 5).
3. Group by `telegram_user_id`; sum `points` for **all** rows in range **regardless of `activity_type`** (so `running` workout points, the `walking`/`cycling`/`strength` 5-point bonuses, **and** `streak_bonus` points all count); keep the most recent `display_name`/`telegram_username` for that user id.
4. Sort descending by total points; tie-break by `display_name` alphabetically.

```
function aggregate(rows, range_start, range_end):
    totals = {}   # user_id -> {points, display_name, username}
    for r in rows:
        wdate = date.fromisoformat(r.workout_date)
        if range_start <= wdate <= range_end:
            t = totals.setdefault(r.telegram_user_id,
                                  {"points": 0.0, "display_name": r.display_name,
                                   "username": r.telegram_username})
            t["points"] += r.points
            t["display_name"] = r.display_name   # keep latest
    entries = sorted(totals.values(),
                     key=lambda e: (-e["points"], e["display_name"].lower()))
    return entries
```

### Date Ranges ([`bot/utils/dates.py`](bot/utils/dates.py))

- **Previous week (for Monday post):** current Monday minus 7 days → Sunday minus 1 day.
  - `this_monday = today - timedelta(days=today.weekday())`
  - `prev_week_start = this_monday - timedelta(days=7)`
  - `prev_week_end = this_monday - timedelta(days=1)`  (previous Sunday)
- **Previous month (for 1st-of-month post):**
  - `first_of_this_month = today.replace(day=1)`
  - `prev_month_end = first_of_this_month - timedelta(days=1)`
  - `prev_month_start = prev_month_end.replace(day=1)`

### Message Formatting

**Weekly:**
```
Weekly leaders board 🏆

Jane Runner  - 30 points 🥇
@speedy  - 20 points 🥈
Alex  - 10 points 🥉
Sam  - 10 points
```

**Monthly:**
```
Monthly leaders board 🏆

Jane Runner  - 120 points 🥇
@speedy  - 90 points 🥈
Alex  - 40 points 🥉
Sam  - 20 points
```

- Each line is `{name}  - {points} points {medal}` (two spaces before the hyphen). Medals 🥇🥈🥉 are shown for ranks 1–3 only; ranks 4+ have no trailing emoji.
- All participants with points in the period are listed (not truncated), ranked high→low.
- Display name preference: `display_name` if present, else `@username`, else `user {id}`.
- If there are **no entries** in the range, post a friendly empty-state message under the same header (`Weekly leaders board 🏆\n\nNo runs logged this week yet.` / `Monthly leaders board 🏆\n\nNo runs logged this month yet.`); it still posts so the group knows the bot is alive.

### Pairs Aggregation (`LeaderboardService.aggregate_pairs()`)

The coach pairs up chat members (see `PAIRS` in Section 8); each pair competes on the **combined** weekly points of its two members.

1. Call `aggregate(start, end)` **verbatim** — the same `read_rows_in_range()` data path, the same `SEASON_START_DATE` cutoff, the same **already-stored** point values. There is **no re-implementation of the point math and no pair multiplier**: running still yields more (15 / 10 / 7.5) than the flat 5 for walking/cycling/strength, and `streak_bonus` rows count as normal points.
2. Index the resulting `LeaderboardEntry` list by `telegram_user_id`, then, for each configured `(member_a, member_b)` tuple, sum the two members' `points` into a `PairEntry`. A member with **no rows** in the window contributes **0** — the pair is never skipped and nothing crashes.
3. **Labels** reuse `LeaderboardEntry.label()` (name → `@username` → `user {id}`), so a member without a `telegram_username` (e.g. *Elena*) still renders by display name. If a member has no rows at all in the window (so no name in the `Log`), the label falls back to their `Plans` `@username` when available, else `user {id}`; the `Plans` read is only attempted when some member is missing and its failure is logged, never raised.
4. Sort by combined points **descending**, tie-broken by the rendered pair label lowercased (deterministic ordering within ties).
5. Render with the **same** `_format_ranking()` used by the individual boards, so the `{label}  - {points} points {medal}` layout, the `format_points()` trimming (`140`, `7.5`) and the **"1224" standard competition ranking** (tied pairs share a rank/medal, the next rank is skipped) are identical by construction. `PairEntry.label()` joins the two member labels in the **configured order** with `` ; `` (space, semicolon, space).

Both `LeaderboardEntry` and `PairEntry` satisfy the small `_Rankable` protocol (`points` + `label()`), which is how one renderer serves both boards without duplicated ranking logic.

**Pairs:**
```
Weekly pairs leaders board 🏆

ArtLike_ ; MY  - 140 points 🥇
. ; Anastasia S  - 115 points 🥈
Матвѣй ; Marfa Sh  - 110 points 🥉
AB ; Elena  - 70 points
```

An empty `PAIRS` yields no entries and the job is not registered at all (nothing is posted). On demand, the coach-only **`/pairs`** command renders the same board for the **current** (in-progress) Mon–Sun week via `current_week_bounds()`.

---

## 8. Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | yes | Bot token from BotFather; authenticates the PTB client. |
| `ANTHROPIC_API_KEY` | yes | API key for the Anthropic (Claude) vision calls. |
| `ANTHROPIC_MODEL` | no | Claude model id (default e.g. `claude-3-5-sonnet-latest`). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | yes | **Full JSON** of the Google service account, as a single-line string (see Section 9). |
| `GOOGLE_SHEET_ID` | yes | Spreadsheet ID of the target Google Sheet. |
| `TARGET_CHAT_ID` | yes | Telegram chat/group id where leaderboards are posted (e.g. `-1001234567890`). |
| `TIMEZONE` | no | IANA timezone; default `Europe/Nicosia`. Used by scheduler & date logic. |
| `MIN_CONFIDENCE` | no | Float threshold (default `0.6`) below which vision verdicts are ignored. |
| `POINTS_PER_RUN` | no | Legacy gate value (default `10`). Under the plan-based model this no longer sets the per-workout points — it only ensures `running` is an awardable activity type; actual points come from `workout_points()` (see Section 5). |
| `SEASON_START_DATE` | no | ISO date `YYYY-MM-DD` (default `2026-07-12`). Points and the leaderboard count **only** submissions dated **on or after** this date; earlier submissions are ignored so the season restarts everyone at zero without deleting registrations or coach-assigned plans (see Section 5). Parsed into `Settings.season_start_date` (a `datetime.date`); an invalid value fails fast at startup. |
| `LATE_SUBMISSION_GRACE_UNTIL_HOUR` | no | Integer hour `0`–`23` (default `9`). The **Monday** hour (local `TIMEZONE`) until which a workout dated in the **just-finished** Mon–Sun week is still accepted and scored — the default `9` matches the Mon 09:00/09:05 leaderboards. The row keeps its **real** `workout_date`, so it counts toward the week being reported (see Section 5). Set to `0` to **disable** the grace period (strict current-week-only). Parsed into `Settings.late_submission_grace_until_hour`; a non-integer or out-of-range value raises `ConfigError` at startup, like `SEASON_START_DATE`. |
| `COACH_IDS` | no | Comma-separated Telegram user IDs (e.g. `123,456`) allowed to set up workouts/plans (run `/setplan`). Only coaches can set plans — regular users cannot set up their own workouts. Blank/unset → empty set (no coaches; nobody can set plans). Whitespace/blank entries are ignored; non-integer entries are **skipped with a logged warning** (never a boot failure). Exposed via `Settings.coach_ids` and the `Settings.is_coach(user_id)` helper. |
| `PAIRS` | no | Competition pairs for the weekly **pairs** leaderboard (Section 7). Pairs separated by `,`, the two Telegram user IDs within a pair joined by `+` — e.g. `123+456,789+1011`. **Unset** → the coach's built-in defaults (`config.DEFAULT_PAIRS`: `5025515480+572559211`, `6599040404+6108222286`, `1406051646+6572975237`, `1274840834+871410038`). **Explicitly empty** (`PAIRS=`) → an empty list, which **disables** the feature (job not registered, nothing posted, logged at INFO). **Malformed** — an entry that isn't exactly two `+`-separated integers — raises `ConfigError` and **fails fast** at startup, like a bad `SEASON_START_DATE`. Parsed by `_parse_pairs()` into `Settings.pairs: list[tuple[int, int]]`, preserving the configured Member A → Member B order used in the rendered label. |
| `LOG_LEVEL` | no | Logging verbosity (default `INFO`). |

**Validation:** [`bot/config.py`](bot/config.py) fails fast at startup if any required variable is missing or malformed (e.g. `GOOGLE_SERVICE_ACCOUNT_JSON` not valid JSON, `TARGET_CHAT_ID` not an int).

### Slash Commands

| Command | Description |
|---------|-------------|
| `/setplan @user N` | **Coach-only** (caller in `COACH_IDS`). Set a member's weekly plan (`N` in **2–6**) by `@username` (resolved via the `Plans` username directory or a `text_mention` entity) or by **replying** to their message + `/setplan N`. The plan is parsed from the **last** integer token so both `@user 4` and (reply) `4` work; validated to 2–6. Upserts the target's `Plans` row (preserving streak) and replies naming who was set. **Regular users cannot set up their own workouts:** any non-coach caller is rejected early with `Only your coach can set up workouts for you.` An unresolvable `@username` gets `Couldn't find @user. Ask them to post once (or use /whoami by replying to their message) so I can learn their ID.` |
| `/myplan` | Reply with the caller's own plan + streak (defaults to plan 3 / streak 0 if unset). |
| `/myplan @user` | **Coach-only.** View another member's plan + streak by `@username` or by **replying** to their message. Shows defaults (plan 3 / streak 0) with a `(no plan set yet, using default 3)` note if they have no row. Same permission/not-found messages as coach `/setplan`. |
| `/whoami` | Reply with the caller's Telegram id + name (id in `<code>` monospace for easy copy). Used as a **reply** to another user's message, reports THAT user's id + name instead — the primary way coaches discover member IDs (for `COACH_IDS` and username resolution). |
| `/pairs` | **Coach-only** (same `Settings.is_coach()` check as `/setplan`; non-coaches get `Only a coach can view the pairs leaderboard.`). Replies with the **current** week's pairs board via `current_week_bounds()`, reusing `aggregate_pairs()` + `format_pairs()` — the same code the Monday 09:00 job uses. The optional argument **`last`** (aliases `prev`/`previous`, case-insensitive) switches to `previous_week_bounds()` — the exact window the scheduled Monday 09:00 board reports — and appends the window (e.g. `(2026-08-03 – 2026-08-09)`) so a re-posted previous-week board can't be mistaken for the current one; any other argument falls back to the current week. Useful after a late submission is accepted under the grace period (Section 5), since such a row keeps its real `workout_date` and belongs to the previous week. Replies `No pairs configured (the PAIRS setting is empty).` when `PAIRS` is empty. Registered in [`bot/main.py`](bot/main.py) but intentionally **omitted from `setMyCommands`** (unadvertised, like `/setplan`). |
| `/status` | Consolidated health report (Telegram, Anthropic, Google Sheets, target chat, timezone). |
| `/testsheet` | Verify Google Sheets connectivity and Editor access. |
| `/chatid` | Reply with the current chat's ID for `TARGET_CHAT_ID`. |

---

## 9. Railway Deployment Notes

### Service type: **Worker**

Run Bot uses **long-polling**, not webhooks, so it needs a persistent worker process (no HTTP port required).

### Start configuration

**`railway.toml`:**
```toml
[build]
builder = "NIXPACKS"

[deploy]
startCommand = "python -m bot.main"
restartPolicyType = "ON_FAILURE"
restartPolicyMaxRetries = 10
```

**`Procfile` (fallback):**
```
worker: python -m bot.main
```

### Google service-account JSON via env var

- Store the entire service-account JSON in `GOOGLE_SERVICE_ACCOUNT_JSON` as a single string (Railway variables support multi-line values; JSON is fine).
- At startup, [`bot/services/sheets.py`](bot/services/sheets.py) parses it with `json.loads(...)` and builds credentials via `google.oauth2.service_account.Credentials.from_service_account_info(...)` with scopes:
  - `https://www.googleapis.com/auth/spreadsheets`
- **No JSON file is written to disk**, avoiding secret leakage in the image.
- The target Sheet must be **shared** with the service account's `client_email` (Editor access).

### Keeping long-polling alive

- The process runs `application.run_polling()` (PTB manages its own event loop, reconnection, and backoff on network errors).
- `restartPolicyType = "ON_FAILURE"` restarts the worker if the process exits unexpectedly.
- Because the Sheet is the source of truth and the scheduler uses `misfire_grace_time`/`coalesce`, restarts are safe: no data loss and no duplicated leaderboard posts within the grace window.
- Timezone data: include `tzdata` in `requirements.txt` so `ZoneInfo("Europe/Nicosia")` resolves in the container.

### requirements.txt (minimum)

```
python-telegram-bot>=21,<22
APScheduler>=3.10
gspread>=6
google-auth>=2
anthropic>=0.30
pydantic>=2
tzdata>=2024.1
```

---

## 10. Edge Cases & Error Handling

| Case | Handling |
|------|----------|
| **Duplicate submission** (same user + same image hash) | Rejected silently. Dedup lookup runs **before** the (costly) vision call; a second race-safe check runs before append. |
| **Non-photo messages** | Ignored by handler filter (`filters.PHOTO`). |
| **Ordinary photo — nature/selfie/meme/food, or another app (Strava, Nike Run Club, Apple Fitness)** | `is_garmin=false` → **silently ignored** (log only, NO reply), so sharing normal photos never spams the group. |
| **Garmin/WHOOP screenshot but not a completed workout / unsupported activity / planned only** | No points, and the bot replies `⚠️ Couldn't confirm a completed workout in this screenshot — no points awarded. Please send the workout summary screenshot from Garmin or WHOOP.` |
| **Garmin achievements/badges screen or WHOOP daily overview (Strain/Recovery/Sleep/Health Monitor/coach card)** | `is_achievement=true` → not eligible; the bot replies `⚠️ This looks like a summary/achievements screen, not a completed workout. Please send the workout summary screenshot from Garmin or WHOOP.` and awards nothing. |
| **WHOOP workout with no distance/pace/map** | Normal for WHOOP — accepted on the `ACTIVITY STRAIN` + `DURATION H:MM:SS` + `ZONE n … BPM` markers; scored exactly like Garmin. |
| **Workout dated in the just-finished week, submitted Monday before `LATE_SUBMISSION_GRACE_UNTIL_HOUR`** | **Accepted and scored** (grace period, Section 5). The row keeps its real `workout_date`, and running points count it against that previous week. |
| **Workout older than the accepted window** (e.g. previous week submitted Mon 09:30 or later, or on any other day) | No log, no points; the bot replies `⚠️ This workout is dated {date}, which is outside the week we're currently counting. Points can only be added for the current week.` |
| **Low confidence** (`< MIN_CONFIDENCE`) | **Silently ignored** (log only): a barely-recognizable image is not a reliable basis for telling someone their screenshot was wrong. |
| **Claude JSON parse failure** | Fallback substring extraction; if still invalid → ignore + warning log. |
| **Claude API error / timeout / rate limit** | Optional single retry with backoff; on final failure → ignore + warning log. No user-facing error to avoid group spam. |
| **Malformed / corrupt image bytes** | If download or base64 encoding fails → ignore + warning log. |
| **Missing `workout_date`** (WHOOP shows only a time-of-day range) | The handler substitutes the **submission/message date** (message timestamp in `TIMEZONE`) before the eligibility gate, so the photo is still processed and no empty date is written. |
| **Large integer IDs precision** | Written as text to the Sheet; read back and parsed as int. |
| **Google Sheets write failure** | Log ERROR (visible in Railway logs) and reply `⚠️ Couldn't save this workout just now — please send the screenshot again in a few minutes.` Retries with backoff run before final failure. |
| **Google Sheets read failure during leaderboard** | Log error; post a graceful "leaderboard unavailable, will retry" message or skip; scheduler will fire again next period. |
| **Restart near scheduled fire time** | `misfire_grace_time=3600` + `coalesce=True` ensure at most one leaderboard post. |
| **User with no username** | `telegram_username` empty; display falls back to full name, then `user {id}`. |
| **Bot lacks send permission in group** | Log error; cannot post (operational fix: grant permissions). |
| **Blocking SDK calls on event loop** | All gspread/anthropic calls wrapped in `asyncio.to_thread(...)`. |

---

## Appendix: End-to-End Photo Sequence

```mermaid
sequenceDiagram
    participant U as Telegram User
    participant B as Run Bot (PTB)
    participant S as Google Sheet
    participant C as Claude Vision

    U->>B: sends photo in group
    B->>B: download bytes, compute image_hash
    B->>S: dedup lookup (user_id, image_hash)
    alt duplicate
        B-->>U: (silent, no reply)
    else new
        B->>C: image + prompt (return JSON only)
        C-->>B: JSON verdict
        B->>B: parse + validate (schema, confidence)
        alt not a Garmin/WHOOP screenshot / low confidence / parse fail
            B-->>U: (silent, no reply — ordinary photos are ignored)
        else eligible + workout_date in current Mon–Sun week
            B->>S: append_row (10 pts, running, ...)
            B->>B: INFO log (after confirmed write)
            B-->>U: ✅ Nice run, {name}! +{points} points.
        else eligible, previous week, Monday before the grace cutoff
            B->>S: append row (real workout_date, previous week)
            B-->>U: ✅ Nice run, {name}! +{points} points.
        else eligible but outside the accepted window
            B-->>U: ⚠️ This workout is dated {date}, which is outside the week we're currently counting.
        end
    end