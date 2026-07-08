# Run Bot — Architecture & Blueprint

> **Status:** Design blueprint (implementation-ready). No application code is included here.
> **Purpose:** A Python Telegram bot that passively monitors a running group, validates Garmin Connect running screenshots via Claude vision, awards points, logs to Google Sheets, and posts weekly/monthly leaderboards.

---

## 1. High-Level Overview

**Run Bot** is deployed as a **Railway worker service** running a single long-lived async Python process. It:

1. Joins a Telegram group and passively listens to all messages.
2. On any **photo** message, downloads the image, hashes the raw bytes (dedup), and sends it to **Claude vision**.
3. Claude returns a **strict JSON verdict** (is it a Garmin running screenshot, completed, with a workout date, etc.).
4. The bot applies **plan-based points/date-window logic**: if the workout date falls in the **current Mon–Sun week** (Europe/Nicosia) → award points based on the user's **weekly plan** (see Section 5), log the run to Google Sheets (row written first, then an INFO log line), and reply in chat with `✅ Nice run, {name}! +{points} points.`. Otherwise → silently ignore.
5. An **APScheduler** (AsyncIOScheduler, timezone `Europe/Nicosia`) runs on the same event loop and posts a **weekly leaderboard** (Monday 09:00) and a **monthly leaderboard** (1st of month 09:00). The weekly job also performs a **streak rollover** (awarding streak bonuses) just before posting the board.
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
    VER --> DEC{Decision:<br/>Garmin + running +<br/>completed + in current week?}
    DEC -->|no / low confidence / parse fail| IGN2[Silently ignore]
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
│   ├── models.py                # Dataclasses/Pydantic models: VisionVerdict, WorkoutLogRow, LeaderboardEntry
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
| [`bot/models.py`](bot/models.py) | Typed data models: `VisionVerdict`, `WorkoutLogRow`, `LeaderboardEntry`. |
| [`bot/handlers/photo.py`](bot/handlers/photo.py) | End-to-end photo pipeline orchestration; writes to the Sheet first, then replies `✅ Nice run, {name}! +{points} points.`. |
| [`bot/services/vision.py`](bot/services/vision.py) | Call Claude vision; enforce strict JSON schema; return validated `VisionVerdict`. |
| [`bot/services/sheets.py`](bot/services/sheets.py) | All Google Sheets I/O: dedup lookup, append row, read range for aggregation. |
| [`bot/services/scheduler.py`](bot/services/scheduler.py) | Configure & start `AsyncIOScheduler` cron triggers on the PTB loop. |
| [`bot/services/leaderboard.py`](bot/services/leaderboard.py) | Aggregate points per user for a date range; format weekly/monthly messages. |
| [`bot/utils/dates.py`](bot/utils/dates.py) | Compute current/previous Mon–Sun week and previous calendar month in Europe/Nicosia. |
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
| F | `activity_type` | string | Lowercase enum: `running` for workouts, or `streak_bonus` for weekly streak-bonus rows. |
| G | `points` | integer | Points awarded (plan-based per-workout value, or the streak bonus for `streak_bonus` rows). |
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
| C | `plan` | integer | Workouts/week target, clamped to `[2, 7]`. Blank/invalid → default `3`. |
| D | `streak` | integer | Consecutive completed weeks. Blank/invalid → `0`. |
| E | `updated_at` | string (ISO 8601) | UTC time the row was last written. |

**Upsert key:** `telegram_user_id`. `/setplan` updates the row if present (preserving `streak`), else appends a new one. The Monday rollover updates `streak` (preserving `plan`/`username`).

---

## 4. Claude Vision Contract

### Approach

- Use the `anthropic` SDK, model configured via env `ANTHROPIC_MODEL` (e.g. `claude-3-5-sonnet-latest` or newer vision-capable model).
- Send **one user message** containing:
  1. An `image` content block (base64 of the downloaded bytes, correct `media_type`).
  2. A `text` content block with the instruction to analyze and return **only** JSON.
- Use a **system prompt** that pins the role, the strict JSON schema, and the "return JSON only, no prose" rule.
- Set a low `temperature` (e.g. `0`) and a modest `max_tokens`.

### System Prompt (verbatim intent)

