import os
import unittest
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db
from trainmate.util import fmt_date
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_cli_signals.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestCliSignals(unittest.TestCase):
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

    def run_cli(self, args, input_value="n"):
        return run_cli(args, input_value)

    @patch("trainmate.runtime.calendar_syncer")
    def test_signal_add_and_list(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        ids = iter(["evt-1", "evt-2", "evt-3"])
        mock_calendar.add_signal_event.side_effect = lambda *a, **k: next(ids)

        exit_code, stdout, _ = self.run_cli([
            "signal", "add", "heat", "severe", "heatwave",
            "--value", "38",
            "-d", "2026-06-25..2026-06-27",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("3 days", stdout)
        # Each authored day is echoed in the exact 'signal list' rendering.
        for day in ("2026-06-25", "2026-06-26", "2026-06-27"):
            self.assertIn(
                f"| {fmt_date(day)} | heat = 38.0 — severe heatwave (38.0)", stdout
            )
        # One tagged event authored per day in the range.
        self.assertEqual(mock_calendar.add_signal_event.call_count, 3)
        rows = test_db.get_daily_signals("2026-06-25", "2026-06-27", metric="heat")
        self.assertEqual([r["date"] for r in rows],
                         ["2026-06-25", "2026-06-26", "2026-06-27"])
        self.assertTrue(all(r["value"] == 38.0 for r in rows))
        # A provided label gets the value appended in parentheses.
        self.assertTrue(all(r["text"] == "severe heatwave (38.0)" for r in rows))

        exit_code, stdout, _ = self.run_cli([
            "signal", "list", "-d", "2026-06-25..2026-06-27"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("heat", stdout)
        self.assertIn("2026-06-26", stdout)

        exit_code, stdout, _ = self.run_cli(["signal", "lm"])
        self.assertEqual(exit_code, 0)
        self.assertIn("heat: 3 days", stdout)

    @patch("trainmate.runtime.calendar_syncer")
    def test_signal_add_no_label_uses_metric_value_form(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        mock_calendar.add_signal_event.return_value = "evt-1"
        # No label + a value → "Metric: value" (matching ingested "Alcohol: 2.0").
        self.run_cli(["signal", "add", "alcohol", "--value", "2",
                      "-d", "2026-06-25"], input_value="")
        rows = test_db.get_daily_signals("2026-06-25", "2026-06-25", metric="alcohol")
        self.assertEqual(rows[0]["text"], "Alcohol: 2.0")
        # The calendar summary matches what we mirror locally.
        self.assertEqual(
            mock_calendar.add_signal_event.call_args.args[3], "Alcohol: 2.0"
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_signal_add_label_flag(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        mock_calendar.add_signal_event.return_value = "evt-1"
        # --label wins over (and is cleaner than) the positional text.
        exit_code, _, _ = self.run_cli([
            "signal", "add", "heat",
            "--label", "severe heatwave, poor sleep", "-d", "2026-06-25",
        ])
        self.assertEqual(exit_code, 0)
        rows = test_db.get_daily_signals("2026-06-25", "2026-06-25", metric="heat")
        self.assertEqual(rows[0]["text"], "severe heatwave, poor sleep")

    @patch("trainmate.runtime.calendar_syncer")
    def test_signal_add_idempotent_by_date_metric(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        mock_calendar.add_signal_event.return_value = "evt-1"

        self.run_cli(["signal", "add", "heat", "first", "-d", "2026-06-25"])
        self.run_cli(["signal", "add", "heat", "second", "-d", "2026-06-25"])

        # Re-adding the same (date, metric) updates the existing event in place.
        last_call = mock_calendar.add_signal_event.call_args
        self.assertEqual(last_call.args[-1], "evt-1")  # existing_event_id passed
        rows = test_db.get_daily_signals("2026-06-25", "2026-06-25", metric="heat")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text"], "second")

    @patch("trainmate.runtime.calendar_syncer")
    def test_signal_rm_deletes_calendar_event(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        test_db.upsert_daily_signal_by_event("evt-9", "2026-06-25", "heat", None, "hot")
        row = test_db.get_daily_signals("2026-06-25", "2026-06-25")[0]

        exit_code, stdout, _ = self.run_cli(["signal", "rm", str(row["id"])])
        self.assertEqual(exit_code, 0)
        mock_calendar.delete_event.assert_called_once_with("evt-9")
        self.assertEqual(test_db.get_daily_signals("2026-06-25", "2026-06-25"), [])

    @patch("trainmate.runtime.calendar_syncer")
    def test_signal_rm_refuses_unscoped(self, mock_calendar):
        mock_calendar.calendar_id = "cal-1"
        test_db.upsert_daily_signal_by_event("evt-9", "2026-06-25", "heat", None, "hot")
        exit_code, stdout, _ = self.run_cli(["signal", "rm"])
        self.assertEqual(exit_code, 1)
        mock_calendar.delete_event.assert_not_called()
        self.assertEqual(len(test_db.get_daily_signals()), 1)
