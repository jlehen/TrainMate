import os
import yaml
from typing import Any, Dict, List, Optional
from trainmate.signals import DEFAULT_SIGNAL_METRICS, normalize_metric

# Which config file this process runs against. TRAINMATE_CONFIG selects one explicitly —
# that is how a second athlete runs from the same checkout (ARCHITECTURE.md §9); the
# default is config.yaml at the repo root. Relative paths written in the file
# (`database:`, `science_dir:`, `service_account_file`) resolve against the config file's
# directory — or against `data_dir:` when that key is set, which moves the whole
# instance's state under one directory without repeating the prefix on every path key.
# Either way an instance's state lives beside its config, never beside the code.
_DEFAULT_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")
CONFIG_PATH = os.path.abspath(
    os.path.expanduser(os.environ.get("TRAINMATE_CONFIG") or _DEFAULT_CONFIG_PATH))
CONFIG_DIR = os.path.dirname(CONFIG_PATH)

# Fallback when `llm.models` is missing or empty.
DEFAULT_LLM_MODEL = "google/gemini-3.5-flash"

class Config:
    """Manages application settings loaded from config.yaml and env variables."""

    def __init__(self) -> None:
        """Initializes the configuration store from the config file if it exists.

        A file named via TRAINMATE_CONFIG must exist and parse: the env var picks which
        athlete's data (database, Garmin account) this process touches, so a typo must
        abort rather than fall back to defaults that point at the primary athlete's
        database. The default path stays lenient — a bare checkout must still run.
        """
        self.data: dict[str, Any] = {}
        explicit = bool(os.environ.get("TRAINMATE_CONFIG"))
        if not os.path.exists(CONFIG_PATH):
            if explicit:
                raise SystemExit(f"TRAINMATE_CONFIG names a missing file: {CONFIG_PATH}")
            return
        try:
            with open(CONFIG_PATH, "r") as f:
                self.data = yaml.safe_load(f) or {}
        except Exception as e:
            if explicit:
                raise SystemExit(f"TRAINMATE_CONFIG file failed to load: {CONFIG_PATH} ({e})")
            print(f"Warning: Failed to load config.yaml: {e}")

    def get(self, key: str, default: Any = None) -> Any:
        """Retrieves a configuration value by key with an optional default.

        Args:
            key: The configuration option name.
            default: Fallback value if key is not found or is None.

        Returns:
            The configuration value.
        """
        val = self.data.get(key)
        return val if val is not None else default

    def raw(self, *path: str) -> Any:
        """The literal value at a nested key path, or None when any level is absent.

        `get` cannot tell "absent" from "set to the default"; a setting that reports where
        its value came from has to (DESIGN_settings.md §3)."""
        node: Any = self.data
        for step in path:
            if not isinstance(node, dict):
                return None
            node = node.get(step)
            if node is None:
                return None
        return node

    @property
    def openrouter_api_key(self) -> Optional[str]:
        """Gets OpenRouter API Key. Prefers env variable over config.json."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key or key == "YOUR_OPENROUTER_API_KEY":
            key = self.get("llm", {}).get("api_key")
        return key

    @property
    def google_calendar_id(self) -> Optional[str]:
        """Gets the target Google Calendar ID for training events."""
        return self.get("google", {}).get("calendar_id")

    @property
    def llm_models(self) -> list[str]:
        """The OpenRouter models this install may use, in `model list` display order
        (DESIGN_model_selection.md §1). Entries are either a bare identifier or a
        `model:` mapping, so a list can carry per-entry keys later without a reformat.
        An absent/empty `llm.models` yields the built-in default alone, so a bare
        config still runs."""
        models = []
        for entry in self.get("llm", {}).get("models") or []:
            identifier = entry.get("model") if isinstance(entry, dict) else entry
            if identifier and str(identifier).strip():
                models.append(str(identifier).strip())
        return models or [DEFAULT_LLM_MODEL]

    @property
    def default_llm_model(self) -> str:
        """The model used when nothing is stored in the database: first of `llm.models`."""
        return self.llm_models[0]

    @property
    def llm_request_timeout(self) -> int:
        """Seconds an OpenRouter HTTP request may run before it's aborted
        (openrouter.py). Generation is slow — the old hard-coded 45s tripped on
        large plans — so the default is generous. Under `llm:`. Default 120."""
        return int(self.get("llm", {}).get("request_timeout_seconds", 120))

    @property
    def data_dir(self) -> str:
        """Base directory every relative path key resolves against: top-level `data_dir:`
        key, itself resolved against the config file's directory; absent, the config
        file's directory itself — the pre-`data_dir` rule, unchanged (ARCHITECTURE.md §9)."""
        prefix = self.get("data_dir")
        if not prefix:
            return CONFIG_DIR
        prefix = os.path.expanduser(str(prefix))
        return prefix if os.path.isabs(prefix) else os.path.join(CONFIG_DIR, prefix)

    def _resolve(self, path: str) -> str:
        """The one resolution rule for path keys: ~ expands, an absolute path is
        respected as written, a relative one lands in `data_dir` (ARCHITECTURE.md §9)."""
        path = os.path.expanduser(str(path))
        return path if os.path.isabs(path) else os.path.join(self.data_dir, path)

    @property
    def service_account_file(self) -> str:
        """Gets the service account file path. A relative path resolves against the
        instance's `data_dir` (the CONFIG_PATH rule above)."""
        return self._resolve(
            self.get("google", {}).get("service_account_file", "service_account.json"))

    @property
    def db_path(self) -> str:
        """The SQLite file this instance operates on: top-level `database:` key, default
        trainmate.db. A relative value resolves against the instance's `data_dir`, so a
        TRAINMATE_CONFIG instance cannot silently open another instance's database."""
        return self._resolve(self.get("database") or "trainmate.db")

    @property
    def science_dir(self) -> str:
        """The athlete's own sports-science guidelines: top-level `science_dir:` key,
        default science/. A relative value resolves against the instance's `data_dir`,
        so a TRAINMATE_CONFIG instance gets its own training philosophy rather than
        inheriting the primary athlete's (ARCHITECTURE.md §9)."""
        return self._resolve(self.get("science_dir") or "science")

    @property
    def logging_dir(self) -> str:
        """Root of the operator-facing logs — the run journal and the LLM exchanges
        (DESIGN_logging.md §6). Top-level `logging.dir` key, default logs/.

        A relative value resolves against the instance's `data_dir`, like `database:`
        and `science_dir:`, so a TRAINMATE_CONFIG instance keeps its logs beside its own
        config instead of interleaving them with the primary athlete's."""
        return self._resolve(self.get("logging", {}).get("dir") or "logs")

    @property
    def llm_logs_dir(self) -> str:
        """Gets the directory where LLM interaction logs are stored."""
        return os.path.join(self.logging_dir, "llm_exchanges")

    @property
    def logging_level(self) -> str:
        """Lowest level that reaches the journal file: debug|info|warn|error, default
        info. `debug` turns on the swallowed-exception tier (DESIGN_logging.md §9)."""
        return str(self.get("logging", {}).get("level", "info")).strip().lower()

    @property
    def logging_retain_days(self) -> int:
        """Days of `logs/runs/*.jsonl` kept by the daily sweep. Default 90."""
        return int(self.get("logging", {}).get("retain_days", 90))

    @property
    def logging_retain_exchange_days(self) -> int:
        """Days of `logs/llm_exchanges/*.md` kept by the daily sweep. Default 90 —
        the model-comparison work reads old exchanges, and a benchmark you cannot
        re-read is one you have to re-run (DESIGN_logging.md §10)."""
        return int(self.get("logging", {}).get("retain_exchange_days", 90))

    @property
    def app_science_dir(self) -> str:
        """Gets the internal application directory containing default sports science guidelines."""
        return os.path.join(os.path.dirname(__file__), "science")

    @property
    def user_profile(self) -> dict[str, Any]:
        """Gets the user profile information dict.

        Trainable thresholds (`ftp`/`lthr`) no longer live here — they moved to the
        benchmark logbook (DESIGN_benchmark_workouts.md §3.4), which is the only home for
        measured, trainable quantities. A fresh install therefore has no threshold on
        record and the app nudges rather than refuses (§3.4 cold start); config keeps only
        quasi-fixed physiology (`max_hr`) and life logistics.
        """
        return self.get("user_profile", {})

    @property
    def metrics_lookback_days(self) -> int:
        """Gets the number of days of metrics history to look at, defaulting to 15."""
        return self.get("coach", {}).get("metrics_lookback_days", 15)

    @property
    def adapt_terminal_window_days(self) -> int:
        """Gets how close to a block's end counts as its terminal window, defaulting to 3.

        See DESIGN_block_boundary.md §3/§4 — inside this window an adaptation has no runway
        to rebound, and the next block is out of reach.
        """
        return self.get("coach", {}).get("adapt_terminal_window_days", 3)

    @property
    def goals_lookback_days(self) -> int:
        """Gets the number of days into the past to look for preceding goals, defaulting to 90."""
        return self.get("coach", {}).get("goals_lookback_days", 90)

    @property
    def workout_generation_span_days(self) -> int:
        """Gets the default number of days to generate workouts for, defaulting to 28."""
        raw = self.get("coach", {}).get("workout_generation_span_days", 28)
        return int(raw)

    @property
    def replan_displaced_load_pct(self) -> float:
        """Displaced-load trigger for the §7 replan proposal: propose a replan when a
        constraint's overlapping planned load is at least this percentage of the plan's
        trailing weekly planned load (default 50 — half a typical week)."""
        return float(self.get("coach", {}).get("replan_displaced_load_pct", 50))

    @property
    def threshold_replan_pct(self) -> float:
        """Relative drift (percent) a physiological threshold (max_hr/lthr/ftp) may
        move from the value the active plan was generated with before the plan is
        flagged stale (coach/service.config_changed). Small retest corrections flow
        into workout targets without invalidating the periodization strategy; a
        genuine fitness shift past this band suggests a replan (default 5)."""
        return float(self.get("coach", {}).get("threshold_replan_pct", 5.0))

    @property
    def replan_rest_span_days(self) -> int:
        """Rest-window floor for the §7 replan proposal: a `rest` constraint spanning at
        least this many days is intrinsically plan-shaping regardless of displaced load
        (default 3). Falls back to the pre-rev-6 name `replan_hard_span_days`."""
        coach = self.get("coach", {})
        return int(coach.get("replan_rest_span_days",
                             coach.get("replan_hard_span_days", 3)))

    @property
    def learning_confidence_thresholds(self) -> dict[str, int]:
        """Distinct net supporting weeks required to reach each confidence level
        (evidence-based confidence; see DESIGN_evidence_based_confidence.md §3).

        Only `moderate` and `established` are tunable; `tentative` is always >=1 and
        proposed-retirement always <=0. Tuning the map re-levels learnings on the next
        recompute, with no migration.
        """
        raw = self.get("learning_confidence_thresholds") or {}
        return {
            "moderate": int(raw.get("moderate", 3)),
            "established": int(raw.get("established", 5)),
        }

    @property
    def minor_activity_load_threshold(self) -> float:
        """Gets the workload score below which an activity is considered minor (default 25).

        Used to: (1) widen mismatch tolerance to 50% for matched activities, and
        (2) display unplanned activities as '(minor)' rather than 'UNPLANNED'.
        """
        return float(self.get("coach", {}).get("minor_activity_load_threshold", 25.0))

    @property
    def rpe_divergence_ratio(self) -> float:
        """Ratio of RPE-implied load to measured (power/HR) load above which a
        session is flagged 'felt harder than it measured' and its load taken from
        RPE instead (garmin.py). Under `coach:`. Default 1.5; set very high to
        disable."""
        return float(self.get("coach", {}).get("rpe_divergence_ratio", 1.5))

    @property
    def adherence_tolerance(self) -> dict[str, float]:
        """Bounds of the dynamic +/- tolerance band for the duration/workload
        adherence comparison (adherence.py): `easy_pct` at planned loads <=
        `low_load`, `hard_pct` at loads >= `high_load`, linearly interpolated
        between. Looser for easy sessions (small absolute swings read large in %),
        tighter for hard ones. Under `coach:`."""
        raw = self.get("coach", {}).get("adherence_tolerance") or {}
        return {
            "easy_pct": float(raw.get("easy_pct", 0.50)),
            "hard_pct": float(raw.get("hard_pct", 0.15)),
            "low_load": float(raw.get("low_load", 20.0)),
            "high_load": float(raw.get("high_load", 100.0)),
        }

    @property
    def learning_staleness_days(self) -> dict[str, int]:
        """Days a learning may go unreinforced before it goes dormant and earns a
        one-level staleness demotion, per confidence level (db/learnings.py). The
        tunable sibling of `learning_confidence_thresholds`. Overridable via the
        top-level `learning_staleness_days` map."""
        raw = self.get("learning_staleness_days") or {}
        return {
            "tentative": int(raw.get("tentative", 21)),
            "moderate": int(raw.get("moderate", 60)),
            "established": int(raw.get("established", 180)),
        }

    @property
    def analysis_staleness_days(self) -> int:
        """Days the cached backward-evaluation reconstruction may lag today before
        `plan generate` says so (DESIGN_backward_evaluation.md §5). The cache only
        refreshes on `data bootstrap`/`data reflect`, so nothing else would."""
        return int(self.get("analysis_staleness_days", 14))

    # --- Garmin direct-pull knobs (see DESIGN_garmin_direct_pull.md §13) ---
    @property
    def garmin_email(self) -> Optional[str]:
        """Garmin Connect login email. Read from config.yaml only — credentials are
        kept out of the environment (config.yaml is gitignored)."""
        return self.get("garmin", {}).get("email")

    @property
    def garmin_password(self) -> Optional[str]:
        """Garmin Connect password. Read from config.yaml only (see garmin_email)."""
        return self.get("garmin", {}).get("password")

    @property
    def garmin_token_dir(self) -> str:
        """Directory where garminconnect persists OAuth tokens: `garmin.token_dir`,
        default .garminconnect beside the config file (under `data_dir:` when set,
        like every other path key) — configs sharing a directory must set it apart,
        or the second silently resumes the first account's session
        (DESIGN_garmin_direct_pull.md §11)."""
        return self._resolve(self.get("garmin", {}).get("token_dir") or ".garminconnect")

    @property
    def data_refresh_minutes(self) -> int:
        """Minimum minutes before an automatic refresh re-pulls — gating both Garmin hits
        and Calendar-signal syncs; within this window reads reuse the local cache
        (top-level `refresh_minutes`, default 120)."""
        return int(self.get("refresh_minutes", 120))

    @property
    def garmin_mutable_days(self) -> int:
        """Trailing days a forward-refresh re-fetches for late-finalizing data (default 3)."""
        return int(self.get("garmin", {}).get("mutable_days", 3))

    @property
    def garmin_backfill_prompt_days(self) -> int:
        """Backward-gap size above which a backfill is surfaced as a command rather than
        run automatically; also gates interior gaps (default 30)."""
        return int(self.get("garmin", {}).get("backfill_prompt_days", 30))

    @property
    def garmin_initial_backfill_days(self) -> int:
        """Range used to build the cold-start backfill command (default 90)."""
        return int(self.get("garmin", {}).get("initial_backfill_days", 90))

    @property
    def garmin_throttle_seconds(self) -> float:
        """Default sleep between Garmin API calls; --sleep overrides on `data pull` (default 0.2)."""
        return float(self.get("garmin", {}).get("throttle_seconds", 0.2))

    @property
    def hr_zone_coverage_min(self) -> float:
        """Minimum fraction of an activity's duration that must fall inside a known
        HR zone before hrTSS is trusted (garmin.py); below it, effort sat under
        zone 1 and hrTSS undercounts, so RPE is preferred. Under `garmin:`.
        Default 0.5 — calibrated against the activity history, where genuine aerobic
        sessions cluster at >=0.77 coverage and low-intensity ones at <0.3, with a
        clean gap at 0.5."""
        return float(self.get("garmin", {}).get("hr_zone_coverage_min", 0.5))

    @property
    def zone_min_activity_minutes(self) -> float:
        """Shortest activity that may raise an undercount marker in the progress tables
        (DESIGN_intensity_distribution.md §11). Under `garmin:`. Default 20.

        A 5-minute mobility session with a cold strap is not evidence that a 340-TSS
        week is undercounted, and it is not evidence about recording quality either —
        it is too small to carry either claim. Sessions below this still contribute
        their load and their zone minutes; they simply get no vote on the markers."""
        return float(self.get("garmin", {}).get("zone_min_activity_minutes", 20))

    @property
    def zone_coverage_display_min(self) -> float:
        """Default share of a sport's duration that must land in a known zone before its
        weekly row stops marking itself incomplete. Under `garmin:`. Default 0.8.

        Deliberately NOT `hr_zone_coverage_min` (0.5), which is a "safe to compute load
        from" bar. Per-sport overrides live in
        `intensity.COVERAGE_MIN_BY_SPORT`; `zone_coverage_display_min_by_sport`
        overrides those in turn."""
        return float(self.get("garmin", {}).get("zone_coverage_display_min", 0.8))

    @property
    def zone_coverage_display_min_by_sport(self) -> Dict[str, float]:
        """Per-canonical-sport overrides of the bar above, keyed by canonical sport name.
        Under `garmin:`. Empty by default — the shipped per-sport table in
        `intensity.COVERAGE_MIN_BY_SPORT` applies unless overridden here."""
        raw = self.get("garmin", {}).get("zone_coverage_display_min_by_sport") or {}
        return {str(k).strip().lower(): float(v) for k, v in raw.items()}

    # --- PMC time constants, config-backed under `garmin:`. Non-default values are
    # experimental; calibration caveat in config_template_full.yaml and
    # DESIGN_pmc_fitness_fatigue.md §3.4. ---
    def _load_window_days(self, key: str, default: int) -> int:
        """A `garmin:` load time-constant, validated positive — both are EWMA divisors,
        so a non-positive value would crash recompute or store nonsense. Fail loud at
        read time."""
        v = int(self.get("garmin", {}).get(key, default))
        if v <= 0:
            raise ValueError(
                f"config garmin.{key} must be a positive number of days, got {v}"
            )
        return v

    @property
    def pmc_ctl_days(self) -> int:
        """CTL (fitness) EWMA time constant τ, in days — the classic 42-day Coggan
        constant (garmin.py, DESIGN_pmc_fitness_fatigue.md §3). Under `garmin:`. Default 42."""
        return self._load_window_days("pmc_ctl_days", 42)

    @property
    def pmc_atl_days(self) -> int:
        """ATL (fatigue) EWMA time constant τ, in days — the classic 7-day Coggan
        constant (garmin.py). Under `garmin:`. Default 7."""
        return self._load_window_days("pmc_atl_days", 7)

    @property
    def calendar_signal_tag(self) -> str:
        """Gets the source tag for calendar events. The default keeps the pre-rename
        string: it is written into events the external syncer also produces, so changing
        it orphans every event already tagged (DESIGN_calendar_signal_ingest.md §4)."""
        return self.get("google", {}).get("calendar_signal_tag", "trainmate-context")

    @property
    def high_intensity_rpe_threshold(self) -> int:
        """Gets the RPE threshold above which an activity is considered high intensity."""
        return int(self.get("coach", {}).get("high_intensity_rpe_threshold", 8))

    @property
    def high_intensity_tss_threshold(self) -> float:
        """Gets the TSS threshold above which an activity is considered high intensity."""
        return float(self.get("coach", {}).get("high_intensity_tss_threshold", 120.0))

    @property
    def signal_days_lookahead(self) -> int:
        """The look-ahead `k` for quantitative signal-impact alignment: how many mornings
        bracket each signal episode (before and after) and the drink-free gap below which
        two signal runs merge into one episode (DESIGN_quantitative_signal_impact.md §3,
        §3.0). Default 3."""
        return int(self.get("coach", {}).get("signal_days_lookahead", 3))

    @property
    def signal_days_min_days(self) -> int:
        """Minimum total signal-days a signal category must have before its aligned rows
        are shown to the coach at all — a floor against prompting on one stray night
        (DESIGN_quantitative_signal_impact.md §5). Counts signal-days, not episodes.
        Default 1 (show whatever exists; the LLM judges from the visible count)."""
        return int(self.get("coach", {}).get("signal_days_min_days", 1))

    @property
    def signal_metrics(self) -> Dict[str, str]:
        """Daily-signal categories offered to the coach, as `metric -> gloss`. Under
        `coach:`. Augments `signals.DEFAULT_SIGNAL_METRICS` rather than replacing it, and
        a config entry reusing a shipped name overrides that gloss
        (DESIGN_signal_extraction.md §5). Keys are normalized, so `Heat` and `heat` are
        one category. Suggested, never enforced: `signal add` and the coach may both use
        a category outside this list."""
        merged = dict(DEFAULT_SIGNAL_METRICS)
        raw = self.get("coach", {}).get("signal_metrics") or {}
        for name, gloss in raw.items():
            key = normalize_metric(name)
            if not key:
                continue
            merged[key] = str(gloss or "").strip()
        return merged

    @property
    def telegram_bot_token(self) -> Optional[str]:
        """Telegram bot token used by the chat front-end (trainmate_bot.py).

        Prefers the TELEGRAM_BOT_TOKEN env variable over config.yaml so the secret
        can be injected at runtime; returns None when neither is set."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        if not token:
            token = self.get("telegram", {}).get("bot_token")
        return token or None

    @property
    def telegram_allowed_chat_ids(self) -> list[int]:
        """Telegram chat IDs allowed to drive the bot — the single-user allowlist.

        An empty list means *no one* is authorized (the bot refuses every message);
        the operator must add their own chat id. Non-integer entries are ignored."""
        raw = self.get("telegram", {}).get("allowed_chat_ids") or []
        ids = []
        for entry in raw:
            try:
                ids.append(int(entry))
            except (TypeError, ValueError):
                continue
        return ids

    @property
    def telegram_command_timeout(self) -> int:
        """Seconds a single bot-dispatched CLI command may run before it's killed.

        LLM-backed commands (plan/workout generate) are slow, so the default is
        generous. Default 180."""
        return int(self.get("telegram", {}).get("command_timeout_seconds", 180))

    @property
    def telegram_prompt_timeout(self) -> int:
        """Seconds the bot waits for the athlete to answer an interactive prompt
        (tap a button or send text) before it cancels the command and kills the
        parked CLI process. Default 300 (5 min)."""
        return int(self.get("telegram", {}).get("prompt_timeout_seconds", 300))

    @property
    def telegram_wrap_width(self) -> int:
        """Column width the CLI wraps prose to when driven by the bot (via the
        TRAINMATE_WRAP_WIDTH env var). The CLI's terminal default is 80, which a
        phone-width monospace block then double-wraps; ~48 fits portrait without
        the client re-wrapping. Default 48."""
        return int(self.get("telegram", {}).get("wrap_width", 48))

    @property
    def telegram_ui(self) -> str:
        """Which persona the Telegram bot presents (DESIGN_bot_simple_frontend.md §3):
        'expert' (default) is the raw CLI-over-chat; 'simple' adds the reply keyboard,
        free-text router, morning push and simple rendering. Under `telegram:`."""
        return str(self.get("telegram", {}).get("ui", "expert")).strip().lower()

    # --- Web front-end (trainmate_web.py) ---
    @property
    def web_host(self) -> str:
        """Interface the Flask web UI binds to. Defaults to loopback (127.0.0.1)
        so the dev server isn't exposed on all interfaces; set to 0.0.0.0 under
        `web:` to serve the LAN. Under `web:`."""
        return self.get("web", {}).get("host", "127.0.0.1")

    @property
    def web_port(self) -> int:
        """TCP port the web UI listens on. Under `web:`. Default 5000."""
        return int(self.get("web", {}).get("port", 5000))

    @property
    def web_debug(self) -> bool:
        """Whether Flask runs with the Werkzeug debugger/reloader. Defaults to
        False — the interactive debugger is a remote-code-execution vector on any
        traceback and must never be on for a reachable bind. Under `web:`."""
        return bool(self.get("web", {}).get("debug", False))

# Singleton instance
config = Config()


# Threshold anchors are excluded from the config fingerprint: they anchor per-workout zone
# targets (recomputed from live values at every generation), not the phase structure. They
# are snapshotted on the macrocycle and only flag the plan stale past a relative drift
# tolerance (`coach.threshold_replan_pct`) — see service.config_changed() /
# service.effective_thresholds(). Post-DESIGN_benchmark_workouts §3.4 only `max_hr` still
# lives in config (lthr/ftp moved to the benchmark logbook); the other two names are kept
# here so any legacy config that still carries them is excluded from the hash.
PROFILE_THRESHOLD_FIELDS = ('max_hr', 'lthr', 'ftp')

# Profile fields that reach every prompt but cannot shape the *periodization*, so editing
# one must not flag the plan stale (DESIGN_plan_staleness.md §3).
PROFILE_NON_PLAN_FIELDS = ('name', 'equipment')

# Per-day `weekly_schedule` sub-keys that shape individual sessions but not the block
# structure — swapping a day's kit changes what that day is, not the periodization
# (DESIGN_plan_staleness.md §4). The day's hours/max_sessions/certainty_percent stay in.
SCHEDULE_NON_PLAN_KEYS = ('equipment',)


def plan_profile() -> Dict[str, Any]:
    """The user_profile fields that shape the periodization strategy.

    Single source of truth for the config_hash fingerprint. The partition and its
    rationale are DESIGN_plan_staleness.md §3–§4: thresholds are tolerance-checked
    separately (see above), `name`/`equipment` and each day's `equipment` are excluded as
    not plan-shaping, and everything else — availability, target hours, preferences,
    injuries, sports — is. The exclusions are a denylist so a profile field added later
    counts as plan-shaping until someone decides otherwise (§6).

    Deliberately NOT fingerprinted: prompt-context knobs such as
    `coach.metrics_lookback_days`, which change what the coach *sees*, not what the plan
    should be.
    """
    excluded = set(PROFILE_THRESHOLD_FIELDS) | set(PROFILE_NON_PLAN_FIELDS)
    profile = {k: v for k, v in config.user_profile.items() if k not in excluded}
    schedule = profile.get('weekly_schedule')
    if isinstance(schedule, dict):
        profile['weekly_schedule'] = {
            day: (
                {k: v for k, v in spec.items() if k not in SCHEDULE_NON_PLAN_KEYS}
                if isinstance(spec, dict) else spec
            )
            for day, spec in schedule.items()
        }
    return profile


def changed_plan_profile_fields(old_profile: Dict[str, Any]) -> List[str]:
    """The plan-shaping profile fields that differ between `old_profile` (a snapshot taken
    at plan generation) and the live config, so staleness can say *what* moved.

    Names a field whether it was added, removed, or edited — the athlete needs to know
    which input to look at, not which of the three happened to it (DESIGN_plan_staleness.md
    §5)."""
    current = plan_profile()
    return sorted(
        k for k in set(old_profile) | set(current)
        if old_profile.get(k) != current.get(k)
    )


def plan_config_hash() -> str:
    """Hash of the plan-shaping user config (see `plan_profile`).

    Lives here, beside the config it fingerprints, rather than on the coaching engine:
    the read-only web dashboard shows a "config changed since this plan" banner, and
    reaching the engine for it would drag the LLM client and the Google Calendar
    service-account credentials into a surface that writes to neither (ARCHITECTURE.md §8).
    """
    import hashlib
    import json
    serialized = json.dumps({'user_profile': plan_profile()}, sort_keys=True)
    return hashlib.sha256(serialized.encode('utf-8')).hexdigest()
