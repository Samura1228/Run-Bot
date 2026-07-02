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
from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.services.sheets import check_sheets

logger = logging.getLogger(__name__)


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