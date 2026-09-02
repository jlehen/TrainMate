"""The adapt-extracted daily-signal path (DESIGN_signal_extraction.md).

Covers the three guards that stand between a model's guess and a stored measurement: the
category vocabulary shown to the model, the normalization applied to whatever it returns,
and the refusal to store a `value` the note never stated.
"""
import os
import unittest
from unittest.mock import Mock, patch

from tests.helpers import clear_all_tables, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_signal_extraction.db")

from trainmate import runtime, signals
from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service


class TestVocabulary(unittest.TestCase):
    def test_config_augments_the_shipped_set(self):
        from trainmate.config import config
        with patch.object(
            config, "data", {"coach": {"signal_metrics": {"Altitude": "above 1500 m"}}}
        ):
            merged = config.signal_metrics
        self.assertIn("alcohol", merged, "shipped categories must survive config")
        self.assertEqual(merged["altitude"], "above 1500 m", "config key is normalized")

    def test_config_can_reword_a_shipped_gloss(self):
        from trainmate.config import config
        with patch.object(
            config, "data", {"coach": {"signal_metrics": {"heat": "my own wording"}}}
        ):
            self.assertEqual(config.signal_metrics["heat"], "my own wording")

    def test_every_shipped_sleep_category_trips_the_exclusion_guard(self):
        """The guard matches a substring, so a sleep category named without it would
        silently correlate against the sleep score (DESIGN_signal_extraction.md §4)."""
        for name, gloss in signals.DEFAULT_SIGNAL_METRICS.items():
            if "sleep" not in name and "sleep" not in gloss.split(";")[0].lower():
                continue
            if "sleep" not in name:
                continue
            with self.subTest(metric=name):
                self.assertIn("sleep", signals.excluded_channels(name))

    def test_a_sleep_category_without_the_substring_loses_the_guard(self):
        """Pins the coupling that rules out names like 'insomnia' (§4)."""
        self.assertEqual(signals.excluded_channels("insomnia"), set())
        self.assertEqual(signals.excluded_channels("disturbed_sleep"), {"sleep"})

    def test_vocabulary_shows_counts_and_unused_categories(self):
        rendered = signals.format_vocabulary(
            {"alcohol": "drinks", "heat": "degrees"},
            [{"metric": "alcohol", "count": 23, "first_date": "2026-01-01",
              "last_date": "2026-08-24"}],
        )
        self.assertIn("- alcohol (23 days, last 2026-08-24): drinks", rendered)
        self.assertIn("- heat (not yet used): degrees", rendered)

    def test_a_logged_category_absent_from_config_still_appears(self):
        rendered = signals.format_vocabulary(
            {"alcohol": "drinks"},
            [{"metric": "sauna", "count": 1, "first_date": "2026-08-01",
              "last_date": "2026-08-01"}],
        )
        self.assertIn("- sauna (1 day, last 2026-08-01)", rendered)


class TestNearMiss(unittest.TestCase):
    KNOWN = ["alcohol", "disturbed_sleep", "heat", "travel"]

    def test_a_close_spelling_is_offered(self):
        self.assertEqual(signals.nearest_known("heatwave", self.KNOWN), "heat")

    def test_an_unrelated_category_offers_nothing(self):
        self.assertIsNone(signals.nearest_known("altitude", self.KNOWN))

    def test_the_cutoff_catches_the_drift_spellings_it_exists_for(self):
        """Lower end of the calibration window in `nearest_known`."""
        for coined, expected in (
            ("heatwave", "heat"), ("travelling", "travel"), ("stressed", "stress"),
            ("illnesses", "illness"), ("alcohol_units", "alcohol"),
            ("underfueling", "underfuelling"),
        ):
            with self.subTest(coined=coined):
                known = list(signals.DEFAULT_SIGNAL_METRICS)
                self.assertEqual(signals.nearest_known(coined, known), expected)

    def test_no_two_shipped_categories_look_like_each_other(self):
        """Upper end: a category added to the shipped set must not be close enough to an
        existing one to be offered as a correction for it."""
        names = list(signals.DEFAULT_SIGNAL_METRICS)
        for name in names:
            with self.subTest(metric=name):
                self.assertIsNone(
                    signals.nearest_known(name, names),
                    f"'{name}' is confusable with another shipped category",
                )

    def test_near_miss_never_rewrites_the_metric_itself(self):
        """Detection is advisory: `sleep_debt` must not be folded into a sleep category
        and silently inherit its channel exclusion (§6)."""
        self.assertEqual(signals.normalize_metric("Sleep_Debt"), "sleep_debt")


