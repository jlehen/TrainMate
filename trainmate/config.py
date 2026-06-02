import os
import json
from typing import Any, Optional

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

class Config:
    """Manages application settings loaded from config.json and env variables."""

    def __init__(self) -> None:
        """Initializes the configuration store from config.json if it exists."""
        self.data: dict[str, Any] = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    self.data = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load config.json: {e}")

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
    def google_sheet_id(self) -> Optional[str]:
        """Gets the Google Spreadsheet ID for athlete Garmin metrics."""
        return self.get("google_sheet_id")

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
    def user_profile(self) -> dict[str, Any]:
        """Gets the user profile information dict."""
        return self.get("user_profile", {})

# Singleton instance
config = Config()
