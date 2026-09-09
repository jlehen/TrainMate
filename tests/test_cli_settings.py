"""The `settings` command and the registry behind it: how config.yaml, the stored row and
the built-in default combine (DESIGN_settings.md §3), and what the command surface does
with each knob (§4)."""
import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_cli_settings.db")

from trainmate import settings
from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class SettingsTestCase(unittest.TestCase):
    """Shared fixture: an isolated database and a known config menu."""

    MODELS = [
        "moonshotai/kimi-k3",
        "openai/gpt-5.5",
        "deepseek/deepseek-v4-pro",
        "z-ai/glm-5.2",
    ]
    CONFIG = {"llm": {"models": list(MODELS)}}

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

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
        patcher = patch.dict(config.data, self.CONFIG)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(openrouter_client.reset_model)

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    def row(self, stdout, name):
        """The listing row for `name`, whether or not it carries the changed marker."""
        lines = [ln for ln in stdout.splitlines()
                 if ln.strip().lstrip("*").strip().startswith(name + " ")]
        self.assertEqual(len(lines), 1, f"expected one {name} row, got: {lines}")
        return lines[0]


class TestTheListing(SettingsTestCase):
    """A bare `settings`: every knob, its value, and where the value came from (§4.1)."""

    def test_bare_settings_lists_every_registered_knob(self):
        exit_code, stdout, _ = self.run_cli(["settings"])
        self.assertEqual(exit_code, 0)
        for name in settings.names():
            self.assertIn(name, stdout)

    def test_a_row_names_its_source(self):
        _, stdout, _ = self.run_cli(["settings"])
        # Nothing stored: the model comes from the config menu, the push times are built in.
        self.assertIn("config.yaml", self.row(stdout, settings.COACH_MODEL))
        self.assertIn("default", self.row(stdout, settings.MORNING_TIME))

        self.run_cli(["settings", "set", settings.MORNING_TIME, "07:30"])
        _, stdout, _ = self.run_cli(["settings"])
        self.assertIn("set today", self.row(stdout, settings.MORNING_TIME))

    def test_an_unset_knob_says_what_unset_means(self):
        _, stdout, _ = self.run_cli(["settings"])
        self.assertIn("follows coach-model", self.row(stdout, settings.ROUTER_MODEL))
        self.assertIn("this machine", self.row(stdout, settings.TIMEZONE))

    def test_a_config_value_this_setting_cannot_read_is_reported_not_obeyed(self):
        # What PyYAML makes of an unquoted 07:30 — a sexagesimal integer (§3).
        from trainmate.config import config
        with patch.dict(config.data, {"telegram": {"push": {"morning_time": 450}}}):
            exit_code, stdout, _ = self.run_cli(["settings"])
            self.assertEqual(exit_code, 0)
            self.assertIn("Ignored in config.yaml", stdout)
            self.assertIn("08:00", self.row(stdout, settings.MORNING_TIME))
            self.assertEqual(settings.morning_time(), "08:00")


