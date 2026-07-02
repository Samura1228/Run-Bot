"""Global error handler.

Registered with ``application.add_error_handler`` so any exception raised while
processing an update (or in a background job) is logged cleanly instead of
bubbling up and spamming PTB's "No error handlers are registered" message.

Like the rest of the codebase, this handler never raises: it always logs and
swallows. It special-cases :class:`telegram.error.Conflict` (two pollers on the
same token) with a concise, actionable WARNING.
"""

from __future__ import annotations

import logging

from telegram import Update
from telegram.error import Conflict
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def error_handler(
    update: object, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Log any exception raised in the update loop; never re-raise.

    Args:
        update: The update being processed when the error occurred (may be any
            object, or ``None`` for non-update errors like polling failures).
        context: The PTB context whose ``error`` attribute holds the exception.
    """

    error = context.error

    # Special-case the "two pollers on the same token" conflict. This is almost
    # always a brief, transient overlap during a Railway redeploy (or a local
    # run left running). Log a clear, concise WARNING and do not crash.
    if isinstance(error, Conflict):
        logger.warning(
            "Conflict: another bot instance is polling with the same token. "
            "Ensure only ONE instance runs (stop local runs / avoid overlapping "
            "Railway deployments)."
        )
        return

    # Build a short, secret-free description of the update, if available.
    update_desc = "no update"
    if isinstance(update, Update):
        chat = update.effective_chat
        chat_id = chat.id if chat is not None else "unknown"
        update_desc = f"update_id={update.update_id}, chat_id={chat_id}"

    # Log the exception with a full traceback at ERROR level.
    logger.error(
        "Exception while handling an update (%s): %s",
        update_desc,
        error,
        exc_info=error,
    )