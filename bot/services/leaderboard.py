"""Leaderboard service.

Aggregates points per user over a date range and formats weekly/monthly
leaderboard messages, plus the weekly **team** board (coach-created teams
competing on their members' combined weekly points).
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any, Protocol, Sequence

from bot.models import LeaderboardEntry, TeamEntry
from bot.services.sheets import SheetsService
from bot.utils.points import format_points

logger = logging.getLogger(__name__)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


class _Rankable(Protocol):
    """Minimal surface the shared renderer needs from a leaderboard row.

    Both :class:`~bot.models.LeaderboardEntry` (individual) and
    :class:`~bot.models.TeamEntry` (teams) satisfy it, so the SAME ranking /
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

    async def aggregate_teams(
        self,
        teams: Sequence[tuple[str, Sequence[int]]],
        start_date: date,
        end_date: date,
    ) -> list[TeamEntry]:
        """Aggregate combined points per team over a date range.

        Reuses :meth:`aggregate` verbatim (same ``read_rows_in_range`` data
        path, same season cutoff, same coach exclusion, same stored point
        values — no extra multipliers, legacy ``streak_bonus`` rows excluded),
        then sums each team's members. A member with no rows in the range
        contributes ``0`` and never drops the team.

        Note the sum is over whatever members the team actually has: with
        equal-sized teams that ranks identically to a per-member average, but
        an unequal team is advantaged — which is why :class:`TeamEntry` keeps
        ``size`` so the rendered line can show it.

        Returns teams sorted by points desc, then by name (lowercased) for a
        deterministic order within ties.
        """

        if not teams:
            logger.info("No teams configured; skipping team aggregation.")
            return []

        entries = await self.aggregate(start_date, end_date)
        by_user: dict[int, LeaderboardEntry] = {
            entry.telegram_user_id: entry for entry in entries
        }

        team_entries: list[TeamEntry] = []
        for name, member_ids in teams:
            total = 0.0
            for member_id in member_ids:
                entry = by_user.get(member_id)
                if entry is not None:
                    total += entry.points
            team_entries.append(
                TeamEntry(
                    name=name,
                    member_ids=tuple(member_ids),
                    points=round(total, 2),
                )
            )

        team_entries.sort(key=lambda e: (-e.points, e.label().lower()))
        return team_entries

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

    def format_teams(
        self,
        entries: list[TeamEntry],
        start_date: date,
        end_date: date,
        final: bool = True,
    ) -> str:
        """Format the team leaderboard for a round's date window.

        ``final`` picks the header: the closing board for the round, or the
        every-3-days standings while it is still running. The two must be
        visibly different — people should never mistake a mid-round snapshot
        for the result.

        Uses the SAME renderer as the individual boards, so lines read
        ``{team} ({size})  - {points} points`` (two spaces before the hyphen)
        with medals for ranks 1–3 and the "1224" standard competition ranking
        for ties. The size is rendered because the score is a SUM: with equal
        teams it changes nothing, and with unequal ones it makes the advantage
        visible instead of quietly unfair.
        """

        header = (
            "Team leaders board 🏆"
            if final
            else "Team standings (in progress) 🏆"
        )
        if not entries:
            return f"{header}\n\nNo teams configured."

        class _Sized:
            """Render shim: same points, label carries the member count."""

            def __init__(self, entry: TeamEntry) -> None:
                self.points = entry.points
                self._text = f"{entry.label()} ({entry.size})"

            def label(self) -> str:
                return self._text

        rows = [_Sized(entry) for entry in entries]
        return f"{header}\n\n{self._format_ranking(rows)}"
