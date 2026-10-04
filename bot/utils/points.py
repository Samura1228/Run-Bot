"""Points rules.

Implements the plan-based points model:

- Each user has a weekly **plan** (workouts/week) between :data:`MIN_PLAN` and
  :data:`MAX_PLAN`, defaulting to :data:`DEFAULT_PLAN`.
- Completing the plan yields ~:data:`STANDARD_POINTS_PER_WEEK` points/week: each
  workout up to the plan awards ``STANDARD_POINTS_PER_WEEK / plan`` points.
- Workouts logged **beyond** the plan award NOTHING: the plan is the target,
  and finishing it is what the week is scored on.

``ACTIVITY_POINTS`` / :func:`resolve_points` are retained only so the photo
handler can gate which activity types are awardable at all (running only); the
actual per-workout value now comes from :func:`workout_points`.
"""

from __future__ import annotations

# --- Plan-based model constants ------------------------------------------- #
STANDARD_WORKOUTS_PER_WEEK = 3
STANDARD_POINTS_PER_WEEK = 30
MIN_PLAN = 2
MAX_PLAN = 6
DEFAULT_PLAN = 3
# What a run beyond the plan is worth, as a share of the base rate. Zero: the
# plan is the goal, and extra runs are not paid for. Kept as a constant rather
# than deleting the branch so the rule stays one obvious number to change, and
# so the assistant's prompt can be generated from it.
OVERACHIEVEMENT_RATE = 0.0

# --- Bonus (non-running) activity model ----------------------------------- #
# Walking / cycling / strength are SEPARATE bonus points: they award a flat
# value once a minimum duration is met, and DO NOT count toward the running
# plan or overachievement (those remain running-only).
BONUS_ACTIVITY_POINTS = 5
# Minimum duration (in minutes) required to earn the flat bonus per activity.
ACTIVITY_MIN_MINUTES = {"walking": 40, "cycling": 60, "strength": 15}
# The set of bonus activity types (walking, cycling, strength).
BONUS_ACTIVITIES = set(ACTIVITY_MIN_MINUTES.keys())

# Friendly labels used in the ✅ success reply for each activity type.
_ACTIVITY_LABELS = {
    "running": "run",
    "walking": "walk",
    "cycling": "ride",
    "strength": "strength session",
}


def activity_label(activity_type: str) -> str:
    """Return a friendly word for an activity type, used in the reply text.

    running→"run", walking→"walk", cycling→"ride",
    strength→"strength session". Falls back to the raw type if unknown.
    """

    return _ACTIVITY_LABELS.get(activity_type, activity_type)

# Default awardable-activity table. ``main`` may override the running value at
# runtime from the ``POINTS_PER_RUN`` setting via :func:`build_activity_points`.
# NOTE: with the plan-based model the per-workout value is computed by
# :func:`workout_points`; this mapping now only gates which activity types are
# eligible at all (any non-zero entry means "awardable").
ACTIVITY_POINTS: dict[str, int] = {"running": 10}


def build_activity_points(points_per_run: int) -> dict[str, int]:
    """Return an activity->points mapping using the configured running value."""

    return {"running": points_per_run}


def resolve_points(activity_type: str, activity_points: dict[str, int]) -> int:
    """Return the (legacy) points for an activity type, or 0 if not awarded.

    Used only to gate awardable activity types (running only). The actual
    per-workout value comes from :func:`workout_points`.
    """

    return activity_points.get(activity_type, 0)


def clamp_plan(plan: int) -> int:
    """Clamp a plan value into the inclusive ``[MIN_PLAN, MAX_PLAN]`` range."""

    return max(MIN_PLAN, min(MAX_PLAN, plan))


def workout_points(plan: int, workouts_this_week_so_far: int) -> float:
    """Return the points for a single workout under the plan-based model.

    Args:
        plan: The user's weekly plan (workouts/week). Assumed already clamped.
        workouts_this_week_so_far: How many running workouts the user has
            ALREADY logged in the current week BEFORE this one.

    The base rate is ``STANDARD_POINTS_PER_WEEK / plan``. Workouts within the
    plan earn the base rate; workouts beyond it earn the base rate times
    :data:`OVERACHIEVEMENT_RATE`, which is ``0.0`` — so an extra run returns
    0.0 and the caller must say so rather than reporting "+0 points". The
    result is an EXACT fractional value (e.g. plan 4 → 7.5), rounded only to
    2 decimals to avoid float noise — it is NOT rounded to an integer.
    """

    base_rate = STANDARD_POINTS_PER_WEEK / plan
    if workouts_this_week_so_far < plan:
        pts = base_rate
    else:
        pts = base_rate * OVERACHIEVEMENT_RATE
    return round(pts, 2)


def format_points(p: float) -> str:
    """Format a point value for display, trimming trailing zeros/decimal point.

    Whole numbers show without a decimal (``15.0`` → ``"15"``) and fractional
    values show cleanly (``7.5`` → ``"7.5"``, ``3.75`` → ``"3.75"``). Values are
    treated with 2-decimal precision to match :func:`workout_points`.
    """

    text = f"{float(p):.2f}".rstrip("0").rstrip(".")
    # Guard against "-0" for negative-zero inputs.
    return text if text not in ("", "-0") else "0"
