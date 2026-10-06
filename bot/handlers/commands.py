"""Command handlers.

Contains simple slash-command handlers:

- ``/chatid`` — replies with the current chat's ID so operators can discover the
  value for the ``TARGET_CHAT_ID`` environment variable.
- ``/testsheet`` — verifies Google Sheets connectivity and Editor access.
- ``/status`` — a consolidated health report across Telegram, Anthropic, and
  Google Sheets, plus the configured target chat and timezone.
- ``/team`` — coaches & TEAM_ADMIN_IDS. A multi-line message creates the
  week's teams; bare ``/team`` shows live standings; ``/team stop`` cancels.

The commands work in any chat type (private, group, supergroup, channel) and,
like the rest of the codebase, never crash on failure — errors are logged and
swallowed, and only concise, secret-free reasons are ever sent to chat.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from html import escape
from typing import Optional
from uuid import uuid4

import anthropic
from telegram import Update, User
from telegram.constants import MessageEntityType, ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot.config import Settings
from bot.services.leaderboard import LeaderboardService
from bot.services.sheets import (
    TEAMS_STATUS_CANCELLED,
    SheetsService,
    check_sheets,
)
from bot.utils.dates import (
    MAX_ROUND_DAYS,
    MIN_ROUND_DAYS,
    parse_date_range,
    today_in,
)
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
    f"Usage (coach only): /setplan @user N  or reply to a user + /setplan N "
    f"(N {MIN_PLAN}–{MAX_PLAN})."
)
_COACH_ONLY_MSG = "Only a coach can set or view another member's plan."
# Shown for /chatid, /status and /testsheet to anyone not in ADMIN_IDS. They
# expose chat IDs, service-account and API health, so they are operator-only.
_ADMIN_ONLY_MSG = "This is a bot admin command."
_SETPLAN_COACH_ONLY_MSG = "Only your coach can set up workouts for you."
# Shown when someone who is neither a coach nor a TEAM_ADMIN_IDS member tries
# to run /team. Deliberately does not name who is allowed.
_TEAM_COACH_ONLY_MSG = (
    "Only a coach or the team organiser can manage the team leaderboard."
)
# Accepted spellings of the /team argument that cancels the active round.
_TEAM_STOP_ARGS = frozenset({"stop", "cancel", "end"})
_TEAM_STATUS_ARGS = frozenset({"status", "now", "score"})
_TEAM_USAGE = (
    "Usage (coach only) — dates first, then a team name per block:\n\n"
    "/team 28/09/26 - 04/10/26\n"
    "Team 1\n"
    "Alexey B\n"
    "Elena\n"
    "Team 2\n"
    "Marfa\n"
    "Anastacia S\n\n"
    "Dates are DD/MM/YY (or DD/MM/YYYY) and are REQUIRED. Any line that is "
    "NOT a known member name starts a new team. Names come from the 'Members' "
    "tab of the Google Sheet — add people there first.\n"
    "/team status shows current standings, /team stop cancels the round."
)
_NO_ACTIVE_ROUND_MSG = (
    "No team competition is running right now, so no teams are being tracked.\n"
    "Start one by sending /team with the teams listed underneath."
)


async def chatid_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Reply with the current chat's ID, type, and title — ADMIN ONLY.

    Restricted to ``ADMIN_IDS``: the chat ID is operational plumbing, not
    something the group needs. Registered with a ``CommandHandler("chatid", chatid_command)``. PTB's
    ``CommandHandler`` also matches the ``/chatid@BotUsername`` form used in
    groups, so no extra handling is needed for that.

    The chat ID is wrapped in Telegram HTML ``<code>`` formatting so it can be
    tapped/copied easily.
    """

    message = update.effective_message
    chat = update.effective_chat
    if message is None or chat is None:
        return

    settings = _get_settings(context)
    caller = message.from_user
    if settings is None or caller is None or not settings.is_admin(caller.id):
        await _safe_reply(message, _ADMIN_ONLY_MSG)
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


