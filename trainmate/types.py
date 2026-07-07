from typing import TypedDict, Optional

class Objective(TypedDict):
    """Represents a training goal or target event."""
    id: Optional[int]
    title: str
    target_date: str
    sport_type: str  # Can be a single sport or comma-separated list of sports
    description: Optional[str]
    priority: int
    status: str  # 'active', 'completed', 'archived'

class Constraint(TypedDict):
    """A single directive — anything the athlete asks the coach to work around, at
    any horizon (DESIGN_constraints.md §5).

    `binding` ('hard'|'soft'), `sport` (None = all sports) and `replan` (1 = escalated
    to plan-shaping) are read by deterministic code; `type` is an opaque
    user-vocabulary label (never branched on), `title`/`description` are prose for
    display and the LLM. `source` records authoring channel ('manual'|'message'|
    'lifeevent').
    """
    id: Optional[int]
    start_date: str
    end_date: str
    binding: str
    sport: Optional[str]
    type: Optional[str]
    title: str
    description: Optional[str]
    replan: int
    source: Optional[str]
    created: Optional[str]

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
    """Represents a single planned or synced workout."""
    id: Optional[int]
    date: str
    sport_type: str
    title: str
    description: Optional[str]
    original_description: Optional[str]
    pushed_signature: Optional[str]  # hash of calendar fields at last push; freshness derived (trainmate.calendar_state)
    modification_reason: Optional[str]  # non-None <=> modified; short per-workout note. Kind derived via trainmate.modification_state
    adaptation_summary: Optional[str]  # set <=> from `workout adapt`; long batch rationale
    google_event_id: Optional[str]  # set <=> a Calendar event exists (may be stale)
    duration_minutes: Optional[int]
    rpe: Optional[int]
    tss: Optional[int]
    original_date: Optional[str]
    removed: Optional[bool]
    removed_reason: Optional[str]
    source: Optional[str]  # origin, fixed at creation: 'generated'|'manual' (None = legacy)

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
    acute_workload: Optional[float]
    chronic_workload: Optional[float]
    acwr: Optional[float]
    # Performance Management Chart (DESIGN_pmc_fitness_fatigue.md §3). CTL = fitness
    # (42-day EWMA of load), ATL = fatigue (7-day EWMA), TSB = form = CTL(yesterday) -
    # ATL(yesterday). Suppressed at display inside the leading-edge warm-up window and
    # NULL on any pre-recompute row.
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
    """Represents a high-level periodized training macrocycle."""
    id: Optional[int]
    objective_id: int
    strategy: str
    goals_hash: str
    constraints_hash: str
    config_hash: Optional[str]
    created_at: str
    feedback: Optional[str]

class Mesocycle(TypedDict):
    """Represents a specific block/phase of training within a macrocycle."""
    id: Optional[int]
    macrocycle_id: int
    name: str
    start_date: str
    end_date: str
    focus: str
    feedback: Optional[str]
