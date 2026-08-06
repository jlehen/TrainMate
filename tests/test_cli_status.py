import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_status.db")


def _days_out(n: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=n)).isoformat()


# `status` only lists the goal and constraints that are still live, so fixed dates
# stop appearing once they pass (same rot 2a7cd71 fixed in test_constraints.py).
GOAL_DATE = _days_out(120)

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestCliStatus(unittest.TestCase):
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

    @patch("trainmate.cli.status.ensure_recent_data")
    def test_status_command(self, _ens):
        test_db.save_metric_cache(
            date="2026-05-31",
            rhr=48, hrv=82, sleep_score=90, stress=15,
            ctl=60.0, atl=68.4, tsb=-8.4,
        )
        test_db.save_baseline(
            date="2026-05-31",
            rhr_mean=50.0, rhr_std=1.5,
            hrv_mean=78.0, hrv_std=4.0,
            sleep_mean=82.0, sleep_std=3.0,
        )
        test_db.add_objective(
            title="London Marathon", target_date=GOAL_DATE,
            sport_type="running", priority=1,
        )
        learning_id = test_db.add_learning("Rest well on Fridays")

        exit_code, stdout, stderr = self.run_cli(["status"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== TRAINMATE ATHLETE STATUS ===", stdout)
        self.assertIn("London Marathon", stdout)
        self.assertIn("Resting HR : 48 bpm", stdout)
        self.assertIn("Overnight HRV: 82 ms", stdout)
        self.assertIn("Baselines (28-day)", stdout)
        # Status shows only a one-line learnings summary; the full text lives under 'learnings'.
        self.assertIn("Coach Learnings:", stdout)
        self.assertIn("1 active", stdout)
        self.assertIn("see 'learnings list'", stdout)
        self.assertNotIn("Rest well on Fridays", stdout)

        # The full text is reachable via the dedicated 'learnings' command.
        exit_code, ln_stdout, _ = self.run_cli(["learnings", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn(f"[{learning_id}|general|tentative]", ln_stdout)
        self.assertIn("Rest well on Fridays", ln_stdout)

        trip_start, trip_end = _days_out(1), _days_out(39)
        test_db.add_constraint(
            title="Ibiza Trip", start_date=trip_start, end_date=trip_end,
            description="Rest weeks",
        )

        goals = test_db.get_objectives()
        g_id = goals[0]["id"]
        events = test_db.get_constraints()
        e_id = events[0]["id"]

        exit_code_v, stdout_v, _ = self.run_cli(["status", "-v"])
        self.assertEqual(exit_code_v, 0)
        self.assertIn("Goals:", stdout_v)
        self.assertIn(
            f"- [UPCOMING] ID: {g_id} | London Marathon (running) on {GOAL_DATE} (Priority: 1)",
            stdout_v,
        )
        self.assertIn("Active Constraints:", stdout_v)
        self.assertIn(
            f"- ID: {e_id} | Ibiza Trip: {trip_start} to {trip_end} | advisory",
            stdout_v,
        )
        self.assertIn("  Details:\n    Rest weeks", stdout_v)

        exit_code_vv, stdout_vv, _ = self.run_cli(["status", "--verbose"])
        self.assertEqual(exit_code_vv, 0)
        self.assertIn("Goals:", stdout_vv)
        self.assertIn(
            f"- [UPCOMING] ID: {g_id} | London Marathon (running) on {GOAL_DATE} (Priority: 1)",
            stdout_vv,
        )

    @patch("trainmate.cli.status.ensure_recent_data")
    def test_status_pmc_never_zero_fills_and_explains_warmup(self, _ens):
        # §6.1/§8: pulled-but-never-recomputed rows have NULL PMC — status must not
        # print "CTL 0.0 | ... | TSB 0.0" (a zero TSB reads as a real neutral balance).
        # The whole Fitness line drops, but the §3.3(b) still-warming flag shows WHY.
        today = datetime.now().date()
        for i in range(40, -1, -1):
            test_db.save_metric_cache(
                date=(today - timedelta(days=i)).isoformat(),
                rhr=50, hrv=70, sleep_score=80, stress=20,
            )
        exit_code, stdout, stderr = self.run_cli(["status"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("CTL 0.0", stdout)
        self.assertNotIn("- Fitness", stdout)
        self.assertIn("PMC still warming", stdout)
        self.assertIn("40 days", stdout)

    @patch("trainmate.cli.status.ensure_recent_data")
    def test_status_pmc_line_past_warmup(self, _ens):
        # A DB with 60 days of steady load, recomputed: the latest row is past the
        # 42-day warm-up cutoff, so status shows real CTL/ATL/TSB, a ramp, the TSB-lag
        # footnote, and (59 < 126 days) the still-warming flag.
        from trainmate import garmin as real_garmin
        today = datetime.now().date()
        for i in range(59, -1, -1):
            ds = (today - timedelta(days=i)).isoformat()
            test_db.save_metric_cache(
                date=ds, rhr=50, hrv=70, sleep_score=80, stress=20
            )
            test_db.save_completed_activity(
                activity_id=f"pmc{i}", date=ds, start_time=f"{ds} 09:00:00",
                activity_name="Run", activity_type="running", duration_sec=3600,
                distance_km=10.0, elevation_gain_m=0.0, avg_hr=150, max_hr=170,
                rpe=None, tss=60.0,
            )
        real_garmin.recompute_derived(dbh=test_db)
        exit_code, stdout, stderr = self.run_cli(["status"])
        self.assertEqual(exit_code, 0)
        self.assertIn("- Fitness    : CTL ", stdout)
        self.assertIn("ATL:CTL ", stdout)                  # relative-overload ratio
        self.assertIn("/wk", stdout)                       # ramp rendered
        self.assertIn("TSB is CTL(yesterday)", stdout)     # lag footnote rides with TSB
        self.assertIn("PMC still warming", stdout)
        self.assertNotIn("CTL 0.0", stdout)
