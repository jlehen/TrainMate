from typing import Any, Dict, List, TypedDict, Optional

class Objective(TypedDict):
    """Represents a training goal or target event."""
    id: Optional[int]
    title: str
    target_date: str
    sport_type: str  # Can be a single sport or comma-separated list of sports
    description: Optional[str]
    # Called off or not. 'completed' is derived from target_date, never stored: see
    # db.objectives.goal_state() and DESIGN_backward_evaluation.md §12.
    status: str  # 'active' | 'archived'
    # Whether target_date is a scheduled event or just how far the athlete wants to
    # train toward the goal (ARCHITECTURE.md §15 "Goal dates").
    date_type: str  # 'event' | 'horizon'

class Constraint(TypedDict):
    """A single directive — anything the athlete asks the coach to work around, at
    any horizon (DESIGN_constraints.md §5).

    `rest` (1 = a deterministic full no-training window, the one enforced edge) and
    `replan` (1 = escalated to plan-shaping) are read by deterministic code;
    `title`/`description` are advisory prose for display and the LLM. `source` records
    authoring channel ('manual'|'message'|'lifeevent').
    """
    id: Optional[int]
    start_date: str
    end_date: str
    rest: int
    title: str
    description: Optional[str]
    replan: int
    source: Optional[str]
    created: Optional[str]
    # When a coach pass last had this constraint in scope with authority over every day
    # of it still ahead — NULL = the plan does not reflect it yet
    # (DESIGN_constraint_reschedule.md §8). Declared last: the column is appended by an
    # ALTER, and test_types.py pins declaration order against PRAGMA table_info.
    honored_at: Optional[str]

class DailyContext(TypedDict):
    """An external daily context signal ingested from a tagged Calendar event.

    TrainMate is domain-agnostic about these: `metric` is an opaque category
    (e.g. 'alcohol'), `value` an optional numeric magnitude, `text` the human
    blurb shown to the coach. `google_event_id` reconciles edits/deletes.
    """
    id: Optional[int]
    date: str
    metric: str
    value: Optional[float]
    text: Optional[str]
    google_event_id: str
    updated: Optional[str]

class Workout(TypedDict):
    """One planned session, as `get_workouts()` returns it.

    Not a table row: `workouts` is an append-only revision log, and this is the live
    revision hydrated with what its lineage derives — `original_*`, the adaptation tally,
    `source` (DESIGN_workout_revisions.md §5). tests/test_types.py pins these keys against
    a session written and read back, so a new key must be declared here in the same commit
    that starts hydrating it.
    """
    id: Optional[int]  # the LINEAGE id: stable session identity, what `rm`/`swap` address
    revision_id: Optional[int]  # the physical row; only history surfaces read it
    date: str
    sport_type: str
    title: str
    description: Optional[str]
    original_description: Optional[str]  # the lineage's first revision
    pushed_signature: Optional[str]  # hash of calendar fields at last push; freshness derived (trainmate.calendar_state)
    adherence_pushed_signature: Optional[str]  # calendar fields + verdict at the last `compare --mark`
    modification_reason: Optional[str]  # this revision's note; NULL on a void, where the note is removed_reason
    adaptation_summary: Optional[str]  # the batch rationale of the change that appended this revision
    change_kind: Optional[str]  # what kind of change this session's live form is (§7); replaces modification_state
    google_event_id: Optional[str]  # set <=> a Calendar event exists (may be stale)
    removed: Optional[bool]  # the live revision is a void: no session this day (§3)
    removed_reason: Optional[str]  # this revision's note, when it is a void
    source: Optional[str]  # 'manual' <=> the lineage was started by `workout add`, else 'generated'
    duration_minutes: Optional[int]
    rpe: Optional[int]
    tss: Optional[int]
    macrocycle_id: Optional[int]  # plan version this session belongs to
    original_date: Optional[str]  # where the lineage's first revision put it
    created_at: Optional[str]  # when the SESSION entered the plan, carried across revisions
    adapted_at: Optional[str]  # the newest standing easing, walked over the lineage (§7)
    adaptation_count: Optional[int]  # how many easings still describe this form (§7)
    original_duration_minutes: Optional[int]  # the lineage's first revision
    original_tss: Optional[int]
    original_rpe: Optional[int]
    benchmark_type: Optional[str]  # set <=> a fitness test; creation-time intent (DESIGN_benchmark_workouts.md §3.1)
    # Planned intensity distribution, authored with the session and graded against the
    # activity's recorded zones (DESIGN_intensity_distribution.md). `currency` names the
    # scale the seconds are expressed in ('hr' | 'power').
    planned_zone_currency: Optional[str]
    planned_zone1_sec: Optional[int]
    planned_zone2_sec: Optional[int]
    planned_zone3_sec: Optional[int]
    planned_zone4_sec: Optional[int]
    planned_zone5_sec: Optional[int]
    planned_zone6_sec: Optional[int]
    planned_zone7_sec: Optional[int]