def _get_leaderboard(
    context: ContextTypes.DEFAULT_TYPE,
) -> Optional[LeaderboardService]:
    """Return the shared :class:`LeaderboardService` stashed in ``bot_data``."""

    leaderboard = context.application.bot_data.get("leaderboard")
    if isinstance(leaderboard, LeaderboardService):
        return leaderboard
    logger.error("LeaderboardService not found in bot_data; /team unavailable.")
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
    """Set a weekly plan (workouts/week) via ``/setplan`` — COACHES ONLY.

    Also registered under the aliases ``/setmyplan``, ``/setuserplan`` and
    ``/setplans`` (see :func:`bot.main.build_application`), since PTB ignores
    any command it has no handler for — an unregistered spelling makes the bot
    look dead: no reply AND no sheet write.

    Forms (coach only):
      - ``/setplan @username N`` → set that user's plan.
      - reply to a user's message + ``/setplan N`` → set their plan.

    The plan is parsed from the LAST integer token (so ``@user 4`` and (reply)
    ``4`` both work) and validated to ``[MIN_PLAN, MAX_PLAN]`` (2–6). Only a
    configured coach may set plans: regular users can no longer set up their
    own workouts (self-service is disabled) and are told to ask their coach.
    On success the target's Plans row is upserted.
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

        # Coach-only guard: setting up workouts/plans is restricted to coaches.
        # Regular users can no longer set up their own workouts — they must ask
        # their coach. Reject non-coach callers early with a clear message.
        if not settings.is_coach(caller.id):
            await _safe_reply(message, _SETPLAN_COACH_ONLY_MSG)
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
    """Reply with a plan via ``/myplan``.

    Forms:
      - ``/myplan`` → the caller's own plan (self-service).
      - ``/myplan @username`` (coach) → that user's plan.
      - reply to a user's message + ``/myplan`` (coach) → their plan.

    Defaults to plan :data:`DEFAULT_PLAN` (3) if the target has no
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

    if target_id == caller.id:
        await _safe_reply(
            message,
            f"Your plan: {plan} workouts/week.",
        )
    else:
        who = _who_label(target_id, target_username, target_display)
        note = "" if record is not None else " (no plan set yet, using default 3)"
        await _safe_reply(
            message,
            f"{who} — plan: {plan} workouts/week.{note}",
        )

def parse_team_message(
    text: str, directory: dict[str, dict]
) -> tuple[list[tuple[str, list[int]]], list[str], list[str]]:
    """Parse a multi-line ``/team`` message into teams.

    The format is the one a coach naturally writes: a team name on its own
    line, then one member per line, repeated. There is no marker distinguishing
    a heading from a member, so the RULE IS THE DIRECTORY — a line that
    resolves to a known member is a member, and any other non-empty line starts
    a new team. That is why the ``Members`` tab has to be filled in first, and
    why a misspelled name surfaces as an unexpected new team rather than
    silently vanishing.

    Args:
        text: The full message text, ``/team`` line included.
        directory: ``normalize_member_name`` → member dict, from
            :meth:`SheetsService.member_directory`.

    Returns:
        ``(teams, unknown, duplicates)`` where ``teams`` is a list of
        ``(name, [member_id, ...])`` in the order written, ``unknown`` lists
        lines that started a team but look like a stray name (a heading with no
        members under it), and ``duplicates`` lists members placed on more than
        one team. The caller rejects the message if either list is non-empty.
    """

    lines = (text or "").splitlines()
    # Drop the command itself, including a /team@BotName form and "stop".
    if lines and lines[0].lstrip().startswith("/"):
        lines = lines[1:]

    teams: list[tuple[str, list[int]]] = []
    seen_members: dict[int, str] = {}
    duplicates: list[str] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        member = directory.get(SheetsService.normalize_member_name(line))
        if member is None:
            # Not a known member → this line names a new team.
            teams.append((line, []))
            continue
        if not teams:
            # A member before any team heading: start an unnamed team so the
            # caller can report it rather than dropping the person.
            teams.append((f"Team {len(teams) + 1}", []))
        user_id = member["user_id"]
        if user_id in seen_members:
            duplicates.append(f"{member['name']} ({seen_members[user_id]})")
            continue
        seen_members[user_id] = teams[-1][0]
        teams[-1][1].append(user_id)

    # A heading with no members under it is almost always a misspelled name.
    unknown = [name for name, members in teams if not members]
    teams = [(name, members) for name, members in teams if members]
    return teams, unknown, duplicates