```
You are an image verification assistant for a running club.
You will be shown a single screenshot. Determine whether it is a Garmin Connect
activity screenshot for a COMPLETED (not planned/scheduled) RUNNING activity,
and extract structured details.

Respond with a SINGLE valid JSON object and NOTHING else — no markdown, no code
fences, no commentary. Use exactly this schema and these keys:

{
  "is_garmin": boolean,        // true only if this is clearly a Garmin Connect screenshot
  "activity_type": string,     // one of: "running", "cycling", "walking", "swimming", "other", "unknown"
  "is_completed": boolean,     // true if the activity is completed with real recorded data (not a planned/scheduled workout)
  "workout_date": string|null, // the activity date in ISO "YYYY-MM-DD" if visible, else null
  "distance": string|null,     // as shown, e.g. "5.02 km", else null
  "duration": string|null,     // as shown, e.g. "00:28:14", else null
  "confidence": number         // 0.0–1.0, your overall confidence in this verdict
}

Rules:
- If it is not a Garmin screenshot, set is_garmin=false and confidence accordingly.
- Never invent a date; if the date is not clearly visible, set workout_date=null.
- Do not add extra keys. Do not omit keys.
```

### User Message (text block)

```
Analyze the attached screenshot and return the JSON verdict per the schema.
```

### JSON Schema (canonical, for validation)

| Field | Type | Required | Constraints |
|-------|------|----------|-------------|
| `is_garmin` | boolean | yes | — |
| `activity_type` | string | yes | enum: `running`, `cycling`, `walking`, `swimming`, `other`, `unknown` |
| `is_completed` | boolean | yes | — |
| `workout_date` | string \| null | yes | ISO `YYYY-MM-DD` when non-null |
| `distance` | string \| null | yes | free text or null |
| `duration` | string \| null | yes | free text or null |
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

A verdict is **eligible for awarding** only if **all** are true:
- `is_garmin == true`
- `activity_type == "running"` (only running is active now)
- `is_completed == true`
- `workout_date` is a valid, non-null ISO date
- `confidence >= MIN_CONFIDENCE`

If eligible, proceed to the date-window/points decision (Section 5). Otherwise IGNORE.

---

## 5. Points & Date-Window Logic

### Definition of "current Mon–Sun week" (Europe/Nicosia)

- All boundary math is done in the **Europe/Nicosia** timezone (`ZoneInfo("Europe/Nicosia")`), then reduced to plain calendar **dates**.
- "Now" = current datetime in Europe/Nicosia. Its **date** is `today`.
- **Week start (Monday):** `week_start = today - timedelta(days=today.weekday())` where `weekday()` is 0=Monday … 6=Sunday.
- **Week end (Sunday):** `week_end = week_start + timedelta(days=6)`.
- A `workout_date` is **in the current week** iff `week_start <= workout_date <= week_end` (inclusive, date comparison).

> Because comparison is date-based (not datetime), there is no ambiguity around midnight or DST for the eligibility check; the Europe/Nicosia timezone is only used to determine what "today" is.

### Plan-Based Points Model

Each user has a weekly **plan** — the number of workouts/week they aim for — stored in the `Plans` worksheet. Plans are set with `/setplan N` and clamped to `[MIN_PLAN, MAX_PLAN]` = **2–7**; the default is **3** for users who never set one.

Constants live in [`bot/utils/points.py`](bot/utils/points.py):

| Constant | Value | Meaning |
|----------|-------|---------|
| `STANDARD_WORKOUTS_PER_WEEK` | 3 | Reference plan. |
| `STANDARD_POINTS_PER_WEEK` | 30 | Points for completing the plan. |
| `MIN_PLAN` / `MAX_PLAN` | 2 / 7 | Allowed plan range. |
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
return round(pts)                       # Python banker's rounding
```

`workouts_this_week_so_far` is how many **running** rows the user already has in the current week BEFORE the new one (streak_bonus rows and other users are excluded). Completing exactly the plan yields ~`STANDARD_POINTS_PER_WEEK` (30) points for the week; extra workouts earn 50% of the base rate. Plan changes apply **going forward only** — already-logged rows keep their points.

**Sample per-workout values** (rounded):

| Plan | Within-plan | Overachievement |
|------|-------------|-----------------|
| 2 | 15 | 8 (round(7.5)) |
| 3 | 10 | 5 |
| 4 | 8 (round(7.5)) | 4 |
| 5 | 6 | 3 |
| 7 | 4 | 2 |

### Decision Pseudocode

```
function decide_and_process(message, verdict, image_hash):
    # eligibility (Section 4)
    if not (verdict.is_garmin and verdict.activity_type == "running"
            and verdict.is_completed and verdict.workout_date is not None
            and verdict.confidence >= MIN_CONFIDENCE):
        return IGNORE   # silent

    if ACTIVITY_POINTS.get(verdict.activity_type, 0) == 0:
        return IGNORE   # silent (gate: running only)

    tz = ZoneInfo("Europe/Nicosia")
    today = datetime.now(tz).date()
    week_start = today - timedelta(days=today.weekday())   # Monday
    week_end = week_start + timedelta(days=6)              # Sunday
    wdate = date.fromisoformat(verdict.workout_date)

    if not (week_start <= wdate <= week_end):
        return IGNORE   # older than current week → silent, no log, no reply

    # dedup (Section 10) already checked BEFORE calling vision, but re-check race:
    if sheets.exists(user_id=message.from_user.id, image_hash=image_hash):
        return IGNORE   # silent

    plan = sheets.get_plan(user_id)                        # default 3
    so_far = sheets.count_user_workouts_in_week(user_id, week_start, week_end)
    points = workout_points(plan, so_far)                  # plan-based value

    row = build_log_row(message, verdict, points, image_hash)
    sheets.append_row(row)                     # real-time log (write-first)
    logger.info("Logged workout: user=%s date=%s points=%s", ...)
    message.reply_text(f"✅ Nice run, {name}! +{points} points.")  # after write
    return AWARDED
