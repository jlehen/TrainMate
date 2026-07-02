import os
import yaml
from typing import Any, Optional

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.yaml")

class Config:
    """Manages application settings loaded from config.yaml and env variables."""

    def __init__(self) -> None:
        """Initializes the configuration store from config.yaml if it exists."""
        self.data: dict[str, Any] = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    self.data = yaml.safe_load(f) or {}
            except Exception as e:
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
    def openrouter_model(self) -> str:
        """Gets the OpenRouter model identifier, defaulting to gemini-3.5-flash."""
        return self.get("llm", {}).get("model", "google/gemini-3.5-flash")

    @property
    def service_account_file(self) -> str:
        """Gets the service account file path. Resolves relative path to absolute."""
        path = self.get("google", {}).get("service_account_file", "service_account.json")
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
        return path

    @property
    def db_path(self) -> str:
        """Gets the database absolute file path."""
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "trainmate.db")

    @property
    def science_dir(self) -> str:
        """Gets the directory containing training guidelines/sports science texts."""
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "science")

    @property
    def llm_logs_dir(self) -> str:
        """Gets the directory where LLM interaction logs are stored."""
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "logs", "llm_exchanges")

    @property
    def app_science_dir(self) -> str:
        """Gets the internal application directory containing default sports science guidelines."""
        return os.path.join(os.path.dirname(__file__), "science")

    @property
    def user_profile(self) -> dict[str, Any]:
        """Gets the user profile information dict."""
        profile = self.get("user_profile", {})
        if profile:
            if "lthr" not in profile and "ftp" not in profile:
                raise ValueError(
                    "Configuration error: user_profile must contain at least 'lthr' or 'ftp'."
                )
        return profile

    @property
    def metrics_lookback_days(self) -> int:
        """Gets the number of days of metrics history to look at, defaulting to 15."""
        return self.get("coach", {}).get("metrics_lookback_days", 15)

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
    def replan_hard_span_days(self) -> int:
        """Hard-window floor for the §7 replan proposal: a `hard` constraint spanning at
        least this many days is intrinsically plan-shaping regardless of displaced load
        (default 3)."""
        return int(self.get("coach", {}).get("replan_hard_span_days", 3))

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
        """Directory where garminconnect persists OAuth tokens (default ~/.garminconnect)."""
        path = self.get("garmin", {}).get("token_dir")
        if not path:
            path = os.path.join(os.path.expanduser("~"), ".garminconnect")
        return os.path.expanduser(path)

    @property
    def data_refresh_minutes(self) -> int:
        """Minimum minutes before an automatic refresh re-pulls — gating both Garmin hits
        and Calendar-context syncs; within this window reads reuse the local cache
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
    def calendar_context_tag(self) -> str:
        """Gets the source tag for calendar events."""
        return self.get("google", {}).get("calendar_context_tag", "trainmate-context")

    @property
    def high_intensity_rpe_threshold(self) -> int:
        """Gets the RPE threshold above which an activity is considered high intensity."""
        return int(self.get("coach", {}).get("high_intensity_rpe_threshold", 8))

    @property
    def high_intensity_tss_threshold(self) -> float:
        """Gets the TSS threshold above which an activity is considered high intensity."""
        return float(self.get("coach", {}).get("high_intensity_tss_threshold", 120.0))

    @property
    def context_days_lookahead(self) -> int:
        """The look-ahead `k` for quantitative context-impact alignment: how many mornings
        bracket each signal episode (before and after) and the drink-free gap below which
        two signal runs merge into one episode (DESIGN_quantitative_context_impact.md §3,
        §3.0). Default 3."""
        return int(self.get("coach", {}).get("context_days_lookahead", 3))

    @property
    def context_days_min_signal_days(self) -> int:
        """Minimum total signal-days a context category must have before its aligned rows
        are shown to the coach at all — a floor against prompting on one stray night
        (DESIGN_quantitative_context_impact.md §5). Counts signal-days, not episodes.
        Default 1 (show whatever exists; the LLM judges from the visible count)."""
        return int(self.get("coach", {}).get("context_days_min_signal_days", 1))

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

# Singleton instance
config = Config()
