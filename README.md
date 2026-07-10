# 🏃 Run Bot

A Python Telegram bot for running clubs. It passively watches a group chat and,
whenever someone posts a **photo**, it:

1. Downloads the image and hashes it (to prevent duplicate submissions).
2. Sends it to **Claude vision** to check whether it's a **completed Garmin
   Connect screenshot**, classify the **activity** (running / walking / cycling /
   strength) and extract the workout date + duration.
3. If it's a valid activity **dated within the current Mon–Sun week** (Cyprus
   time), it awards points and logs to a **Google Sheet** (the row is written
   first), then replies in chat:
   - **Running** → **plan-based points** (see [Points & plans](#points--plans)):
     `✅ Nice run, {name}! +{points} points.`
   - **Walking / cycling / strength** → a flat **5 points** once a minimum
     duration is met (walking ≥ 40 min, cycling ≥ 60 min, strength/stretch ≥ 15
     min): `✅ Nice {walk|ride|strength session}, {name}! +5 points.`. Below the
     minimum it replies with a short warning and awards nothing. These are
     **separate bonus points** — they don't affect the running plan or streak.

   Otherwise (old week, duplicate, non-Garmin, not completed, unrecognized) it
   silently ignores the photo.
4. Automatically posts a **weekly leaderboard** every Monday at 09:00 and a
   **monthly leaderboard** on the 1st of each month at 09:00 (Europe/Nicosia).
   The weekly job also awards **streak bonuses** just before posting the board.

Google Sheets is the **single source of truth**, so restarts never lose data.

> Architecture details live in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

---

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [Step 1 — Create the Telegram bot](#step-1--create-the-telegram-bot)
3. [Step 2 — Get the group chat ID](#step-2--get-the-group-chat-id)
4. [Step 3 — Get an Anthropic API key](#step-3--get-an-anthropic-api-key)
5. [Step 4 — Set up Google Sheets + service account](#step-4--set-up-google-sheets--service-account)
6. [Step 5 — Run locally](#step-5--run-locally)
7. [Step 6 — Deploy to Railway](#step-6--deploy-to-railway)
8. [How the leaderboards work](#how-the-leaderboards-work)
9. [Environment variables reference](#environment-variables-reference)
10. [Troubleshooting](#troubleshooting)

---

## Prerequisites

- Python **3.11+** (only needed for local runs; Railway handles this in the cloud).
- A Telegram account, a Google account, and an Anthropic account.
- The Google Sheet you want to use as the log/source of truth.

---

## Step 1 — Create the Telegram bot

1. In Telegram, open a chat with **[@BotFather](https://t.me/BotFather)**.
2. Send `/newbot` and follow the prompts:
   - Choose a **name** (display name) and a **username** (must end in `bot`).
3. BotFather replies with your **bot token** — a string like
   `123456789:AAE...`. **Copy it**; this is your `TELEGRAM_BOT_TOKEN`.
4. **Disable privacy mode** so the bot can read all group messages (including
   photos):
   - Still in @BotFather, send `/setprivacy`.
   - Select your bot.
   - Choose **Disable**.
   - You should see: *"Privacy mode is disabled..."*.
   > ⚠️ If privacy mode stays **enabled**, the bot will not receive normal group
   > messages/photos and will never award points.
5. **Add the bot to your group:**
   - Open your running group → group settings → **Add members** → search your
     bot's username → add it.
   - It's simplest to make the bot an **admin** of the group so it can reliably
     read messages and post leaderboards. At minimum it must be able to **send
     messages**.

---

## Step 2 — Get the group chat ID

You need the numeric chat ID (usually a large **negative** number like
`-1001234567890`) for `TARGET_CHAT_ID`.

**Easiest method — the built-in `/chatid` command:**

1. Add **this bot** to your group (Step 1.5).
2. In the group, type **`/chatid`**. The bot replies with the group's chat ID.
3. Copy the negative number (e.g. `-1001234567890`) into `TARGET_CHAT_ID`.

> `TARGET_CHAT_ID` is **optional at first**: you can boot the bot without it just
> to run `/chatid`, then set the variable and redeploy. While it's unset, photo
> handling and `/chatid` still work; only the scheduled leaderboards are skipped
> (the bot logs a clear warning and does not crash).

**Alternative (using your own bot's API):**

1. Send any message in the group, then open in a browser:
   `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
2. Find the `"chat":{"id":...}` for your group in the JSON.

> Supergroup IDs typically start with `-100`. Keep the minus sign.

---

## Step 3 — Get an Anthropic API key

1. Go to **[console.anthropic.com](https://console.anthropic.com)** and sign in
   / sign up.
2. Add billing if required (vision calls consume credits).
3. Open **API Keys** → **Create Key** → copy the key
   (starts with `sk-ant-...`). This is your `ANTHROPIC_API_KEY`.
4. The default model is `claude-3-5-sonnet-20241022` (a valid, widely-available
   Claude 3.5 Sonnet vision model). You can override it via `ANTHROPIC_MODEL` if
   you prefer a different model.
   > If you get a 404 `not_found_error` for the model, set `ANTHROPIC_MODEL` to a
   > model ID your Anthropic account has access to (see your Anthropic Console).

---

## Step 4 — Set up Google Sheets + service account

The bot writes to and reads from one Google Sheet using a **service account**
(a robot Google identity). No key file is stored on disk — you paste the whole
JSON key into an environment variable.

### 4a. Create a Google Cloud project & enable APIs

1. Go to **[console.cloud.google.com](https://console.cloud.google.com)**.
2. Create a new project (top bar → project dropdown → **New Project**).
3. Enable the required APIs (menu → **APIs & Services** → **Library**):
   - **Google Sheets API** → Enable.
   - **Google Drive API** → Enable. *(Needed by gspread to open the sheet.)*

### 4b. Create the service account & key

1. Menu → **APIs & Services** → **Credentials**.
2. **Create Credentials** → **Service account**.
   - Give it a name (e.g. `run-bot`) → **Create and continue** → **Done**.
3. Click the new service account → **Keys** tab → **Add Key** → **Create new
   key** → **JSON** → **Create**.
4. A `.json` file downloads. Open it in a text editor — you'll paste its
   **entire contents** into the `GOOGLE_SERVICE_ACCOUNT_JSON` variable later.
5. Note the service account's **email** (looks like
   `run-bot@your-project.iam.gserviceaccount.com`) — you'll share the sheet with
   it next.

### 4c. Create and share the target Google Sheet

1. Create (or pick) a Google Sheet at
   **[sheets.new](https://sheets.new)**.
2. **Get the Sheet ID** from the URL:
   ```
   https://docs.google.com/spreadsheets/d/1AbCdEf...XYZ/edit#gid=0
                                          └──────┬──────┘
                                        this is GOOGLE_SHEET_ID
   ```
   Copy that middle part → this is your `GOOGLE_SHEET_ID`.
3. **Share** the sheet with the service account:
   - Click **Share** → paste the service account email
     (`run-bot@your-project.iam.gserviceaccount.com`) → set role to **Editor**
     → **Send**.
   > The bot auto-creates the worksheets **`Log`** and **`Plans`** with the
   > correct header rows on first run — you don't need to add tabs or headers
   > yourself.

### 4d. Prepare the JSON for the env var

- The value of `GOOGLE_SERVICE_ACCOUNT_JSON` must be the **full JSON** of the
  key file.
- **Local `.env`:** paste it on a **single line** (keep the `\n` escapes inside
  `private_key` exactly as they are in the file).
- **Railway:** you can paste it in the variable value field; Railway accepts
  multi-line values. The bot parses it with `json.loads`.

---

## Step 5 — Run locally

```bash
# 1. Clone / open this project, then create a virtual environment
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Create your .env from the example and fill in real values
cp .env.example .env
#   → edit .env and paste your token, keys, sheet id, chat id, and the
#     full service-account JSON on one line.

# 4. Load the env vars and run the bot
#    (option A) export them via a tool like `dotenv`, or
#    (option B) export manually, e.g. on macOS/Linux:
set -a; source .env; set +a
python -m bot.main
```

You should see log lines like `Starting Run Bot ...`, `SheetsService
initialized ...`, and `Bot started; scheduler running.` Post a Garmin running
screenshot in the group to test — for a valid, current-week run a new row is
written to the sheet first, an INFO log line `Logged workout: ...` appears in
the Railway logs, and the bot then replies in chat with
`✅ Nice run, {name}! +{points} points.`.

> The `.env` file is git-ignored. Never commit real credentials.

---

## Step 6 — Deploy to Railway

Run Bot uses **long-polling**, so it runs as a **worker** (no HTTP port).

1. Go to **[railway.app](https://railway.app)** → **New Project** → **Deploy
   from GitHub repo** (push this repo to GitHub first), or use the Railway CLI.
2. Railway auto-detects Python (NIXPACKS) and uses the provided
   [`railway.toml`](railway.toml) start command `python -m bot.main`. A
   [`Procfile`](Procfile) (`worker: python -m bot.main`) is included as a
   fallback.
3. Open the service → **Variables** → add **every** required variable:
   - `TELEGRAM_BOT_TOKEN`
   - `ANTHROPIC_API_KEY`
   - `ANTHROPIC_MODEL` *(optional)*
   - `GOOGLE_SERVICE_ACCOUNT_JSON` *(paste the whole JSON key here)*
   - `GOOGLE_SHEET_ID`
   - `TARGET_CHAT_ID` *(optional at first — boot the bot, run `/chatid` to
     discover it, then set it and redeploy)*
   - `TIMEZONE`, `MIN_CONFIDENCE`, `POINTS_PER_RUN`, `LOG_LEVEL` *(optional)*
4. Deploy. Watch the **Deploy Logs** — you should see the same startup lines as
   local. Because it's long-polling, the worker **stays alive** indefinitely.
5. `restartPolicyType = "ON_FAILURE"` (in `railway.toml`) automatically restarts
   the worker if the process ever crashes. Restarts are safe: the Sheet holds
   all data, and the scheduler uses `misfire_grace_time`/`coalesce` so a restart
   near a leaderboard time won't drop or duplicate posts.

> **Tip:** Verify the service is a *worker* (no public domain/port needed). If
> Railway created it expecting a web port, the long-polling worker still runs
> fine — it simply doesn't serve HTTP.

---

## How the leaderboards work

- Times are in **Europe/Nicosia** (Cyprus). Change with `TIMEZONE` if needed.
- **Weekly (Mon 09:00):** first awards **streak bonuses** for the previous week
  (so they show up in the board), then posts totals for the **previous**
  Monday–Sunday week.
- **Monthly (1st 09:00):** posts totals for the **previous** full calendar month.
- Rankings sum each user's points over the range (**including** the
  walking/cycling/strength bonus points and `streak_bonus` points), sorted
  high→low, with 🥇🥈🥉 medals for the top three (ranks 4+ have
  no medal). Users are labelled by full name, else `@username`, else
  `user <id>`. All participants with points are listed.
- If nobody logged a run in the period, the bot still posts a friendly "no runs
  logged" message so the group knows it's alive.

Example output:

```
Weekly leaders board 🏆

Jane Runner  - 30 points 🥇
@speedy  - 20 points 🥈
Alex  - 10 points 🥉
Sam  - 10 points
```

```
Monthly leaders board 🏆

Jane Runner  - 120 points 🥇
@speedy  - 90 points 🥈
Alex  - 40 points 🥉
Sam  - 20 points
```

## Points & plans

Instead of a flat points-per-run, each user has a weekly **plan** — how many
workouts/week they aim for — and points scale to that plan.

- **Set your plan:** `/setplan N` where `N` is between **2 and 6** (five plans:
  2, 3, 4, 5, 6; default is **3** if you never set one). Example: `/setplan 4`.
- **Completing your plan earns exactly 30 points/week.** Each workout up to your
  plan is worth `30 / plan` points — an **exact fraction** (no rounding to whole
  numbers), shown trimmed (e.g. `7.5`, not `8`; `15`, not `15.0`):

  | Plan | Points per workout (within plan) | Beyond plan (50%) |
  |------|:--------------------------------:|:-----------------:|
  | 2 | 15 | 7.5 |
  | 3 | 10 | 5 |
  | 4 | 7.5 | 3.75 |
  | 5 | 6 | 3 |
  | 6 | 5 | 2.5 |

- **Overachievement:** workouts logged **beyond** your plan in the same week
  still count, at **50%** of the base rate (also an exact fraction).
- **Streak bonus:** every Monday the bot checks the previous week. If you
  **completed your plan**, your streak increments; otherwise it resets to 0.
  Consecutive completed weeks award a bonus (added to that week's leaderboard):

  | Consecutive weeks | 1 | 2 | 3 | 4 | 5 | 6 | 7+ |
  |-------------------|---|---|---|---|---|---|----|
  | Bonus points | 0 | 0 | +5 | +10 | +15 | +20 | +20 |

  Streak bonuses are logged as `streak_bonus` rows in the sheet and **count
  toward the leaderboards**. Changing your plan applies **going forward only** —
  already-logged runs keep their points.

### Other activities — walking, cycling & strength

Besides running, three **bonus** activities each earn a flat **5 points** once a
minimum duration is met. They count in the weekly/monthly leaderboards but are
**separate** from the running plan — they do **not** affect your plan progress,
streak, or overachievement (those stay running-only).

| Activity | Minimum duration | Points | Success reply | Below-minimum reply |
|----------|:----------------:|:------:|---------------|---------------------|
| Walking | **40 min** | 5 | `✅ Nice walk, {name}! +5 points.` | `⚠️ Walk is {dur} min — minimum is 40 min to earn points.` |
| Cycling | **60 min** | 5 | `✅ Nice ride, {name}! +5 points.` | `⚠️ Ride is {dur} min — minimum is 60 min to earn points.` |
| Strength/stretch | **15 min** | 5 | `✅ Nice strength session, {name}! +5 points.` | `⚠️ Strength/stretch is {dur} min — minimum is 15 min to earn points.` |

- "Strength" covers **strength training and stretching/yoga/mobility**.
- Below the minimum duration → **no points, not logged**; the bot just replies
  with the short warning above. If Claude can't read a duration for a bonus
  activity, it replies `⚠️ Couldn't read the duration — no points awarded.`
- Same rules as running otherwise: it must be a **Garmin**, **completed**
  activity dated in the **current week**, above the confidence threshold, and
  not a duplicate — otherwise it's silently ignored.

**Eligibility:** a photo is only awarded if Claude confirms it's a **Garmin**,
**completed** activity of a supported type (**running**, **walking**,
**cycling**, or **strength**) with a **valid date** in the **current week** and
confidence ≥ `MIN_CONFIDENCE` (default **0.6**). Bonus activities additionally
require their minimum duration. Everything else is silently ignored.

## Diagnostics

Slash commands help configure plans and verify the bot's integrations at
runtime. They work in any chat (DM the bot or run them in the group) and never
leak secrets — only a concise result is sent to chat.

**Plan commands:**

- **`/setplan N`** — set your weekly plan to `N` workouts/week (`N` between
  **2** and **6**). Replies with the per-workout point value (trimmed, e.g. plan
  4 → `7.5`); invalid or out-of-range input returns a short usage message.
  Attributed to whoever sends it, so it works in the group.
- **`/myplan`** — replies with your current plan and streak (defaults to plan 3,
  streak 0 if you've never set one).

**Coach commands (targeting other members):**

Coaches (Telegram user IDs listed in `COACH_IDS`) can set or view **other**
members' plans. Everyone else can only manage their **own** plan (self-service,
unchanged). If a non-coach tries to target someone else, the bot replies
`Only a coach can set or view another member's plan.` and does nothing.

- **`/setplan @username N`** *(coach)* — set another member's plan. The plan is
  parsed from the last integer, so `/setplan @jane 4` works.
- **`/setplan N`** as a **reply** to a member's message *(coach)* — sets the
  replied-to member's plan.
- **`/myplan @username`** *(coach)* — view another member's plan + streak.
- **`/myplan`** as a **reply** to a member's message *(coach)* — view the
  replied-to member's plan + streak. For a member with no plan yet, defaults
  (plan 3 / streak 0) are shown with a `(no plan set yet, using default 3)` note.

**Finding user IDs & the username directory:**

- **`/whoami`** — replies with your Telegram id and name (id in copy-friendly
  monospace). Use it as a **reply** to a member's message to get **their** id and
  name instead — the easiest way for a coach to learn a member's ID.
- The bot maintains a lightweight **username directory** in the `Plans`
  worksheet: as people post runs (and via `/setplan`/`/myplan`/`/whoami`), it
  records their `@username → id`. That lets `/setplan @username N` and
  `/myplan @username` resolve usernames for anyone the bot has seen. Telegram
  does **not** expose a numeric id from plain `@username` text, so if a member
  hasn't been seen yet the bot replies `Couldn't find @username. Ask them to
  post once (or use /whoami by replying to their message) so I can learn their
  ID.` Text mentions (which carry a real user object) are resolved directly.

**Diagnostic commands:**

- **`/status`** — a consolidated health report across all integrations:
  - **Telegram** — ✅ with the bot's `@username` (via `get_me()`).
  - **Anthropic** — ✅ if `ANTHROPIC_API_KEY` is present and a minimal, cheap
    `messages.create` (`max_tokens=1`) with the configured model succeeds; ❌ on
    an invalid key; ⚠️ on other API errors.
  - **Google Sheets** — same check as `/testsheet` (see below).
  - **Target chat** — shows `TARGET_CHAT_ID` if set, or a ⚠️ note that
    leaderboards are disabled.
  - **Timezone** — the configured `TIMEZONE`.
- **`/testsheet`** — verifies Google Sheets connectivity and **Editor** access:
  authorizes with the service account, opens the spreadsheet by `GOOGLE_SHEET_ID`,
  ensures the `Log` worksheet (auto-creating it — which itself requires Editor
  access — if missing, without appending junk to real data), and reads the
  header row to confirm read access. (The `Plans` worksheet is auto-created at
  startup.) On failure it returns a short hint
  (permission denied → share the sheet with the service-account email; bad JSON
  → check `GOOGLE_SERVICE_ACCOUNT_JSON`; missing/incorrect `GOOGLE_SHEET_ID`).
- **`/chatid`** — replies with the current chat's ID, type, and title so you can
  discover the value for `TARGET_CHAT_ID`.
- **`/whoami`** — replies with your (or, when used as a reply, the replied-to
  user's) Telegram id and name, so coaches can discover member IDs for
  `COACH_IDS` and for username resolution.

---
---

## Environment variables reference

| Variable | Required | Default | Description |
|----------|:--------:|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | ✅ | — | Bot token from @BotFather. |
| `ANTHROPIC_API_KEY` | ✅ | — | Anthropic (Claude) API key. |
| `ANTHROPIC_MODEL` | ❌ | `claude-3-5-sonnet-20241022` | Claude vision model id. |
| `ANTHROPIC_TEMPERATURE` | ❌ | *(omitted)* | Sampling temperature. Leave unset for newer models like `claude-sonnet-5` that reject `temperature`; set e.g. `0` only for older models that support it. |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | ✅ | — | Full service-account JSON (as a string). |
| `GOOGLE_SHEET_ID` | ✅ | — | Target spreadsheet ID (from its URL). |
| `TARGET_CHAT_ID` | ❌ | — | Group chat id for leaderboards (usually negative). Optional at first — discover it with the `/chatid` command; leaderboards are skipped until it's set. |
| `TIMEZONE` | ❌ | `Europe/Nicosia` | IANA timezone for scheduling & dates. |
| `MIN_CONFIDENCE` | ❌ | `0.6` | Min vision confidence to accept a verdict. |
| `POINTS_PER_RUN` | ❌ | `10` | Legacy setting. Under the plan-based model it no longer sets per-run points — it only marks `running` as awardable. Actual points come from each user's plan (set with `/setplan`). |
| `COACH_IDS` | ❌ | *(empty)* | Comma-separated Telegram user IDs (e.g. `123,456`) allowed to set/view OTHER users' plans. Blank/unset → no coaches. Non-integer entries are skipped with a warning. Use `/whoami` (reply to a member) to find IDs. |
| `LOG_LEVEL` | ❌ | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR`. |

See [`.env.example`](.env.example) for a copy-paste template.

---

## Troubleshooting

- **Bot ignores photos in the group:** privacy mode is probably still on — redo
  Step 1.4 (`/setprivacy` → Disable), then remove & re-add the bot to the group.
- **`Configuration error: Missing required environment variable ...`:** a
  required variable isn't set. Check your `.env` / Railway variables.
- **`GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON`:** the pasted key is
  malformed. Re-copy the whole file contents; keep the `\n` escapes in
  `private_key`.
- **Sheets errors (permission / not found):** ensure the Sheet is **shared with
  the service account email** as **Editor**, and both **Sheets API** and
  **Drive API** are enabled in your Google Cloud project.
- **No leaderboard posted:** confirm `TARGET_CHAT_ID` is set (with the minus
  sign) and the bot can send messages in that group. If the startup logs say
  *"TARGET_CHAT_ID not set"*, run `/chatid` in the group, set the variable, and
  redeploy.
- **Valid run not awarded:** it must be dated **within the current Mon–Sun
  week** (Cyprus time) and pass the confidence threshold; older or low-confidence
### Conflict / "terminated by other getUpdates request"

If the logs show `telegram.error.Conflict: terminated by other getUpdates
request`, it means **two instances are polling the same bot token at once**.
Telegram only allows one long-poller per token. To resolve it:

- **Don't run locally while Railway runs** the same token (or vice-versa). Stop
  any local `python -m bot.main` session.
- **In Railway, ensure only ONE deployment/replica is active.** Set replicas =
  **1** and make sure old deployments are **fully stopped** — during a redeploy
  the new deploy can briefly overlap the old one.
- A brief overlap during a redeploy is **transient** and resolves automatically
  once the old deployment stops; the bot logs a concise WARNING and keeps
  running (it does not crash).

The `/status` Anthropic check now uses the **configured** model, so a model
`404 not_found_error` will surface there as `⚠️ model not found — set
ANTHROPIC_MODEL to a valid model`, distinct from an `❌ invalid API key`.

### Anthropic model 404 (`not_found_error`)

If you see `not_found_error` for the model (e.g. an alias your account can't
use), set `ANTHROPIC_MODEL` to a model ID your Anthropic account has access to
(see your Anthropic Console). The default is `claude-3-5-sonnet-20241022`.
  runs are silently ignored by design.