```


**Write-first, then reply:** on success the row is written to the Sheet and an INFO log line `Logged workout: user=... date=... points=<computed>` is emitted; **only after** the confirmed write does the bot reply in chat with `✅ Nice run, {name}! +{points} points.`. A failed reply is logged but never undoes the saved row. Failures/ignored images remain silent. (Weekly/monthly leaderboards are still posted to the group.)

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
| Weekly leaderboard | `CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=tz)` | Monday 09:00 | Run the **streak rollover** (award `streak_bonus` rows), then post ranked totals for the **previous** Mon–Sun week. |
| Monthly leaderboard | `CronTrigger(day=1, hour=9, minute=0, timezone=tz)` | 1st of month 09:00 | Post ranked totals for the **previous** full calendar month. |

### Setup Pseudocode ([`bot/services/scheduler.py`](bot/services/scheduler.py))

```
def build_scheduler(bot, sheets, leaderboard, target_chat_id, tz):
    scheduler = AsyncIOScheduler(timezone=tz)

    scheduler.add_job(
        run_weekly_leaderboard,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=tz),
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
2. Filter rows where `workout_date` (column E) falls in `[range_start, range_end]` (inclusive dates).
3. Group by `telegram_user_id`; sum `points` for **all** rows in range **regardless of `activity_type`** (so `running` workout points **and** `streak_bonus` points both count); keep the most recent `display_name`/`telegram_username` for that user id.
4. Sort descending by total points; tie-break by `display_name` alphabetically.

```
function aggregate(rows, range_start, range_end):
    totals = {}   # user_id -> {points, display_name, username}
    for r in rows:
        wdate = date.fromisoformat(r.workout_date)
        if range_start <= wdate <= range_end:
            t = totals.setdefault(r.telegram_user_id,
                                  {"points": 0, "display_name": r.display_name,
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
| `LOG_LEVEL` | no | Logging verbosity (default `INFO`). |

**Validation:** [`bot/config.py`](bot/config.py) fails fast at startup if any required variable is missing or malformed (e.g. `GOOGLE_SERVICE_ACCOUNT_JSON` not valid JSON, `TARGET_CHAT_ID` not an int).

### Slash Commands

| Command | Description |
|---------|-------------|
| `/setplan N` | Set the caller's weekly plan to `N` workouts/week (`N` in **2–7**). Upserts the user's `Plans` row (preserving streak) and replies with the per-workout point value. Invalid/out-of-range/missing `N` → a short usage message. Works in groups; attributed to the poster. |
| `/myplan` | Reply with the caller's current plan + streak (defaults to plan 3 / streak 0 if unset). |
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
| **Photo but not Garmin / not running / planned only** | Vision verdict fails eligibility → silent ignore. |
| **Workout older than current week** | Silent ignore (no log, no reply). |
| **Low confidence** (`< MIN_CONFIDENCE`) | Treated as ignore. |
| **Claude JSON parse failure** | Fallback substring extraction; if still invalid → ignore + warning log. |
| **Claude API error / timeout / rate limit** | Optional single retry with backoff; on final failure → ignore + warning log. No user-facing error to avoid group spam. |
| **Malformed / corrupt image bytes** | If download or base64 encoding fails → ignore + warning log. |
| **Missing `workout_date`** | Not eligible → ignore. |
| **Large integer IDs precision** | Written as text to the Sheet; read back and parsed as int. |
| **Google Sheets write failure** | Log ERROR (visible in Railway logs); stay silent (no chat message). Retries with backoff run before final failure. |
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
        alt not eligible / low confidence / parse fail
            B-->>U: (silent, no reply)
        else eligible + workout_date in current Mon–Sun week
            B->>S: append_row (10 pts, running, ...)
            B->>B: INFO log (after confirmed write)
            B-->>U: ✅ Nice run, {name}! +{points} points.
        else eligible but older than current week
            B-->>U: (silent, no reply)
        end
    end