class TestCaptureMessageSignal(unittest.TestCase):
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
        clear_all_tables(test_db)
        self.syncer = Mock()
        self.syncer.calendar_id = "cal"
        self._events = []

        def _add(date, metric, value, text, existing_id=None):
            self._events.append((date, metric, value, text, existing_id))
            return existing_id or f"evt-{date}-{metric}"

        self.syncer.add_signal_event.side_effect = _add
        runtime.calendar_syncer = self.syncer

    def test_a_stated_number_is_stored(self):
        rows = coach_service.capture_message_signal(
            {"metric": "alcohol", "date": "2026-08-29", "end_date": "2026-08-29",
             "value": 3, "text": "three beers"},
            "2026-08-30",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["value"], 3)
        self.assertEqual(rows[0]["metric"], "alcohol")

    def test_a_non_numeric_value_is_dropped_not_coerced(self):
        """The model may only pass a number the note stated; anything else is not a
        measurement (§3)."""
        rows = coach_service.capture_message_signal(
            {"metric": "disturbed_sleep", "date": "2026-08-29", "value": "bad"},
            "2026-08-30",
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["value"])

    def test_a_boolean_is_not_a_number(self):
        rows = coach_service.capture_message_signal(
            {"metric": "stress", "date": "2026-08-29", "value": True}, "2026-08-30"
        )
        self.assertIsNone(rows[0]["value"])

    def test_the_metric_is_normalized(self):
        rows = coach_service.capture_message_signal(
            {"metric": "  Heat  ", "date": "2026-08-29", "value": 38}, "2026-08-30"
        )
        self.assertEqual(rows[0]["metric"], "heat")

    def test_the_chosen_category_overrides_the_proposed_one(self):
        """What the athlete confirmed wins over what the model coined (§6)."""
        rows = coach_service.capture_message_signal(
            {"metric": "heatwave", "date": "2026-08-29", "value": 38},
            "2026-08-30", metric_override="heat",
        )
        self.assertEqual(rows[0]["metric"], "heat")

    def test_a_range_writes_one_row_per_day(self):
        rows = coach_service.capture_message_signal(
            {"metric": "heat", "date": "2026-08-29", "end_date": "2026-08-31"},
            "2026-08-30",
        )
        self.assertEqual([r["date"] for r in rows],
                         ["2026-08-29", "2026-08-30", "2026-08-31"])

    def test_an_inverted_range_collapses_to_its_start(self):
        rows = coach_service.capture_message_signal(
            {"metric": "heat", "date": "2026-08-31", "end_date": "2026-08-29"},
            "2026-08-30",
        )
        self.assertEqual([r["date"] for r in rows], ["2026-08-31"])

    def test_a_missing_date_defaults_to_the_run_date(self):
        rows = coach_service.capture_message_signal({"metric": "stress"}, "2026-08-30")
        self.assertEqual([r["date"] for r in rows], ["2026-08-30"])

    def test_a_blank_metric_writes_nothing(self):
        self.assertEqual(
            coach_service.capture_message_signal({"metric": "  "}, "2026-08-30"), []
        )
        self.syncer.add_signal_event.assert_not_called()

    def test_a_second_capture_updates_the_same_day_rather_than_duplicating(self):
        coach_service.capture_message_signal(
            {"metric": "alcohol", "date": "2026-08-29", "value": 2}, "2026-08-30"
        )
        coach_service.capture_message_signal(
            {"metric": "alcohol", "date": "2026-08-29", "value": 4}, "2026-08-30"
        )
        rows = test_db.get_daily_signals("2026-08-29", "2026-08-29", metric="alcohol")
        self.assertEqual(len(rows), 1, "upsert by (date, metric)")
        self.assertEqual(rows[0]["value"], 4)

    def test_a_failed_calendar_write_stores_nothing(self):
        """The calendar is the source of truth, so a local row cannot outlive a failed
        event write (DESIGN_signal_authoring.md §2)."""
        self.syncer.add_signal_event.side_effect = lambda *a, **k: None
        rows = coach_service.capture_message_signal(
            {"metric": "heat", "date": "2026-08-29"}, "2026-08-30"
        )
        self.assertEqual(rows, [])
        self.assertEqual(test_db.get_daily_signals("2026-08-29", "2026-08-29"), [])

    def test_known_metrics_merge_config_and_what_is_logged(self):
        coach_service.capture_message_signal(
            {"metric": "sauna", "date": "2026-08-29"}, "2026-08-30"
        )
        known = coach_service.known_signal_metrics()
        self.assertIn("sauna", known, "a logged category counts as known")
        self.assertIn("alcohol", known, "so does a shipped one never logged")