class CompletedActivity(TypedDict):
    """Represents a completed Garmin activity synced from Sheets."""
    activity_id: str
    date: str
    start_time: Optional[str]
    activity_name: Optional[str]
    activity_type: str
    duration_sec: float
    distance_km: float
    elevation_gain_m: float
    avg_hr: Optional[int]
    max_hr: Optional[int]
    rpe: int
    tss: float
    bike_avg_watts: Optional[int]
    zone1_sec: Optional[int]
    zone2_sec: Optional[int]
    zone3_sec: Optional[int]
    zone4_sec: Optional[int]
    zone5_sec: Optional[int]
    power_zone1_sec: Optional[int]
    power_zone2_sec: Optional[int]
    power_zone3_sec: Optional[int]
    power_zone4_sec: Optional[int]
    power_zone5_sec: Optional[int]
    power_zone6_sec: Optional[int]
    power_zone7_sec: Optional[int]

class AthleteMetric(TypedDict):
    """Represents Garmin health/performance metrics cached for a specific day."""
    date: str
    rhr: Optional[int]
    hrv: Optional[int]
    sleep_score: Optional[int]
    stress: Optional[int]
    # Performance Management Chart (DESIGN_pmc_fitness_fatigue.md §3). CTL = fitness
    # (42-day EWMA of load), ATL = fatigue (7-day EWMA), TSB = form = CTL(yesterday) -
    # ATL(yesterday). Suppressed at display inside the leading-edge warm-up window and
    # NULL on any pre-recompute row. The ATL/CTL load ratio derives from these at read
    # time (garmin.pmc.load_ratio) rather than being stored.
    ctl: Optional[float]
    atl: Optional[float]
    tsb: Optional[float]

class AthleteBaseline(TypedDict):
    """Represents rolling baseline stats calculated for an athlete."""
    date: str
    rhr_baseline_mean: float
    rhr_baseline_std: float
    hrv_baseline_mean: float
    hrv_baseline_std: float
    sleep_baseline_mean: float
    sleep_baseline_std: float

class Macrocycle(TypedDict):
    """Represents a high-level periodized training macrocycle.

    The `*_hash` fields fingerprint the inputs the strategy was generated from, and the
    matching `*_snapshot` fields keep the inputs themselves so a stale plan can say what
    actually changed. `status`/`superseded_at` carry the rollback axis: regenerating
    supersedes the previous version rather than deleting it (DESIGN_plan_rollback.md).
    """
    id: Optional[int]
    objective_id: int
    strategy: str
    goals_hash: str
    constraints_hash: str
    config_hash: Optional[str]
    config_snapshot: Optional[str]
    profile_snapshot: Optional[str]
    goals_snapshot: Optional[str]
    constraints_snapshot: Optional[str]
    all_constraints_snapshot: Optional[str]
    created_at: str
    status: Optional[str]  # 'active' | 'superseded'
    superseded_at: Optional[str]

class Mesocycle(TypedDict):
    """Represents a specific block/phase of training within a macrocycle."""
    id: Optional[int]
    macrocycle_id: int
    name: str
    start_date: str
    end_date: str
    focus: str

class PlanFeedback(TypedDict):
    """One note in a plan's feedback log — a message the athlete addressed to the next
    plan version, written while this one is in force (DESIGN_plan_feedback.md §2).

    `mesocycle_id` None = the note addresses the plan as a whole. `mesocycle_name` is
    not a column: `list_plan_feedback` joins it in, because a phase NAME survives the
    version churn its ID does not (§7).
    """
    id: Optional[int]
    macrocycle_id: int
    mesocycle_id: Optional[int]
    created_at: str  # ISO-8601 UTC, full precision
    text: str
    mesocycle_name: Optional[str]

class PlanProposal(TypedDict):
    """What `plan generate` produced, before the athlete has accepted it.

    `goal` is the objective the plan belongs to — the requested one, or the next active
    goal when none was named. It is `None` only when there are no active goals at all.
    """
    strategy: str
    mesocycles: List[Dict[str, Any]]
    reused: bool
    goal: Optional[Objective]
