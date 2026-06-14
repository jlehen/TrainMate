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
            default: Fallback value if key is not found.

        Returns:
            The configuration value.
        """
        return self.data.get(key, default)

    @property
    def openrouter_api_key(self) -> Optional[str]:
        """Gets OpenRouter API Key. Prefers env variable over config.json."""
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key or key == "YOUR_OPENROUTER_API_KEY":
            key = self.get("openrouter_api_key")
        return key

    @property
    def google_calendar_id(self) -> Optional[str]:
        """Gets the target Google Calendar ID for training events."""
        return self.get("google_calendar_id")

    @property
    def openrouter_model(self) -> str:
        """Gets the OpenRouter model identifier, defaulting to gemini-3.5-flash."""
        return self.get("openrouter_model", "google/gemini-3.5-flash")

    @property
    def service_account_file(self) -> str:
        """Gets the service account file path. Resolves relative path to absolute."""
        path = self.get("service_account_file", "service_account.json")
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
        return self.get("metrics_lookback_days", 15)

    @property
    def goals_lookback_days(self) -> int:
        """Gets the number of days into the past to look for preceding goals, defaulting to 90."""
        return self.get("goals_lookback_days", 90)

    @property
    def workout_generate_days(self) -> int:
        """Gets the default number of days to generate workouts for, defaulting to 28."""
        raw = self.get("workout_generate_days", 28)
        return int(raw)

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
    def low_load_threshold(self) -> float:
        """Gets the workload score below which an activity is considered minor (default 25).

        Used to: (1) widen mismatch tolerance to 50% for matched activities, and
        (2) display unplanned activities as '(minor)' rather than 'UNPLANNED'.
        """
        return float(self.get("low_load_threshold", 25.0))

    # --- Garmin direct-pull knobs (see DESIGN_garmin_direct_pull.md §13) ---
    @property
    def garmin_email(self) -> Optional[str]:
        """Garmin Connect login email. Read from config.yaml only — credentials are
        kept out of the environment (config.yaml is gitignored)."""
        return self.get("garmin_email")

    @property
    def garmin_password(self) -> Optional[str]:
        """Garmin Connect password. Read from config.yaml only (see garmin_email)."""
        return self.get("garmin_password")

    @property
    def garmin_token_store(self) -> str:
        """Directory where garminconnect persists OAuth tokens (default ~/.garminconnect)."""
        path = self.get("garmin_token_store")
        if not path:
            path = os.path.join(os.path.expanduser("~"), ".garminconnect")
        return os.path.expanduser(path)

    @property
    def garmin_refresh_minutes(self) -> int:
        """Minimum minutes between automatic Garmin hits before a refresh re-pulls (default 120)."""
        return int(self.get("garmin_refresh_minutes", 120))

    @property
    def garmin_mutable_days(self) -> int:
        """Trailing days a forward-refresh re-fetches for late-finalizing data (default 3)."""
        return int(self.get("garmin_mutable_days", 3))

    @property
    def garmin_backfill_prompt_days(self) -> int:
        """Backward-gap size above which a backfill is surfaced as a command rather than
        run automatically; also gates interior gaps (default 30)."""
        return int(self.get("garmin_backfill_prompt_days", 30))

    @property
    def garmin_initial_backfill_days(self) -> int:
        """Range used to build the cold-start backfill command (default 90)."""
        return int(self.get("garmin_initial_backfill_days", 90))

    @property
    def garmin_throttle_seconds(self) -> float:
        """Default sleep between Garmin API calls; --sleep overrides on `data pull` (default 0.2)."""
        return float(self.get("garmin_throttle_seconds", 0.2))

    @property
    def calendar_context_source(self) -> str:
        """Gets the source tag for calendar events."""
        return self.get("calendar_context_source", "trainmate-context")

    @property
    def high_intensity_rpe_threshold(self) -> int:
        """Gets the RPE threshold above which an activity is considered high intensity."""
        return int(self.get("high_intensity_rpe_threshold", 8))

    @property
    def high_intensity_tss_threshold(self) -> float:
        """Gets the TSS threshold above which an activity is considered high intensity."""
        return float(self.get("high_intensity_tss_threshold", 120.0))

# Singleton instance
config = Config()
