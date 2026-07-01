"""Points rules.

``ACTIVITY_POINTS`` maps activity types to their point values. Only ``running``
is currently active; the mapping is intentionally extensible so other activity
types can be added later without changing the decision logic.
"""

from __future__ import annotations

# Default points table. ``main`` may override the running value at runtime from
# the ``POINTS_PER_RUN`` setting via :func:`build_activity_points`.
ACTIVITY_POINTS: dict[str, int] = {"running": 10}


def build_activity_points(points_per_run: int) -> dict[str, int]:
    """Return an activity->points mapping using the configured running value."""

    return {"running": points_per_run}


def resolve_points(activity_type: str, activity_points: dict[str, int]) -> int:
    """Return the points for an activity type, or 0 if it is not awarded."""

    return activity_points.get(activity_type, 0)