"""Leaderboard service.

Aggregates points per user over a date range and formats weekly/monthly
leaderboard messages.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

from bot.models import LeaderboardEntry
from bot.services.sheets import SheetsService
from bot.utils.points import format_points

logger = logging.getLogger(__name__)

_MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}


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

    @staticmethod
    def _format_ranking(entries: list[LeaderboardEntry]) -> str:
        """Render one line per entry.

        Each line is ``{name}  - {points} points`` (note the two spaces
        before the hyphen, per the requested layout) with a trailing medal
        for ranks 1–3 and no trailing emoji for ranks 4+.
        """

        lines: list[str] = []
        for rank, entry in enumerate(entries, start=1):
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