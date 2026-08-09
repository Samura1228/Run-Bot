"""Leaderboard service.

Aggregates points per user over a date range and formats weekly/monthly
leaderboard messages, plus the weekly **pairs** board (two configured members
competing on their combined weekly points).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol, Sequence

from bot.models import LeaderboardEntry, PairEntry
from bot.services.sheets import SheetsService
from bot.utils.points import format_points

logger = logging.getLogger(__name__)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class _Rankable(Protocol):
    """Minimal surface the shared renderer needs from a leaderboard row.

    Both :class:`~bot.models.LeaderboardEntry` (individual) and
    :class:`~bot.models.PairEntry` (pairs) satisfy it, so the SAME ranking /
    formatting code (including the "1224" tie logic) drives both boards.
    """

    points: float

    def label(self) -> str:  # pragma: no cover - structural typing only
        ...


class LeaderboardService:
    """Computes and formats leaderboards from Sheet data."""

    def __init__(self, sheets: SheetsService) -> None:
        self._sheets = sheets

    async def aggregate(
        self, start_date: date, end_date: date
    ) -> list[LeaderboardEntry]:
        """Aggregate points per user over ``[start_date, end_date]``.

        Groups by ``telegram_user_id``, sums points, keeps the latest display
        name/username seen, then sorts by points desc, display name asc.
        """

        rows = await self._sheets.read_rows_in_range(start_date, end_date)

        totals: dict[int, dict[str, Any]] = {}
        for row in rows:
            user_id = row["telegram_user_id"]
            entry = totals.setdefault(
                user_id,
                {
                    "points": 0.0,
                    "display_name": row["display_name"],
                    "telegram_username": row["telegram_username"],
                },
            )
            entry["points"] += row["points"]
            # Keep the latest display name/username seen for the user.
            entry["display_name"] = row["display_name"]
            entry["telegram_username"] = row["telegram_username"]

        entries = [
            LeaderboardEntry(
                telegram_user_id=user_id,
                display_name=data["display_name"],
                telegram_username=data["telegram_username"],
                points=data["points"],
            )
            for user_id, data in totals.items()
        ]
        entries.sort(key=lambda e: (-e.points, e.label().lower()))
        return entries

    async def aggregate_pairs(
        self,
        pairs: Sequence[tuple[int, int]],
        start_date: date,
        end_date: date,
    ) -> list[PairEntry]:
        """Aggregate combined points per configured pair over a date range.

        Reuses :meth:`aggregate` verbatim (same ``read_rows_in_range`` data
        path, same season cutoff, same stored point values — no extra
        multipliers, and ``streak_bonus`` rows count as normal points), then
        simply sums the two configured members' totals. A member with no rows in
        the range contributes ``0`` and never skips the pair.

        Display labels reuse :meth:`LeaderboardEntry.label` so a member with no
        ``telegram_username`` still renders by display name. If a member has no
        rows at all in the range (so no name in the ``Log``), the label falls
        back to their ``Plans`` ``@username`` if available, else ``user {id}``.

        Returns the pairs sorted by combined points desc, then by the rendered
        pair label (lowercased) for deterministic ordering within ties.
        """

        if not pairs:
            logger.info("No pairs configured; skipping pairs aggregation.")
            return []

        entries = await self.aggregate(start_date, end_date)
        by_user: dict[int, LeaderboardEntry] = {
            entry.telegram_user_id: entry for entry in entries
        }

        # Only hit the Plans tab if some member is missing from the Log window.
        missing = [
            member_id
            for pair in pairs
            for member_id in pair
            if member_id not in by_user
        ]
        plan_usernames: dict[int, str] = {}
        if missing:
            try:
                plan_usernames = {
                    row["user_id"]: (row.get("username") or "")
                    for row in await self._sheets.list_plans()
                }
            except Exception as exc:  # noqa: BLE001 - labels must never crash
                logger.warning(
                    "Pairs: could not read Plans for display-name fallback: %s",
                    exc,
                )

        pair_entries: list[PairEntry] = []
        for member_a, member_b in pairs:
            labels: list[str] = []
            total = 0.0
            for member_id in (member_a, member_b):
                entry = by_user.get(member_id)
                if entry is not None:
                    total += entry.points
                    labels.append(entry.label())
                    continue
                username = plan_usernames.get(member_id, "").strip().lstrip("@")
                labels.append(f"@{username}" if username else f"user {member_id}")
            pair_entries.append(
                PairEntry(
                    member_ids=(member_a, member_b),
                    member_labels=(labels[0], labels[1]),
                    points=total,
                )
            )

        pair_entries.sort(key=lambda e: (-e.points, e.label().lower()))
        return pair_entries

    @staticmethod
    def _format_ranking(entries: Sequence[_Rankable]) -> str:
        """Render one line per entry.

        Each line is ``{name}  - {points} points`` (note the two spaces
        before the hyphen, per the requested layout) with a trailing medal
        for ranks 1–3 and no trailing emoji for ranks 4+.

        Ranks use **standard competition ranking ("1224" style)**: users with
        the SAME point total share the SAME rank, and the next lower total's
        rank equals its 1-based position in the sorted list (so ranks are
        skipped after a tie). ``entries`` MUST already be sorted by points
        descending with a stable deterministic secondary sort (see
        :meth:`aggregate`), which keeps ordering within a tie consistent while
        still assigning every tied user the same rank number/medal. When a rank
        is skipped due to a tie (e.g. nobody is 2nd because two share 1st), that
        medal simply does not appear.
        """

        lines: list[str] = []
        prev_points: float | None = None
        rank = 0
        for position, entry in enumerate(entries, start=1):
            # Standard competition ranking: a new (lower) total takes the rank
            # equal to its 1-based position; equal totals keep the prior rank.
            if prev_points is None or entry.points != prev_points:
                rank = position
            prev_points = entry.points

            name = entry.label()
            line = f"{name}  - {format_points(entry.points)} points"
            medal = _MEDALS.get(rank)
            if medal:
                line = f"{line} {medal}"
            lines.append(line)
        return "\n".join(lines)

    def format_weekly(
        self,
        entries: list[LeaderboardEntry],
        start_date: date,
        end_date: date,
    ) -> str:
        """Format a weekly leaderboard message for a Mon–Sun range."""

        header = "Weekly leaders board 🏆"
        if not entries:
            return f"{header}\n\nNo runs logged this week yet."
        return f"{header}\n\n{self._format_ranking(entries)}"

    def format_monthly(
        self,
        entries: list[LeaderboardEntry],
        start_date: date,
        end_date: date,
    ) -> str:
        """Format a monthly leaderboard message for a full calendar month."""

        header = "Monthly leaders board 🏆"
        if not entries:
            return f"{header}\n\nNo runs logged this month yet."
        return f"{header}\n\n{self._format_ranking(entries)}"

    def format_pairs(
        self,
        entries: list[PairEntry],
        start_date: date,
        end_date: date,
    ) -> str:
        """Format the weekly pairs leaderboard message for a Mon–Sun range.

        Uses the SAME renderer as the individual boards, so lines read
        ``{A} ; {B}  - {points} points`` (two spaces before the hyphen) with
        medals for ranks 1–3 and the "1224" standard competition ranking for
        ties.
        """

        header = "Weekly pairs leaders board 🏆"
        if not entries:
            return f"{header}\n\nNo pairs configured."
        return f"{header}\n\n{self._format_ranking(entries)}"