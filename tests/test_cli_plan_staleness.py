"""The surfaces that report a plan built from inputs that have since changed
(DESIGN_plan_staleness.md §9): `plan show` reports it, `plan keep` dismisses it."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_plan_stale.db")

from trainmate.db import Database
import trainmate_cli  # noqa: F401  (registers the command tree)

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestPlanStalenessSurfaces(unittest.TestCase):
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

    def _days_out(self, n: int) -> str:
        return (
            datetime.now(timezone.utc).date() + timedelta(days=n)
        ).strftime("%Y-%m-%d")

    def _seed(self, *, stale: bool) -> tuple:
        """A goal with an active plan, generated either from the live profile or from a
        snapshot that no longer matches it."""
        from trainmate.coach import coach_service
        from trainmate.config import plan_profile

        oid = test_db.add_objective(
            title="Spring Race", target_date=self._days_out(60), sport_type="running"
        )
        if stale:
            snapshot = dict(plan_profile())
            snapshot["preferences"] = "a sentence that has since been reworded"
            config_hash = "not-the-current-hash"
        else:
            snapshot = plan_profile()
            config_hash = coach_service._get_config_hash()
        mid = test_db.save_macrocycle(
            objective_id=oid,
            strategy="strategy",
            goals_hash="gh",
            constraints_hash="ch",
            mesocycles=[{
                "name": "Base", "start_date": self._days_out(0),
                "end_date": self._days_out(30), "focus": "aerobic",
            }],
            config_hash=config_hash,
            config_snapshot=coach_service._get_config_snapshot(),
            profile_snapshot=json.dumps(snapshot),
        )
        return oid, mid

    @patch("trainmate.runtime.garmin")
    def test_plan_show_reports_the_change_and_both_routes_out(self, _mock_garmin):
        self._seed(stale=True)

        exit_code, stdout, _ = run_cli(["plan", "show"])
        flowed = " ".join(stdout.split())  # the block is wrapped to the terminal width

        self.assertEqual(exit_code, 0)
        self.assertIn("An input has changed since this plan was generated", flowed)
        # Names the field, so the athlete knows which input to go and look at (§5).
        self.assertIn("preferences", stdout)
        # The test being applied, not just the fact that something moved (§9).
        self.assertIn("block structure, phase order or volume ramp", flowed)
        self.assertIn("your next 'workout generate' picks it up anyway", flowed)
        # Both routes, so keeping the plan is as reachable as rebuilding it.
        self.assertIn("plan generate", stdout)
        self.assertIn("plan keep", stdout)

    @patch("trainmate.runtime.garmin")
    def test_plan_show_says_nothing_when_the_plan_is_current(self, _mock_garmin):
        self._seed(stale=False)

        exit_code, stdout, _ = run_cli(["plan", "show"])

        self.assertEqual(exit_code, 0)
        self.assertNotIn("An input has changed", stdout)
        self.assertNotIn("plan keep", stdout)

    @patch("trainmate.runtime.garmin")
    def test_a_superseded_version_is_not_flagged(self, _mock_garmin):
        """A past version is out of date by definition; saying so is noise (§9)."""
        oid, first = self._seed(stale=True)
        test_db.save_macrocycle(
            objective_id=oid, strategy="v2", goals_hash="gh", constraints_hash="ch",
            mesocycles=[{
                "name": "Base", "start_date": self._days_out(0),
                "end_date": self._days_out(30), "focus": "aerobic",
            }],
        )

        exit_code, stdout, _ = run_cli(["plan", "show", "--macrocycle", str(first)])

        self.assertEqual(exit_code, 0)
        self.assertIn("SUPERSEDED", stdout)
        self.assertNotIn("An input has changed", stdout)

    @patch("trainmate.runtime.garmin")
    def test_plan_keep_clears_the_flag_without_regenerating(self, _mock_garmin):
        _oid, mid = self._seed(stale=True)

        exit_code, stdout, _ = run_cli(["plan", "keep"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Changed since this plan was generated", stdout)
        self.assertIn("preferences", stdout)
        self.assertIn("won't be flagged again", stdout)
        # Keeping the plan is not losing the edit: it still reaches the sessions.
        self.assertIn("workout generate", stdout)

        # The strategy is untouched — keeping is a re-stamp, not a regeneration.
        self.assertEqual(test_db.get_macrocycle(mid)["strategy"], "strategy")

        # And the flag is gone, on `plan show` and on a second `plan keep`.
        _code, shown, _ = run_cli(["plan", "show"])
        self.assertNotIn("An input has changed", shown)
        _code, again, _ = run_cli(["plan", "keep"])
        self.assertIn("already reflects your current inputs", again)

    @patch("trainmate.runtime.garmin")
    def test_plan_keep_reports_when_there_is_no_plan(self, _mock_garmin):
        test_db.add_objective(
            title="Unplanned", target_date=self._days_out(40), sport_type="running"
        )

        exit_code, stdout, _ = run_cli(["plan", "keep"])

        self.assertEqual(exit_code, 0)
        self.assertIn("No active macrocycle strategy", stdout)


if __name__ == "__main__":
    unittest.main()
