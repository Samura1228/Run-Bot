"""Application entry point.

Wires config, services, handlers, and the scheduler, then starts long-polling.
The APScheduler is started inside a PTB ``post_init`` hook so it attaches to the
same asyncio loop, and shut down in ``post_shutdown``.
"""

from __future__ import annotations

import logging
import sys

from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot.config import ConfigError, Settings, get_settings
from bot.handlers.commands import (
    chatid_command,
    myplan_command,
    setplan_command,
    status_command,
    testsheet_command,
    whoami_command,
)
from bot.handlers.errors import error_handler
from bot.handlers.photo import PhotoHandler
from bot.services.leaderboard import LeaderboardService
from bot.services.scheduler import build_scheduler
from bot.services.sheets import SheetsService
from bot.services.vision import ClaudeVisionService
from bot.utils.points import build_activity_points

logger = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Quiet down noisy third-party loggers.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)


def build_application(settings: Settings) -> Application:
    """Build the PTB Application, wiring services, handlers, and scheduler hooks."""

    # Instantiate services.
    sheets = SheetsService(
        service_account_info=settings.google_service_account_info,
        sheet_id=settings.google_sheet_id,
    )
    vision = ClaudeVisionService(
        api_key=settings.anthropic_api_key,
        model=settings.anthropic_model,
        timezone=settings.timezone,
        temperature=settings.anthropic_temperature,
    )
    leaderboard = LeaderboardService(sheets)
    activity_points = build_activity_points(settings.points_per_run)

    application = ApplicationBuilder().token(settings.telegram_bot_token).build()

    # Register the photo handler.
    photo_handler = PhotoHandler(
        settings=settings,
        vision=vision,
        sheets=sheets,
        activity_points=activity_points,
    )
    application.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    # Register utility & diagnostic commands (work in any chat type).
    application.add_handler(CommandHandler("chatid", chatid_command))
    application.add_handler(CommandHandler("testsheet", testsheet_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("whoami", whoami_command))
    application.add_handler(CommandHandler("setplan", setplan_command))
    application.add_handler(CommandHandler("myplan", myplan_command))

    # Register a global error handler so exceptions in the update loop are
    # logged cleanly (and Conflict is special-cased) instead of bubbling up.
    application.add_error_handler(error_handler)

    # Stash references for the lifecycle hooks.
    application.bot_data["settings"] = settings
    application.bot_data["sheets"] = sheets
    application.bot_data["leaderboard"] = leaderboard

    async def post_init(app: Application) -> None:
        """Initialize Sheets and start the scheduler on the running loop."""

        await sheets.initialize()

        scheduler = build_scheduler(
            bot=app.bot,
            leaderboard=leaderboard,
            sheets=sheets,
            target_chat_id=settings.target_chat_id,
            tz=settings.timezone,
        )
        scheduler.start()
        app.bot_data["scheduler"] = scheduler
        if settings.target_chat_id is not None:
            logger.info(
                "TARGET_CHAT_ID is set (%s); leaderboards will be posted there.",
                settings.target_chat_id,
            )
        else:
            logger.warning(
                "TARGET_CHAT_ID is not set; leaderboards are disabled. "
                "Run /chatid in your group to discover the ID."
            )
        logger.info("Bot started; scheduler running.")

    async def post_shutdown(app: Application) -> None:
        """Gracefully stop the scheduler on shutdown."""

        scheduler = app.bot_data.get("scheduler")
        if scheduler is not None and scheduler.running:
            scheduler.shutdown(wait=False)
            logger.info("Scheduler shut down.")

    application.post_init = post_init
    application.post_shutdown = post_shutdown

    return application


def main() -> None:
    """Load settings, build the application, and run long-polling."""

    try:
        settings = get_settings()
    except ConfigError as exc:
        # Fail fast with a clear error before any logging config exists.
        print(f"Configuration error: {exc}", file=sys.stderr)
        sys.exit(1)

    _configure_logging(settings.log_level)
    logger.info("Starting Run Bot (model=%s, tz=%s).", settings.anthropic_model, settings.timezone)

    application = build_application(settings)

    # run_polling manages the event loop, reconnection, and backoff.
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()