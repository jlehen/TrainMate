import os
import json

CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "config.json")

class Config:
    def __init__(self):
        self.data = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, "r") as f:
                    self.data = json.load(f)
            except Exception as e:
                print(f"Warning: Failed to load config.json: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    @property
    def openrouter_api_key(self):
        # Prefer environment variable, fallback to config
        key = os.environ.get("OPENROUTER_API_KEY")
        if not key or key == "YOUR_OPENROUTER_API_KEY":
            key = self.get("openrouter_api_key")
        return key

    @property
    def google_sheet_id(self):
        return self.get("google_sheet_id")

    @property
    def google_calendar_id(self):
        return self.get("google_calendar_id")

    @property
    def openrouter_model(self):
        return self.get("openrouter_model", "google/gemini-3.5-flash")

    @property
    def service_account_file(self):
        path = self.get("service_account_file", "service_account.json")
        if not os.path.isabs(path):
            path = os.path.join(os.path.dirname(os.path.dirname(__file__)), path)
        return path

    @property
    def db_path(self):
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "trainmate.db")

    @property
    def science_dir(self):
        return os.path.join(os.path.dirname(os.path.dirname(__file__)), "science")

    @property
    def user_profile(self):
        return self.get("user_profile", {})

# Singleton instance
config = Config()