class TestTheDetailView(SettingsTestCase):
    """`settings list <name>`: one knob, with the menu or the clock it needs (§4.2)."""

    def test_the_coach_model_detail_is_the_numbered_menu(self):
        exit_code, stdout, _ = self.run_cli(["settings", "list", "coach-model"])
        self.assertEqual(exit_code, 0)
        for model in self.MODELS:
            self.assertIn(model, stdout)
        self.assertIn("llm.models (first entry)", stdout)

    def test_the_router_model_detail_is_the_same_menu(self):
        exit_code, stdout, _ = self.run_cli(["settings", "list", "router-model"])
        self.assertEqual(exit_code, 0)
        for model in self.MODELS:
            self.assertIn(model, stdout)

    def test_the_detail_names_the_config_key_behind_the_setting(self):
        _, stdout, _ = self.run_cli(["settings", "list", "morning-time"])
        self.assertIn("telegram.push.morning_time", stdout)
        self.assertIn("Default:  08:00", stdout)

    def test_an_unknown_name_fails_and_says_so(self):
        exit_code, stdout, _ = self.run_cli(["settings", "list", "wingspan"])
        self.assertEqual(exit_code, 1)
        self.assertIn("not a setting", stdout)

    def test_an_ambiguous_prefix_lists_the_candidates(self):
        exit_code, stdout, _ = self.run_cli(["settings", "set", "morning", "07:00"])
        self.assertEqual(exit_code, 1)
        self.assertIn("morning-time", stdout)
        self.assertIn("morning-deadline", stdout)

    def test_an_unambiguous_prefix_addresses_the_setting(self):
        exit_code, _, _ = self.run_cli(["settings", "set", "morning-d", "12:00"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(settings.morning_deadline(), "12:00")


class TestCoachModel(SettingsTestCase):
    """The coaching model: the config list, the stored choice and the per-invocation
    override (DESIGN_model_selection.md §3)."""

    def _stored(self):
        return test_db.get_setting("llm_model")

    def test_set_by_number_stores_the_identifier(self):
        exit_code, stdout, _ = self.run_cli(["settings", "set", "coach-model", "3"])
        self.assertEqual(exit_code, 0)
        self.assertIn(self.MODELS[2], stdout)
        # The identifier is stored, not the number — reordering config must not repoint it.
        self.assertEqual(self._stored(), self.MODELS[2])

    def test_set_by_identifier(self):
        exit_code, _, _ = self.run_cli(["settings", "set", "coach-model", self.MODELS[1]])
        self.assertEqual(exit_code, 0)
        self.assertEqual(self._stored(), self.MODELS[1])

    def test_set_rejects_anything_off_the_menu(self):
        for token in ("0", "99", "garbage", "openai/not-on-the-menu"):
            with self.subTest(token=token):
                exit_code, _, _ = self.run_cli(["settings", "set", "coach-model", token])
                self.assertEqual(exit_code, 1)
                self.assertIsNone(self._stored())

    def test_reset_falls_back_to_the_config_default(self):
        self.run_cli(["settings", "set", "coach-model", "4"])
        self.assertEqual(self._stored(), self.MODELS[3])

        exit_code, stdout, _ = self.run_cli(["settings", "reset", "coach-model"])
        self.assertEqual(exit_code, 0)
        self.assertIn(self.MODELS[0], stdout)
        self.assertIsNone(self._stored())

    def test_a_stored_model_dropped_from_config_stays_active(self):
        test_db.set_setting("llm_model", "anthropic/claude-opus-4.8")
        _, stdout, _ = self.run_cli(["settings", "list", "coach-model"])
        self.assertIn("not in config list", stdout)
        # Still the model that would be queried — nothing is auto-corrected.
        from trainmate.llm_models import active_model
        self.assertEqual(active_model(), "anthropic/claude-opus-4.8")

    def test_the_invocation_override_wins_and_stores_nothing(self):
        from trainmate.openrouter import openrouter_client
        self.run_cli(["settings", "set", "coach-model", "2"])
        exit_code, _, _ = self.run_cli(
            ["--llm-model", "google/gemini-2.5-pro", "goal", "list"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(openrouter_client.model, "google/gemini-2.5-pro")
        self.assertEqual(self._stored(), self.MODELS[1])

    def test_the_client_resolves_the_stored_model(self):
        from trainmate.openrouter import openrouter_client
        self.run_cli(["settings", "set", "coach-model", "3"])
        self.assertEqual(openrouter_client.model, self.MODELS[2])


class TestRouterModel(SettingsTestCase):
    """The router role: one allowlist with the coaching model, and 'unset' means 'follow
    the coach' (DESIGN_bot_simple_frontend.md §5.4)."""

    def test_unset_follows_the_coaching_model(self):
        self.assertIsNone(settings.router_model())

    def test_set_picks_from_the_same_menu_by_number(self):
        exit_code, _, _ = self.run_cli(["settings", "set", "router-model", "2"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(settings.router_model(), self.MODELS[1])

    def test_an_off_menu_identifier_is_refused(self):
        exit_code, stdout, _ = self.run_cli(
            ["settings", "set", "router-model", "cheap/not-on-the-menu"])
        self.assertEqual(exit_code, 1)
        self.assertIn("not in the config list", stdout)
        self.assertIsNone(settings.router_model())

    def test_config_seeds_it_and_the_stored_row_overrides(self):
        from trainmate.config import config
        seeded = dict(self.CONFIG["llm"], router_model=self.MODELS[3])
        with patch.dict(config.data, {"llm": seeded}):
            self.assertEqual(settings.router_model(), self.MODELS[3])
            self.run_cli(["settings", "set", "router-model", "1"])
            self.assertEqual(settings.router_model(), self.MODELS[0])
            self.run_cli(["settings", "reset", "router-model"])
            self.assertEqual(settings.router_model(), self.MODELS[3])

    def test_a_config_key_emptied_out_reads_as_unset(self):
        from trainmate.config import config
        seeded = dict(self.CONFIG["llm"], router_model="   ")
        with patch.dict(config.data, {"llm": seeded}):
            self.assertIsNone(settings.router_model())
            _, stdout, _ = self.run_cli(["settings"])
            self.assertNotIn("Ignored in config.yaml", stdout)

    def test_the_router_command_uses_it(self):
        self.run_cli(["settings", "set", "router-model", "2"])
        from trainmate.openrouter import openrouter_client
        with patch.object(openrouter_client, "complete",
                          return_value={"intent": "show_today"}) as complete:
            exit_code, stdout, _ = self.run_cli(["bot", "route", "what's today?"])
        self.assertEqual(exit_code, 0)
        self.assertTrue(complete.called)
        self.assertEqual(openrouter_client.model, self.MODELS[1])
        self.assertIn("show_today", stdout)


class TestMorningPushKnobs(SettingsTestCase):
    """The push window and its switches: config seeds them, the database overrides, and
    the bot reads the result (DESIGN_bot_simple_frontend.md §4.3)."""

    def test_the_built_in_defaults_apply_with_nothing_configured(self):
        self.assertTrue(settings.push_enabled())
        self.assertEqual(settings.morning_time(), "08:00")
        self.assertEqual(settings.morning_deadline(), "15:00")
        self.assertFalse(settings.adapt_first())

    def test_config_seeds_them(self):
        from trainmate.config import config
        block = {"push": {"enabled": False, "morning_time": "07:30",
                          "morning_deadline": "12:00", "adapt_first": True}}
        with patch.dict(config.data, {"telegram": block}):
            self.assertFalse(settings.push_enabled())
            self.assertEqual(settings.morning_time(), "07:30")
            self.assertEqual(settings.morning_deadline(), "12:00")
            self.assertTrue(settings.adapt_first())

    def test_a_stored_row_overrides_config_and_reset_gives_it_back(self):
        from trainmate.config import config
        with patch.dict(config.data, {"telegram": {"push": {"morning_time": "07:30"}}}):
            self.run_cli(["settings", "set", "morning-time", "06:15"])
            self.assertEqual(settings.morning_time(), "06:15")
            self.run_cli(["settings", "reset", "morning-time"])
            self.assertEqual(settings.morning_time(), "07:30")

    def test_a_time_is_normalised_and_a_non_time_refused(self):
        self.run_cli(["settings", "set", "morning-time", "7:05"])
        self.assertEqual(settings.morning_time(), "07:05")
        for token in ("25:00", "07:99", "half past seven", "730"):
            with self.subTest(token=token):
                exit_code, _, _ = self.run_cli(["settings", "set", "morning-time", token])
                self.assertEqual(exit_code, 1)
                self.assertEqual(settings.morning_time(), "07:05")

    def test_a_switch_takes_the_usual_words(self):
        for token, expected in (("off", False), ("on", True), ("false", False),
                                ("yes", True), ("0", False)):
            with self.subTest(token=token):
                exit_code, _, _ = self.run_cli(["settings", "set", "push", token])
                self.assertEqual(exit_code, 0)
                self.assertIs(settings.push_enabled(), expected)

    def test_a_non_switch_is_refused(self):
        exit_code, stdout, _ = self.run_cli(["settings", "set", "adapt-first", "maybe"])
        self.assertEqual(exit_code, 1)
        self.assertIn("on or off", stdout)
        self.assertFalse(settings.adapt_first())

    def test_the_push_window_is_computed_from_the_stored_times(self):
        import datetime
        import trainmate_bot
        self.run_cli(["settings", "set", "morning-time", "06:00"])
        self.run_cli(["settings", "set", "morning-deadline", "09:00"])
        now = datetime.datetime(2026, 6, 10, 5, 0)
        self.assertEqual(
            trainmate_bot.next_push_delay(
                now, settings.morning_time(), settings.morning_deadline()),
            3600.0)

    def test_the_morning_command_adapts_only_when_the_switch_is_on(self):
        with patch("trainmate.cli.bot._auto_adapt_note", return_value="adapted") as note:
            self.run_cli(["bot", "morning", "--force"])
            self.assertFalse(note.called)
            self.run_cli(["settings", "set", "adapt-first", "on"])
            self.run_cli(["bot", "morning", "--force"])
            self.assertTrue(note.called)


class TestRegistryShape(SettingsTestCase):
    """Rules every registered setting obeys, checked over the registry itself so a
    setting added later is covered without editing this file (AGENTS.md)."""

    def test_names_and_keys_are_unique_and_addressable(self):
        names = settings.names()
        self.assertEqual(len(names), len(set(names)))
        keys = [setting.key for setting in settings.SETTINGS]
        self.assertEqual(len(keys), len(set(keys)))
        for name in names:
            self.assertEqual(settings.get(name).name, name)
            self.assertRegex(name, r"^[a-z][a-z0-9-]*$")

    def test_every_setting_resolves_with_an_empty_database_and_config(self):
        from trainmate.config import config
        with patch.dict(config.data, {}, clear=True):
            for name in settings.names():
                with self.subTest(name=name):
                    resolved = settings.resolve(name)
                    self.assertIn(resolved.source, ("db", "config", "default"))
                    settings.value(name)

    def test_every_setting_refuses_a_value_it_cannot_read(self):
        for name in settings.names():
            with self.subTest(name=name):
                exit_code, _, _ = self.run_cli(
                    ["settings", "set", name, "!!not a valid value!!"])
                self.assertEqual(exit_code, 1)
                self.assertIsNone(settings.stored(name))

    def test_every_setting_round_trips_through_set_and_reset(self):
        samples = {
            settings.COACH_MODEL: "2",
            settings.ROUTER_MODEL: "3",
            settings.TIMEZONE: "UTC",
            settings.PUSH: "off",
            settings.MORNING_TIME: "06:45",
            settings.MORNING_DEADLINE: "11:15",
            settings.ADAPT_FIRST: "on",
            settings.COMMITMENT_DAYS: "10",
        }
        self.assertEqual(sorted(samples), sorted(settings.names()),
                         "a new setting needs a sample value here")
        for name, token in samples.items():
            with self.subTest(name=name):
                exit_code, _, _ = self.run_cli(["settings", "set", name, token])
                self.assertEqual(exit_code, 0)
                self.assertIsNotNone(settings.stored(name))
                exit_code, _, _ = self.run_cli(["settings", "reset", name])
                self.assertEqual(exit_code, 0)
                self.assertIsNone(settings.stored(name))


if __name__ == "__main__":
    unittest.main()
