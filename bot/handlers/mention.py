"""@mention handler — the in-chat assistant.

Answers a member's question when they @mention the bot (or reply to one of its
messages) in the target group. Deliberately narrow:

* **Only the target chat.** Not private chats, not any other group the bot is
  added to. The bot already receives every message in the group (that is how
  the photo pipeline works), so without this gate it would answer anywhere.
* **Only when addressed.** A plain message is never answered.
* **Rate limited** per member and per chat, because every answer costs an API
  call and a group can produce a lot of chatter.

Every rejection is silent. An "you are rate limited" message in a group is the
same spam the photo pipeline is careful to avoid.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from typing import Optional

from telegram import Update
from telegram.constants import MessageEntityType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.services.assistant import ClaudeAssistantService
from bot.services.sheets import SheetsService
from bot.utils.dates import current_week_bounds
from bot.utils.points import format_points

logger = logging.getLogger(__name__)

# A question longer than this is almost certainly not a question.
MAX_QUESTION_CHARS = 500
# ...and one shorter than this ("@runcy_bot" alone, "@runcy_bot ?") has nothing
# to answer.
MIN_QUESTION_CHARS = 3


class RateLimiter:
    """Per-user cooldown plus a rolling per-chat hourly cap.

    In-memory on purpose: a restart clears it, which is acceptable for a
    spend guard on a ten-person club and avoids putting chat traffic in the
    sheet. Both limits are checked together so one chatty member cannot
    exhaust the chat's hourly budget faster than the cooldown allows.
    """

    def __init__(self, per_user_seconds: int, per_chat_hourly: int) -> None:
        self._per_user_seconds = per_user_seconds
        self._per_chat_hourly = per_chat_hourly
        self._last_by_user: dict[int, float] = {}
        self._chat_hits: deque[float] = deque()

    def check(self, user_id: int, now: Optional[float] = None) -> Optional[str]:
        """Return ``None`` when allowed, else a short reason for the logs."""

        moment = time.monotonic() if now is None else now

        last = self._last_by_user.get(user_id)
        if last is not None and moment - last < self._per_user_seconds:
            return (
                f"user {user_id} is within the "
                f"{self._per_user_seconds}s cooldown"
            )

        while self._chat_hits and moment - self._chat_hits[0] >= 3600:
            self._chat_hits.popleft()
        if len(self._chat_hits) >= self._per_chat_hourly:
            return f"chat hit the {self._per_chat_hourly}/hour cap"

        return None

    def record(self, user_id: int, now: Optional[float] = None) -> None:
        """Record an answered question against both limits."""

        moment = time.monotonic() if now is None else now
        self._last_by_user[user_id] = moment
        self._chat_hits.append(moment)


def extract_question(message, bot_username: str) -> str:
    """Strip the bot's @mention out of the message and return what's left.

    Only ``mention`` entities matching THIS bot are removed, so a question that
    also mentions a teammate keeps their name — it may well be what the
    question is about.
    """

    text = message.text or ""
    if not text:
        return ""

    target = f"@{bot_username}".lower()
    cuts: list[tuple[int, int]] = []
    for entity in message.entities or ():
        if entity.type != MessageEntityType.MENTION:
            continue
        chunk = text[entity.offset : entity.offset + entity.length]
        if chunk.lower() == target:
            cuts.append((entity.offset, entity.offset + entity.length))

    for start, end in sorted(cuts, reverse=True):
        text = text[:start] + text[end:]
    return " ".join(text.split())


def is_addressed_to_bot(message, bot_id: int, bot_username: str) -> bool:
    """True when the message @mentions the bot or replies to one of its posts."""

    reply = getattr(message, "reply_to_message", None)
    if reply is not None:
        author = getattr(reply, "from_user", None)
        if author is not None and author.id == bot_id:
            return True

    text = message.text or ""
    target = f"@{bot_username}".lower()
    for entity in message.entities or ():
        if entity.type != MessageEntityType.MENTION:
            continue
        chunk = text[entity.offset : entity.offset + entity.length]
        if chunk.lower() == target:
            return True
    return False


class MentionHandler:
    """Callable handler for text messages that address the bot."""

    def __init__(
        self,
        settings: Settings,
        assistant: ClaudeAssistantService,
        sheets: SheetsService,
    ) -> None:
        self._settings = settings
        self._assistant = assistant
        self._sheets = sheets
        self._limiter = RateLimiter(
            per_user_seconds=settings.assistant_user_cooldown_seconds,
            per_chat_hourly=settings.assistant_chat_hourly_limit,
        )

    async def _personal_context(self, user_id: int, name: str) -> str:
        """Collect the asker's own data so personal questions can be answered.

        Best-effort: any read that fails is simply left out, because a partial
        answer beats refusing to answer at all. Coaches legitimately come back
        with 0 points — they are not scored — and the note says so rather than
        letting the model imply they have been lazy.
        """

        start, end = current_week_bounds(self._settings.timezone)
        lines = [f"The member asking is {name}.", f"This week is {start} to {end}."]

        if self._settings.is_coach(user_id):
            lines.append(
                "They are a COACH: coaches are not scored, so they have no "
                "points, plan or team of their own."
            )
            return "\n".join(lines)

        try:
            plan = await self._sheets.get_plan(user_id)
            lines.append(f"Their weekly plan is {plan} runs per week.")
        except Exception as exc:  # noqa: BLE001 - context is optional
            logger.warning("Assistant: could not read the plan: %s", exc)

        try:
            points = await self._sheets.sum_user_points_in_range(
                user_id, start, end
            )
            lines.append(
                f"They have {format_points(points)} points so far this week."
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Assistant: could not read week points: %s", exc)

        try:
            current = await self._sheets.get_current_team_round()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Assistant: could not read the team round: %s", exc)
            current = None
        if current is not None:
            team = next(
                (
                    name_
                    for name_, members in current["teams"]
                    if user_id in members
                ),
                None,
            )
            window = f"{current['start_date']} to {current['end_date']}"
            if team is not None:
                lines.append(f"They are on team '{team}' ({window}).")
            else:
                lines.append(
                    f"A team round is running ({window}) but they are not in it."
                )
        else:
            lines.append("No team round is running right now.")

        return "\n".join(lines)

    async def __call__(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Entry point for a ``MessageHandler(filters.TEXT & ~filters.COMMAND)``."""

        message = update.effective_message
        if message is None or not message.text:
            return
        user = message.from_user
        if user is None or user.is_bot:
            return

        # Gate 1: the target group only. Private chats and any other group the
        # bot is added to get nothing at all.
        target = self._settings.target_chat_id
        if target is None or message.chat_id != target:
            return

        # Gate 2: only when actually addressed.
        bot_username = (context.bot.username or "").lstrip("@")
        if not bot_username:
            logger.warning("Assistant: bot username unknown; ignoring mention.")
            return
        if not is_addressed_to_bot(message, context.bot.id, bot_username):
            return

        question = extract_question(message, bot_username)
        if len(question) < MIN_QUESTION_CHARS:
            logger.info("Assistant: mention with no question; ignoring.")
            return
        if len(question) > MAX_QUESTION_CHARS:
            logger.info(
                "Assistant: question from %s is %d chars (max %d); ignoring.",
                user.id,
                len(question),
                MAX_QUESTION_CHARS,
            )
            return

        # Gate 3: rate limits. Silent — announcing them would be the spam the
        # rest of this bot works to avoid.
        reason = self._limiter.check(user.id)
        if reason is not None:
            logger.info("Assistant: skipping question — %s.", reason)
            return

        name = " ".join(
            part for part in [user.first_name, user.last_name] if part
        ).strip() or (f"@{user.username}" if user.username else "a member")

        try:
            await context.bot.send_chat_action(
                chat_id=message.chat_id, action="typing"
            )
        except TelegramError:
            pass  # cosmetic only

        note = await self._personal_context(user.id, name)
        answer = await self._assistant.answer(question, note)
        if answer is None:
            # Already logged. Stay quiet rather than posting an error.
            return

        # Count it only once an answer actually exists, so a failed call does
        # not eat the member's cooldown.
        self._limiter.record(user.id)

        try:
            await message.reply_text(answer)
        except TelegramError as exc:
            logger.error("Assistant: failed to send the answer: %s", exc)

        logger.info(
            "Assistant answered %s (%d chars in, %d out).",
            user.id,
            len(question),
            len(answer),
        )