async def team_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Create (or stop) the weekly TEAM competition — COACHES & TEAM ADMINS.

    Forms:
      - ``/team <start> - <end>`` followed by team names and their members
        (see :data:`_TEAM_USAGE`) → creates a round over that INCLUSIVE date
        window. The dates are required. Re-running replaces any active round.
      - ``/team status`` (or ``/team`` alone) → live standings, posted into
        whichever chat the command was sent from.
      - ``/team stop`` → cancel the active round. No board is posted.

    Member names are resolved through the hand-maintained ``Members`` tab.
    Nothing is saved unless EVERY name resolves and nobody appears twice: a
    silently dropped member would quietly corrupt a whole week of scoring.
    """

    message = update.effective_message
    if message is None:
        return

    try:
        caller = message.from_user
        if caller is None:
            return

        settings = _get_settings(context)
        sheets = _get_sheets(context)
        if settings is None or sheets is None:
            await _safe_reply(
                message, "❌ Could not handle /team — internal error."
            )
            return

        if not settings.can_manage_teams(caller.id):
            await _safe_reply(message, _TEAM_COACH_ONLY_MSG)
            return

        args = getattr(context, "args", None) or []
        text = message.text or ""
        body_lines = [ln.strip() for ln in text.splitlines()[1:] if ln.strip()]

        # --- /team stop ------------------------------------------------- #
        if args and args[0].strip().lower() in _TEAM_STOP_ARGS:
            current = await sheets.get_current_team_round()
            if current is None:
                await _safe_reply(message, _NO_ACTIVE_ROUND_MSG)
                return
            await sheets.set_team_round_status(
                current["row_index"],
                TEAMS_STATUS_CANCELLED,
                label=current["round_id"],
            )
            logger.info(
                "Team round %s cancelled by %s.", current["round_id"], caller.id
            )
            await _safe_reply(
                message,
                "🛑 Team round stopped. No board will be posted and no teams "
                "are being tracked. Send /team with a new line-up to start "
                "another one.",
            )
            return

        # --- /team status → post the standings on demand ----------------- #
        # Same board the scheduler posts, but triggered by hand: the coach
        # sends it in the group and everyone sees the current totals without
        # waiting for the next 3-day tick. Bare /team does the same thing.
        if not body_lines or (
            args and args[0].strip().lower() in _TEAM_STATUS_ARGS
        ):
            leaderboard = _get_leaderboard(context)
            current = await sheets.get_current_team_round()
            if current is None:
                await _safe_reply(message, _NO_ACTIVE_ROUND_MSG)
                return
            if leaderboard is None:
                await _safe_reply(
                    message, "❌ Could not build the team board — internal error."
                )
                return
            start_date, end_date = current["start_date"], current["end_date"]
            entries = await leaderboard.aggregate_teams(
                current["teams"], start_date, end_date
            )
            await _safe_reply(
                message,
                f"{leaderboard.format_teams(entries, start_date, end_date, final=False)}"
                f"\n\n"
                f"({start_date} – {end_date}, in progress)",
            )
            return

        # --- /team <dates> <line-up> → create the round ------------------ #
        # The date range is REQUIRED and lives on the command line, so a round
        # is always an explicit window the coach chose rather than an implied
        # "this week". Both dates are inclusive: a round ending 04/10 counts
        # everything dated 04/10, i.e. all of that Sunday.
        header = (message.text or "").splitlines()[0]
        _, _, date_text = header.partition(" ")
        window = parse_date_range(date_text)
        if window is None:
            await _safe_reply(
                message,
                "⚠️ Nothing saved — I need the dates on the first line, "
                "day first:\n\n/team 28/09/26 - 04/10/26\n\n"
                f"(a round must be {MIN_ROUND_DAYS}–{MAX_ROUND_DAYS} days and "
                "end on or after it starts)",
            )
            return
        start_date, end_date = window

        try:
            directory = await sheets.member_directory()
        except Exception as exc:
            logger.error("Team: failed to read the Members directory: %s", exc)
            await _safe_reply(
                message,
                "❌ Couldn't read the 'Members' tab of the sheet — try again.",
            )
            return

        if not directory:
            await _safe_reply(
                message,
                "⚠️ The 'Members' tab is empty, so I don't know anyone's name "
                "yet. Fill in name / username / telegram_id there first "
                "(use /whoami to find an ID).",
            )
            return

        teams, unknown, duplicates = parse_team_message(text, directory)

        problems: list[str] = []
        if unknown:
            problems.append(
                "I don't know these names (or the team under them was empty): "
                + ", ".join(unknown)
            )
        if duplicates:
            problems.append(
                "These people are on more than one team: " + ", ".join(duplicates)
            )
        if not teams:
            problems.append("I couldn't find any team with members in it.")
        if problems:
            await _safe_reply(
                message,
                "⚠️ Nothing saved — fix this and send /team again:\n\n"
                + "\n\n".join(f"• {problem}" for problem in problems)
                + "\n\nNames come from the 'Members' tab of the sheet.",
            )
            return

        previous = await sheets.get_current_team_round()
        if previous is not None:
            await sheets.set_team_round_status(
                previous["row_index"],
                TEAMS_STATUS_CANCELLED,
                label=previous["round_id"],
            )
            logger.info(
                "Team: replacing active round %s.", previous["round_id"]
            )

        round_id = uuid4().hex[:8]
        await sheets.create_team_round(
            round_id=round_id,
            start_date=start_date,
            end_date=end_date,
            teams=teams,
            created_by=caller.id,
        )

        roster = "\n".join(
            f"{name} ({len(members)})" for name, members in teams
        )
        replaced = " Previous round replaced." if previous is not None else ""
        await _safe_reply(
            message,
            f"✅ Teams set for {start_date} – {end_date}:\n\n{roster}\n\n"
            f"Standings post every 3 days at 09:05; the final board the "
            f"morning after {end_date}.{replaced}",
        )
        logger.info(
            "Team round %s created by %s: %d teams, %s–%s.",
            round_id,
            caller.id,
            len(teams),
            start_date,
            end_date,
        )
    except Exception as exc:  # noqa: BLE001 - never fail silently
        logger.error("Unexpected error handling /team: %s", exc, exc_info=exc)
        await _safe_reply(
            message, "⚠️ Something went wrong with /team. Try again."
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
    """Verify Google Sheets connectivity and Editor access — ADMIN ONLY.

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
    caller = message.from_user
    if settings is None or caller is None or not settings.is_admin(caller.id):
        await _safe_reply(message, _ADMIN_ONLY_MSG)
        return
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
    """Report health across Telegram, Anthropic, and Google Sheets — ADMIN ONLY.

    Registered with ``CommandHandler("status", status_command)``. Each check is
    guarded in its own try/except so one failing integration still lets the
    others report. All network/blocking calls run in :func:`asyncio.to_thread`
    (via the shared helpers). Secrets are never sent to chat.
    """

    message = update.effective_message
    if message is None:
        return

    settings = _get_settings(context)
    caller = message.from_user
    if settings is None or caller is None or not settings.is_admin(caller.id):
        await _safe_reply(message, _ADMIN_ONLY_MSG)
        return
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