class TestConfirmationLadder(unittest.TestCase):
    """The two-question ladder in `cli/workouts/generate.py` (§6).

    Two y/N calls have to yield all three outcomes — reuse, coin, log nothing — because
    `runtime.prompt.confirm` is shared with the Telegram frontend and stays binary.
    """
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
        clear_all_tables(test_db)
        syncer = Mock()
        syncer.calendar_id = "cal"
        syncer.add_signal_event.side_effect = (
            lambda date, metric, value, text, existing=None:
            existing or f"evt-{date}-{metric}"
        )
        runtime.calendar_syncer = syncer

    def _run(self, candidate, answers, render=""):
        """Drives one candidate through the ladder; returns the questions asked.

        The ladder now lives in cli/candidates.py, where `workout adapt -m` and `bot
        capture note` both reach it (DESIGN_bot_simple_frontend.md §12.10), and the
        questions come from the active renderer — so the voice is pinned here too."""
        from trainmate.cli.candidates import confirm_new_signals
        asked = []
        replies = iter(answers)

        def confirm(question):
            asked.append(question)
            return next(replies)

        runtime.prompt = Mock()
        runtime.prompt.confirm.side_effect = confirm
        # Both handles are process-wide singletons: an assignment shadows the accessor
        # for good, and a cached renderer outlives the env var that chose it.
        self.addCleanup(runtime.reset, "prompt", "render")
        runtime.reset("render")
        with patch("builtins.print"), patch.dict(
            os.environ, {"TRAINMATE_RENDER": render} if render else {}, clear=False
        ):
            if not render:
                os.environ.pop("TRAINMATE_RENDER", None)
            confirm_new_signals([candidate], "2026-08-30")
        return asked

    def test_a_known_category_asks_once(self):
        asked = self._run({"metric": "alcohol", "date": "2026-08-29"}, [True])
        self.assertEqual(len(asked), 1)
        self.assertIn("alcohol", asked[0])

    def test_a_near_miss_offers_the_existing_category_first(self):
        asked = self._run({"metric": "heatwave", "date": "2026-08-29"}, [True])
        self.assertEqual(len(asked), 1, "accepting the reuse ends the ladder")
        self.assertIn("heat", asked[0])
        self.assertNotIn("NEW", asked[0])
        rows = test_db.get_daily_signals("2026-08-29", "2026-08-29")
        self.assertEqual([r["metric"] for r in rows], ["heat"])

    def test_declining_the_reuse_offers_the_coined_category(self):
        asked = self._run({"metric": "heatwave", "date": "2026-08-29"}, [False, True])
        self.assertEqual(len(asked), 2)
        self.assertIn("NEW", asked[1])
        rows = test_db.get_daily_signals("2026-08-29", "2026-08-29")
        self.assertEqual([r["metric"] for r in rows], ["heatwave"])

    def test_declining_both_logs_nothing(self):
        asked = self._run({"metric": "heatwave", "date": "2026-08-29"}, [False, False])
        self.assertEqual(len(asked), 2)
        self.assertEqual(test_db.get_daily_signals("2026-08-29", "2026-08-29"), [])

    def test_an_unrelated_new_category_is_marked_new_and_asked_once(self):
        asked = self._run({"metric": "food_poisoning", "date": "2026-08-29"}, [True])
        self.assertEqual(len(asked), 1)
        self.assertIn("NEW", asked[0])

    def test_a_declined_single_question_logs_nothing(self):
        self._run({"metric": "alcohol", "date": "2026-08-29"}, [False])
        self.assertEqual(test_db.get_daily_signals("2026-08-29", "2026-08-29"), [])

    def test_the_companion_asks_the_same_ladder_in_its_own_words(self):
        """`bot capture note` runs this ladder in companion chat, so the same two rungs
        must arrive without underscores, ISO spans or the word NEW
        (DESIGN_bot_simple_frontend.md §6, §12.3)."""
        asked = self._run(
            {"metric": "heatwave", "date": "2026-08-29"}, [False, True], render="simple"
        )
        self.assertEqual(len(asked), 2, "the ladder keeps both rungs in either voice")
        self.assertNotIn("NEW", asked[1])
        self.assertIn("new one for me", asked[1])
        for question in asked:
            self.assertNotIn("2026-08-29", question)
            self.assertNotIn("_", question)


if __name__ == "__main__":
    unittest.main()
