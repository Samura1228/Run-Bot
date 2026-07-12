"""Command handlers.

Contains simple slash-command handlers:

- ``/chatid`` — replies with the current chat's ID so operators can discover the
  value for the ``TARGET_CHAT_ID`` environment variable.
- ``/testsheet`` — verifies Google Sheets connectivity and Editor access.
- ``/status`` — a consolidated health report across Telegram, Anthropic, and
  Google Sheets, plus the configured target chat and timezone.

The commands work in any chat type (private, group, supergroup, channel) and,
like the rest of the codebase, never crash on failure — errors are logged and
swallowed, and only concise, secret-free reasons are ever sent to chat.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape
from typing import Optional

import anthropic
from telegram import Update, User
from telegram.constants import MessageEntityType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.services.sheets import SheetsService, check_sheets
from bot.utils.points import (
    DEFAULT_PLAN,
    MAX_PLAN,
    MIN_PLAN,
    STANDARD_POINTS_PER_WEEK,
    clamp_plan,
    format_points,
)

logger = logging.getLogger(__name__)

_SETPLAN_USAGE = (
    f"Usage: /setplan [N]  or  /setplan @user N  (N {MIN_PLAN}–{MAX_PLAN}). "
    "Coaches can target a user by @username or by replying to them."
)
_COACH_ONLY_MSG = "Only a coach can set or view another member's plan."


async def chatid_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply with the current chat's ID, type, and title.

    Registered with a ``CommandHandler("chatid", chatid_command)``. PTB's
    ``CommandHandler`` also matches the ``/chatid@BotUsername`` form used in
    groups, so no extra handling is needed for that.

    The chat ID is wrapped in Telegram HTML ``<code>`` formatting so it can be
    tapped/copied easily.
    """

    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    # Build the reply. The ID is placed in a <code> block for easy copying.
    lines = [
        f"Chat ID: <code>{chat.id}</code>",
        f"Type: {chat.type}",
    ]
    if chat.title:
        lines.append(f"Title: {escape(chat.title)}")
    lines.append(
        "Use this ID as <code>TARGET_CHAT_ID</code> in your environment "
        "variables."
    )
    reply_text = "\n".join(lines)

    try:
        await message.reply_text(reply_text, parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logger.error("Failed to send /chatid reply: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error handling /chatid: %s", exc)


def _get_settings(context: ContextTypes.DEFAULT_TYPE) -> Optional[Settings]:
    """Return the shared :class:`Settings` stashed in ``bot_data`` by main.

    Returns ``None`` if unavailable (should not happen in normal operation).
    """

    settings = context.application.bot_data.get("settings")
    if isinstance(settings, Settings):
        return settings
    logger.error("Settings not found in bot_data; diagnostics unavailable.")
    return None


def _get_sheets(context: ContextTypes.DEFAULT_TYPE) -> Optional[SheetsService]:
    """Return the shared :class:`SheetsService` stashed in ``bot_data`` by main."""

    sheets = context.application.bot_data.get("sheets")
    if isinstance(sheets, SheetsService):
        return sheets
    logger.error("SheetsService not found in bot_data; plan commands unavailable.")
    return None


def _display_name_for(user: User) -> str:
    """Return a user's display name: full name, else @username, else id."""

    full = " ".join(
        part for part in [user.first_name, user.last_name] if part
    ).strip()
    if full:
        return full
    if user.username:
        return f"@{user.username}"
    return f"user {user.id}"


def _who_label(user_id: int, username: str, display_name: str) -> str:
    """Return a friendly label for a resolved target (name > @username > id)."""

    name = (display_name or "").strip()
    if name:
        return name
    uname = (username or "").strip()
    if uname:
        return f"@{uname}"
    return f"user {user_id}"


def _text_mention_user(message) -> Optional[User]:
    """Return the User from a ``text_mention`` entity, if any.

    A ``text_mention`` entity DOES carry a full :class:`telegram.User` object
    (including the numeric id), so it lets us target users who have no public
    ``@username``. Returns the first such user found, else ``None``.
    """

    for entity in message.entities or []:
        if entity.type == MessageEntityType.TEXT_MENTION and entity.user:
            return entity.user
    return None


def _first_username_arg(args: list[str]) -> Optional[str]:
    """Return the first ``@username`` token (without the ``@``), if present."""

    for token in args:
        stripped = token.strip()
        if stripped.startswith("@") and len(stripped) > 1:
            return stripped[1:]
    return None


def _last_int_arg(args: list[str]) -> Optional[int]:
    """Return the LAST integer token among args, or ``None`` if there is none.

    Parsing the last integer lets both ``/setplan @user 4`` and (reply)
    ``/setplan 4`` work, ignoring a leading ``@username`` token.
    """

    result: Optional[int] = None
    for token in args:
        try:
            result = int(token.strip())
        except ValueError:
            continue
    return result


class _TargetError(Exception):
    """Raised when a coach command target can't be resolved; carries a reply."""

    def __init__(self, reply: str) -> None:
        super().__init__(reply)
        self.reply = reply


async def _resolve_target(
    message,
    caller: User,
    args: list[str],
    sheets: SheetsService,
    settings: Settings,
) -> tuple[int, str, str]:
    """Resolve the (user_id, username, display_name) a plan command targets.

    Priority:
      1. Reply to another user's message → that replied-to user.
      2. First ``@username`` arg (or a ``text_mention`` entity) → resolved id.
      3. Otherwise → the caller themselves (self-service).

    Raises :class:`_TargetError` (with a user-facing ``reply``) when a
    ``@username`` can't be resolved, or when a non-coach tries to target
    someone other than themselves.
    """

    caller_username = (caller.username or "").strip()
    caller_display = _display_name_for(caller)

    # 1) Reply targeting — reliable id + username from the replied-to message.
    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        target_user = reply.from_user
        _ensure_coach(caller, target_user.id, settings)
        return (
            target_user.id,
            (target_user.username or "").strip(),
            _display_name_for(target_user),
        )

    # 2) text_mention entity (carries a full User with id) → prefer it.
    mention_user = _text_mention_user(message)
    if mention_user is not None:
        _ensure_coach(caller, mention_user.id, settings)
        return (
            mention_user.id,
            (mention_user.username or "").strip(),
            _display_name_for(mention_user),
        )

    # 2b) @username text → resolve via the Plans directory.
    username_arg = _first_username_arg(args)
    if username_arg is not None:
        target_id = await sheets.find_user_id_by_username(username_arg)
        if target_id is None:
            raise _TargetError(
                f"Couldn't find @{username_arg}. Ask them to post once (or use "
                "/whoami by replying to their message) so I can learn their ID."
            )
        _ensure_coach(caller, target_id, settings)
        return target_id, username_arg, f"@{username_arg}"

    # 3) Self-service.
    return caller.id, caller_username, caller_display


def _ensure_coach(caller: User, target_id: int, settings: Settings) -> None:
    """Raise :class:`_TargetError` if a non-coach targets another user."""

    if target_id != caller.id and not settings.is_coach(caller.id):
        raise _TargetError(_COACH_ONLY_MSG)


async def whoami_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Report a user's Telegram id + name so coaches can discover IDs.

    If used as a REPLY to someone's message, reports THAT replied-to user's id
    and name (so a coach can learn a member's id by replying to them). Otherwise
    reports the CALLER's own id and name. The id is wrapped in Telegram HTML
    ``<code>`` (like ``/chatid``) for easy copying. Best-effort touches the
    Plans username directory for the reported user.
    """

    message = update.effective_message
    if message is None:
        return
    caller = message.from_user
    if caller is None:
        return

    reply = message.reply_to_message
    if reply is not None and reply.from_user is not None:
        target = reply.from_user
    else:
        target = caller

    name = _display_name_for(target)
    username = target.username or "—"
    lines = [
        f"👤 {escape(name)}",
        f"ID: <code>{target.id}</code>",
        f"Username: @{escape(username)}" if target.username else "Username: —",
    ]

    # Best-effort: keep the username directory fresh for this user.
    sheets = _get_sheets(context)
    if sheets is not None and target.username:
        try:
            await sheets.touch_user(
                target.id, target.username.strip(), _display_name_for(target)
            )
        except Exception as exc:  # pragma: no cover - best-effort
            logger.warning(
                "touch_user failed during /whoami for %s: %s", target.id, exc
            )

    try:
        await message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except TelegramError as exc:
        logger.error("Failed to send /whoami reply: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error handling /whoami: %s", exc)


async def setplan_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Set a weekly plan (workouts/week) via ``/setplan``.

    Forms:
      - ``/setplan N`` → set the caller's own plan (self-service).
      - ``/setplan @username N`` (coach) → set that user's plan.
      - reply to a user's message + ``/setplan N`` (coach) → set their plan.

    The plan is parsed from the LAST integer token (so ``@user 4`` and (reply)
    ``4`` both work) and validated to ``[MIN_PLAN, MAX_PLAN]`` (2–6). Targeting
    another user requires the caller to be a coach; self-service is unchanged.
    On success the target's Plans row is upserted (preserving streak).
    """

    message = update.effective_message
    if message is None:
        return

    # Defensive wrapper: guarantee this command NEVER fails silently. Any
    # unexpected exception (e.g. a raise inside target resolution that is not a
    # _TargetError) is logged AND surfaced to the user with a short message,
    # rather than escaping to the global error handler (which logs but sends
    # nothing to chat). Specific, expected outcomes below still produce their
    # own, more precise replies.
    try:
        caller = message.from_user
        if caller is None:
            return

        sheets = _get_sheets(context)
        if sheets is None:
            await _safe_reply(
                message, "❌ Could not set plan — internal error, see logs."
            )
            return
        settings = _get_settings(context)
        if settings is None:
            await _safe_reply(
                message, "❌ Could not set plan — internal error, see logs."
            )
            return

        args = context.args or []

        # Parse the plan from the LAST integer token; validate 2–6.
        requested = _last_int_arg(args)
        if requested is None or not (MIN_PLAN <= requested <= MAX_PLAN):
            await _safe_reply(message, _SETPLAN_USAGE)
            return

        # Resolve who is being set (self by default; coach targeting via reply /
        # @username / text_mention).
        try:
            target_id, target_username, target_display = await _resolve_target(
                message, caller, args, sheets, settings
            )
        except _TargetError as exc:
            await _safe_reply(message, exc.reply)
            return

        plan = clamp_plan(requested)
        try:
            await sheets.set_plan(target_id, target_username, plan)
        except Exception as exc:
            logger.error("Failed to set plan for user %s: %s", target_id, exc)
            await _safe_reply(
                message, "❌ Could not set plan — please try again later."
            )
            return

        per_workout = format_points(STANDARD_POINTS_PER_WEEK / plan)
        who = _who_label(target_id, target_username, target_display)
        if target_id == caller.id:
            await _safe_reply(
                message,
                f"✅ Plan set: {plan} workouts/week. Points per workout: "
                f"{per_workout} (complete your plan for ~{STANDARD_POINTS_PER_WEEK} "
                "pts/week).",
            )
        else:
            await _safe_reply(
                message,
                f"✅ Plan set for {who}: {plan} workouts/week. "
                f"Points per workout: {per_workout}.",
            )
    except Exception as exc:  # noqa: BLE001 - defensive: never fail silently
        logger.error("Unexpected error handling /setplan: %s", exc, exc_info=exc)
        await _safe_reply(
            message, "⚠️ Something went wrong setting the plan. Try again."
        )


async def myplan_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply with a plan and streak via ``/myplan``.

    Forms:
      - ``/myplan`` → the caller's own plan + streak (self-service).
      - ``/myplan @username`` (coach) → that user's plan + streak.
      - reply to a user's message + ``/myplan`` (coach) → their plan + streak.

    Defaults to plan :data:`DEFAULT_PLAN` (3) / streak 0 if the target has no
    Plans row. Viewing another user requires the caller to be a coach.
    """

    message = update.effective_message
    if message is None:
        return
    caller = message.from_user
    if caller is None:
        return

    sheets = _get_sheets(context)
    if sheets is None:
        await _safe_reply(message, "❌ Could not read plan — internal error, see logs.")
        return
    settings = _get_settings(context)
    if settings is None:
        await _safe_reply(message, "❌ Could not read plan — internal error, see logs.")
        return

    args = context.args or []
    try:
        target_id, target_username, target_display = await _resolve_target(
            message, caller, args, sheets, settings
        )
    except _TargetError as exc:
        await _safe_reply(message, exc.reply)
        return

    try:
        record = await sheets.get_plan_record(target_id)
    except Exception as exc:
        logger.error("Failed to read plan for user %s: %s", target_id, exc)
        await _safe_reply(message, "❌ Could not read plan — please try again later.")
        return

    plan = record["plan"] if record is not None else DEFAULT_PLAN
    streak = record["streak"] if record is not None else 0

    if target_id == caller.id:
        await _safe_reply(
            message,
            f"Your plan: {plan} workouts/week · streak: {streak} weeks.",
        )
    else:
        who = _who_label(target_id, target_username, target_display)
        note = "" if record is not None else " (no plan set yet, using default 3)"
        await _safe_reply(
            message,
            f"{who} — plan: {plan} workouts/week · streak: {streak} weeks.{note}",
        )


async def _safe_reply(message, text: str) -> None:
    """Send a plain-text reply, swallowing/ logging any Telegram failure."""

    try:
        await message.reply_text(text)
    except TelegramError as exc:
        logger.error("Failed to send reply: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error sending reply: %s", exc)


async def _check_anthropic(settings: Settings) -> tuple[str, str]:
    """Validate the Anthropic API key with a minimal, cheap ``messages.create``.

    Confirms ``ANTHROPIC_API_KEY`` is present, then makes a tiny call
    (``max_tokens=1`` with a one-word prompt) using the configured model to
    prove the key is accepted. The blocking call runs in
    :func:`asyncio.to_thread`. Full error detail is logged; only a concise,
    secret-free reason is returned.

    Returns:
        A ``(status_emoji, message)`` tuple where ``status_emoji`` is one of
        ``"✅"``, ``"❌"``, or ``"⚠️"``.
    """

    if not settings.anthropic_api_key:
        return "❌", "ANTHROPIC_API_KEY not set"

    def _ping() -> None:
        client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
        create_kwargs: dict = {
            "model": settings.anthropic_model,
            "max_tokens": 1,
            "messages": [{"role": "user", "content": "Hi"}],
        }
        # Only include temperature when explicitly configured; omit otherwise so
        # models (e.g. claude-sonnet-5) that reject the parameter still pass.
        if settings.anthropic_temperature is not None:
            create_kwargs["temperature"] = settings.anthropic_temperature
        client.messages.create(**create_kwargs)

    try:
        await asyncio.to_thread(_ping)
    except anthropic.AuthenticationError as exc:
        logger.error("Anthropic auth check failed (invalid key): %s", exc)
        return "❌", "invalid API key"
    except anthropic.NotFoundError as exc:
        # A 404 / not_found_error typically means the configured model id is not
        # available to this account. Report it distinctly from an auth problem
        # so the operator knows to fix ANTHROPIC_MODEL (never leak the key).
        logger.error(
            "Anthropic check failed (model not found: %s): %s",
            settings.anthropic_model,
            exc,
        )
        return "⚠️", "model not found — set ANTHROPIC_MODEL to a valid model"
    except anthropic.APIError as exc:
        # Some SDK/transport paths surface a 404 as a generic APIError; detect a
        # model not_found_error here too so it's still reported distinctly.
        status_code = getattr(exc, "status_code", None)
        message = str(exc).lower()
        if status_code == 404 or "not_found_error" in message:
            logger.error(
                "Anthropic check failed (model not found: %s): %s",
                settings.anthropic_model,
                exc,
            )
            return "⚠️", "model not found — set ANTHROPIC_MODEL to a valid model"
        # A 400 invalid_request_error (e.g. an unsupported parameter) should no
        # longer occur for temperature, but report any other 400 distinctly with
        # a short, secret-free reason so the operator has a hint.
        if status_code == 400 or "invalid_request_error" in message:
            logger.error("Anthropic check failed (bad request): %s", exc)
            return "⚠️", "bad request — see logs"
        logger.error("Anthropic auth check failed (API error): %s", exc)
        return "⚠️", "API error — see logs"
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Anthropic auth check failed (unexpected): %s", exc)
        return "⚠️", "check failed — see logs"

    return "✅", "key valid"


async def testsheet_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Verify Google Sheets connectivity and Editor access, then reply.

    Registered with ``CommandHandler("testsheet", testsheet_command)``. Reuses
    the shared :func:`bot.services.sheets.check_sheets` helper, which authorizes
    with the service account, opens the spreadsheet by ``GOOGLE_SHEET_ID``,
    ensures the ``Log`` worksheet (creating it — proving Editor access — if
    absent, without appending junk to real data), and reads the header row to
    confirm read access. All blocking gspread calls run in
    :func:`asyncio.to_thread`.

    Errors are logged in full but only a concise, secret-free reason is sent to
    chat; the raw service-account JSON is never leaked.
    """

    message = update.effective_message
    if message is None:
        return

    settings = _get_settings(context)
    if settings is None:
        try:
            await message.reply_text("❌ Google Sheets: internal error — see logs.")
        except TelegramError as exc:
            logger.error("Failed to send /testsheet reply: %s", exc)
        return

    ok, detail = await check_sheets(settings)
    reply_text = (
        f"✅ Google Sheets: connected. {detail}"
        if ok
        else f"❌ Google Sheets: {detail}"
    )

    try:
        await message.reply_text(reply_text)
    except TelegramError as exc:
        logger.error("Failed to send /testsheet reply: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error handling /testsheet: %s", exc)


async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Report health across Telegram, Anthropic, and Google Sheets.

    Registered with ``CommandHandler("status", status_command)``. Each check is
    guarded in its own try/except so one failing integration still lets the
    others report. All network/blocking calls run in :func:`asyncio.to_thread`
    (via the shared helpers). Secrets are never sent to chat.
    """

    message = update.effective_message
    if message is None:
        return

    settings = _get_settings(context)
    if settings is None:
        try:
            await message.reply_text("❌ Run Bot Status: internal error — see logs.")
        except TelegramError as exc:
            logger.error("Failed to send /status reply: %s", exc)
        return

    # 1. Telegram — trivially reachable since the command ran; enrich with the
    #    bot username via get_me().
    try:
        me = await context.bot.get_me()
        telegram_line = f"Telegram: ✅ @{me.username}"
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Telegram get_me() failed during /status: %s", exc)
        telegram_line = "Telegram: ⚠️ reachable, username unknown"

    # 2. Anthropic — minimal auth check.
    try:
        anthropic_emoji, anthropic_detail = await _check_anthropic(settings)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Anthropic check raised during /status: %s", exc)
        anthropic_emoji, anthropic_detail = "⚠️", "check failed — see logs"
    anthropic_line = f"Anthropic: {anthropic_emoji} {anthropic_detail}"

    # 3. Google Sheets — same shared check as /testsheet.
    try:
        sheets_ok, sheets_detail = await check_sheets(settings)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Sheets check raised during /status: %s", exc)
        sheets_ok, sheets_detail = False, "check failed — see logs"
    sheets_line = (
        f"Google Sheets: ✅ {sheets_detail}"
        if sheets_ok
        else f"Google Sheets: ❌ {sheets_detail}"
    )

    # 4. TARGET_CHAT_ID — presence and value.
    if settings.target_chat_id is not None:
        target_line = f"Target chat: ✅ {settings.target_chat_id}"
    else:
        target_line = "Target chat: ⚠️ not set — leaderboards disabled"

    # 5. Timezone.
    timezone_line = f"Timezone: {settings.timezone}"

    reply_text = "\n".join(
        [
            "🤖 Run Bot Status",
            "",
            telegram_line,
            anthropic_line,
            sheets_line,
            target_line,
            timezone_line,
        ]
    )

    try:
        await message.reply_text(reply_text)
    except TelegramError as exc:
        logger.error("Failed to send /status reply: %s", exc)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Unexpected error handling /status: %s", exc)