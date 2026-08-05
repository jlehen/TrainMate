"""`model` command tests: how the config list, the stored choice and the
per-invocation override combine (DESIGN_model_selection.md §3)."""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_models.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
trainmate.db.db = test_db
trainmate_cli.db = test_db


class TestModelCommand(unittest.TestCase):
    """The `model` command and how config/DB combine (DESIGN_model_selection.md)."""

    MODELS = [
        "moonshotai/kimi-k3",
        "openai/gpt-5.5",
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
    ]

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        trainmate.db.db = test_db
        trainmate_cli.db = test_db

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        from trainmate.config import config
        from trainmate.openrouter import openrouter_client
        clear_all_tables(test_db)
        openrouter_client.reset_model()
        patcher = patch.dict(config.data, {"llm": {"models": list(self.MODELS)}})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(openrouter_client.reset_model)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    def _stored(self):
        return test_db.get_setting("llm_model")

    def _active_line(self, stdout):
        """The listing line carrying the active marker."""
        lines = [ln for ln in stdout.splitlines() if ln.startswith("*")]
        self.assertEqual(len(lines), 1, f"expected one active row, got: {lines}")
        return lines[0]

    def test_bare_model_lists_and_marks_config_default(self):
        exit_code, stdout, _ = self.run_cli(["model"])
        self.assertEqual(exit_code, 0)
        for model in self.MODELS:
            self.assertIn(model, stdout)
        active = self._active_line(stdout)
        self.assertIn("1 ", active)
        self.assertIn(self.MODELS[0], active)
        self.assertIn("config default", active)
        self.assertIsNone(self._stored())

    def test_set_by_number_stores_identifier(self):
        exit_code, stdout, _ = self.run_cli(["model", "set", "3"])
        self.assertEqual(exit_code, 0)
        self.assertIn(self.MODELS[2], stdout)
        # The identifier is stored, not the number — reordering config must not repoint it.
        self.assertEqual(self._stored(), self.MODELS[2])

        exit_code, stdout, _ = self.run_cli(["model"])
        self.assertEqual(exit_code, 0)
        active = self._active_line(stdout)
        self.assertIn(self.MODELS[2], active)
        self.assertIn("active (set today)", active)

    def test_set_by_identifier(self):
        exit_code, _, _ = self.run_cli(["model", "set", self.MODELS[1]])
        self.assertEqual(exit_code, 0)
        self.assertEqual(self._stored(), self.MODELS[1])

    def test_set_rejects_bad_targets(self):
        for token in ("0", "99", "garbage", "openai/not-on-the-menu"):
            with self.subTest(token=token):
                exit_code, stdout, _ = self.run_cli(["model", "set", token])
                self.assertEqual(exit_code, 1)
                self.assertIsNone(self._stored())

    def test_reset_falls_back_to_config_default(self):
        self.run_cli(["model", "set", "4"])
        self.assertEqual(self._stored(), self.MODELS[3])

        exit_code, stdout, _ = self.run_cli(["model", "reset"])
        self.assertEqual(exit_code, 0)
        self.assertIn(self.MODELS[0], stdout)
        self.assertIsNone(self._stored())

        _, stdout, _ = self.run_cli(["model"])
        self.assertIn("config default", self._active_line(stdout))

    def test_stored_model_dropped_from_config_stays_active(self):
        test_db.set_setting("llm_model", "anthropic/claude-opus-4.8")
        exit_code, stdout, _ = self.run_cli(["model"])
        self.assertEqual(exit_code, 0)
        active = self._active_line(stdout)
        self.assertIn("anthropic/claude-opus-4.8", active)
        self.assertIn("not in config list", active)
        # Still the model that would be queried — nothing is auto-corrected.
        from trainmate.llm_models import active_model
        self.assertEqual(active_model(), "anthropic/claude-opus-4.8")

    def test_invocation_override_wins_and_does_not_store(self):
        from trainmate.openrouter import openrouter_client
        self.run_cli(["model", "set", "2"])
        exit_code, _, _ = self.run_cli(["--llm-model", "google/gemini-2.5-pro", "goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(openrouter_client.model, "google/gemini-2.5-pro")
        self.assertEqual(self._stored(), self.MODELS[1])

    def test_client_resolves_stored_model(self):
        from trainmate.openrouter import openrouter_client
        self.run_cli(["model", "set", "3"])
        self.assertEqual(openrouter_client.model, self.MODELS[2])
