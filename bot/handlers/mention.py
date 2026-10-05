"""@mention handler — the in-chat assistant.

Answers a member's question when they @mention the bot (or reply to one of its
messages) in the target group. Deliberately narrow:

* **The club group, plus the admin's private chat.** Any other group the bot
  is added to gets nothing, and a private chat gets nothing unless the sender
  is in ``ADMIN_IDS`` — that private channel exists so the assistant can be
  tested before the whole club sees it. The bot already receives every message
  in the group (that is how the photo pipeline works), so without this gate it
  would answer anywhere.
* **Only when @mentioned** — in the group. Replying to one of the bot's own
  messages does not count (see :func:`is_addressed_to_bot`). In a one-to-one
  chat with the admin no @mention is needed.
* **The group can be switched off** (``ASSISTANT_GROUP_ENABLED``) while the
  admin's private chat keeps working.
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
from telegram.constants import ChatType, MessageEntityType
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.services.assistant import CITE_RE, ClaudeAssistantService
from bot.services.sheets import SheetsService
from bot.services.voice import VoiceArchive
from bot.utils.dates import current_week_bounds
from bot.utils.points import format_points

logger = logging.getLogger(__name__)

# A question longer than this is almost certainly not a question.
MAX_QUESTION_CHARS = 500
# ...and one shorter than this ("@runcy_bot" alone, "@runcy_bot ?") has nothing
# to answer.
MIN_QUESTION_CHARS = 3


class RateLimiter:
    """Per-user cooldown plus a rolling hourly cap kept PER CHAT.

    The hourly cap is keyed by chat id so the admin's private testing does not
    eat the group's budget (and vice versa) — with two chats allowed, a single
    shared counter would silently couple them.

    In-memory on purpose: a restart clears it, which is acceptable for a spend
    guard on a ten-person club and avoids putting chat traffic in the sheet.
    """

    def __init__(self, per_user_seconds: int, per_chat_hourly: int) -> None:
        self._per_user_seconds = per_user_seconds
        self._per_chat_hourly = per_chat_hourly
        self._last_by_user: dict[int, float] = {}
        self._chat_hits: dict[int, deque[float]] = {}

    def check(
        self, user_id: int, chat_id: int, now: Optional[float] = None
    ) -> Optional[str]:
        """Return ``None`` when allowed, else a short reason for the logs."""

        moment = time.monotonic() if now is None else now

        last = self._last_by_user.get(user_id)
        if last is not None and moment - last < self._per_user_seconds:
            return (
                f"user {user_id} is within the "
                f"{self._per_user_seconds}s cooldown"
            )

        hits = self._chat_hits.get(chat_id)
        if hits is not None:
            while hits and moment - hits[0] >= 3600:
                hits.popleft()
            if len(hits) >= self._per_chat_hourly:
                return (
                    f"chat {chat_id} hit the {self._per_chat_hourly}/hour cap"
                )

        return None

    def record(
        self, user_id: int, chat_id: int, now: Optional[float] = None
    ) -> None:
        """Record an answered question against both limits."""

        moment = time.monotonic() if now is None else now
        self._last_by_user[user_id] = moment
        self._chat_hits.setdefault(chat_id, deque()).append(moment)


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


def is_addressed_to_bot(message, bot_username: str) -> bool:
    """True only when the message @mentions the bot.

    Replying to one of the bot's messages deliberately does NOT count. It used
    to, as a convenience for follow-up questions, and it was a mistake: the
    bot answers every workout screenshot with "✅ Nice run…" and posts the
    leaderboards, so the group is full of its messages. People replied to
    those with "молодец!" and got an assistant answer they never asked for.
    An @mention is the only unambiguous way to address it.
    """

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
        voice: Optional[VoiceArchive] = None,
    ) -> None:
        self._settings = settings
        self._assistant = assistant
        self._sheets = sheets
        self._voice = voice
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

    async def _send(self, context, message, answer: str, cite) -> None:
        """Send the answer, replying to the cited voice message when there is one.

        Replying to the original recording is the whole point of the citation:
        the quote appears above the answer and tapping it jumps to that voice
        note. But the message may be older than the bot, deleted, or carry an
        id Telegram will not accept — so a rejected reply falls back to the
        same text with the date and minute written out, which is still useful.
        """

        target_id = None
        suffix = ""
        if cite is not None and self._voice is not None:
            voice_id = int(cite.group(1))
            stamp = cite.group(2)
            entry = self._voice.get(voice_id)
            if entry is not None:
                target_id = voice_id
                suffix = f"\n\n🎧 Голосовое от {entry.get('date','')}, с {stamp}"

        if target_id is not None:
            try:
                await context.bot.send_message(
                    chat_id=message.chat_id,
                    text=answer + suffix,
                    reply_to_message_id=target_id,
                )
                return
            except TelegramError as exc:
                # Expected for a message Telegram will not let us reply to.
                logger.info(
                    "Assistant: could not reply to voice %s (%s); "
                    "sending plain text instead.",
                    target_id,
                    exc,
                )

        try:
            await message.reply_text(answer + suffix)
        except TelegramError as exc:
            logger.error("Assistant: failed to send the answer: %s", exc)

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

        # Gate 1: which chats are allowed at all.
        #   * private  -> ADMIN_IDS only, so the assistant can be tried out
        #     before the club sees it. No @mention needed: in a one-to-one
        #     chat, making someone tag the bot would be absurd.
        #   * the club group -> everyone, but only while the group switch is
        #     on, and only when the bot is actually addressed.
        #   * anything else -> silence.
        chat = update.effective_chat
        is_private = getattr(chat, "type", None) == ChatType.PRIVATE
        if is_private:
            if not self._settings.is_admin(user.id):
                return
            require_mention = False
        else:
            target = self._settings.target_chat_id
            if target is None or message.chat_id != target:
                return
            if not self._settings.assistant_group_enabled:
                # debug, not info: this fires on every group message while the
                # switch is off, and must not drown the log.
                logger.debug(
                    "Assistant: group answering is off; ignoring message."
                )
                return
            require_mention = True

        bot_username = (context.bot.username or "").lstrip("@")
        if require_mention:
            # Gate 2: only when actually addressed.
            if not bot_username:
                logger.warning(
                    "Assistant: bot username unknown; ignoring mention."
                )
                return
            if not is_addressed_to_bot(message, bot_username):
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
        reason = self._limiter.check(user.id, message.chat_id)
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
        result = await self._assistant.answer(question, note)
        if result is None:
            # Already logged. Stay quiet rather than posting an error.
            return

        # The model may ask for the coach's actual words before answering.
        # Only questions that need the archive pay for this second call.
        if result.needs_voice and self._voice is not None:
            logger.info(
                "Assistant: fetching voice transcripts %s.",
                list(result.needs_voice),
            )
            transcripts = self._voice.transcript_for(list(result.needs_voice))
            if transcripts:
                result = await self._assistant.answer(
                    question, note, transcripts=transcripts
                )
                if result is None:
                    return
        if result.needs_voice and not result.text:
            # It asked for transcripts we could not supply; nothing to send.
            logger.warning(
                "Assistant asked for voice %s but no archive is loaded.",
                list(result.needs_voice),
            )
            return

        answer = result.text
        # Pull the citation tag off the end: it is machinery, not prose.
        cite = CITE_RE.search(answer)
        answer = CITE_RE.sub("", answer).strip()
        if not answer:
            return

        # Count it only once an answer actually exists, so a failed call does
        # not eat the member's cooldown.
        self._limiter.record(user.id, message.chat_id)

        await self._send(context, message, answer, cite)

        logger.info(
            "Assistant answered %s (%d chars in, %d out).",
            user.id,
            len(question),
            len(answer),
        )
