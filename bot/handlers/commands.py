"""Command handlers.

Contains simple slash-command handlers. Currently only the ``/chatid`` command,
a small utility that replies with the current chat's ID so operators can
discover the value for the ``TARGET_CHAT_ID`` environment variable.

The command works in any chat type (private, group, supergroup, channel) and,
like the rest of the codebase, never crashes on failure — errors are logged and
swallowed.
"""

from __future__ import annotations

import logging
from html import escape

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

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