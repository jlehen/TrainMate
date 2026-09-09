"""The surfaces that report a plan built from inputs that have since changed
(DESIGN_plan_staleness.md §9): `plan show` reports it, `plan keep` dismisses it."""

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_plan_stale.db")

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
            # Real fingerprints: goals and the plan-shaping constraints flag the plan too
            # (DESIGN_plan_change_continuity.md §6.5), so a placeholder would make every
            # seeded plan stale.
            goals_hash=coach_service._get_goals_hash(test_db.upcoming_objectives()),
            constraints_hash=coach_service._get_constraints_hash([]),
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
        # The command that actually reaches the days already scheduled (§6.5).
        self.assertIn("'workout generate -d today..' applies it to the days already "
                      "scheduled", flowed)
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

    # --- §10: the diff, and the coach's read ---

    @patch("trainmate.runtime.garmin")
    def test_plan_show_prints_the_edit_itself(self, _mock_garmin):
        """Naming the field is not enough to judge a blob like `preferences`: the old
        and new text are shown, old struck, new added (§10)."""
        self._seed(stale=True)

        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {"reshaping": False, "why": "wording only"}
            exit_code, stdout, _ = run_cli(["plan", "show"])

        self.assertEqual(exit_code, 0)
        self.assertIn("preferences (when the plan was generated)", stdout)
        self.assertIn("preferences (now)", stdout)
        self.assertIn("-a sentence that has since been reworded", stdout)

    @patch("trainmate.runtime.garmin")
    def test_plan_show_asks_the_coach_once_per_edit(self, _mock_garmin):
        """`plan show` is the command whose job is to help the operator decide, so it
        asks — and it stamps nothing, so the answer is cached against the edit rather
        than re-bought on every read (DESIGN_plan_change_continuity.md §7)."""
        self._seed(stale=True)

        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {"reshaping": False, "why": "wording only"}
            _code, first, _ = run_cli(["plan", "show"])
            _code, second, _ = run_cli(["plan", "show"])
            self.assertEqual(client.complete.call_count, 1)

        for stdout in (first, second):
            self.assertIn("keep the plan", " ".join(stdout.split()))
            self.assertIn("wording only", stdout)

    @patch("trainmate.runtime.garmin")
    def test_plan_show_prints_the_plan_when_the_verdict_call_fails(self, _mock_garmin):
        """Fail open, and cache nothing: the plan prints, with no coach line (§7)."""
        self._seed(stale=True)

        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.side_effect = RuntimeError("down")
            exit_code, stdout, _ = run_cli(["plan", "show"])

        self.assertEqual(exit_code, 0)
        self.assertIn("An input has changed", stdout)
        self.assertNotIn("Coach:", stdout)

    @patch("trainmate.runtime.garmin")
    def test_plan_keep_prints_the_edit_it_is_keeping(self, _mock_garmin):
        self._seed(stale=True)

        _code, stdout, _ = run_cli(["plan", "keep"])

        self.assertIn("-a sentence that has since been reworded", stdout)
        self.assertIn("preferences (now)", stdout)

    def _macro(self):
        from trainmate import runtime
        goal = runtime.db.upcoming_objectives()[0]
        return runtime.db.get_macrocycle_for_objective(goal['id'])

    def _confirm_regenerate(self, verdict, *, raises=None):
        """Runs the `plan generate` question against a canned coach reply and returns
        (default the question was asked with, what was printed, the verdict call)."""
        import io
        from contextlib import redirect_stdout
        from trainmate.cli import staleness

        self._seed(stale=True)
        macro = self._macro()
        out = io.StringIO()
        with patch("trainmate.coach.engine.openrouter_client") as client, \
                patch("trainmate.runtime.prompt") as prompt, redirect_stdout(out):
            if raises is not None:
                client.complete.side_effect = raises
            else:
                client.complete.return_value = verdict
            prompt.confirm.return_value = False
            staleness.confirm_regenerate("athlete profile changed: preferences", macro)
        _args, kwargs = prompt.confirm.call_args
        return kwargs.get("default"), out.getvalue(), client.complete

    def test_the_coach_is_asked_with_the_diff_and_the_plan(self):
        """The verdict call is small on purpose: the §2 rubric, the diff, and the blocks
        as they stand — on the coach's own model, under its own journal label."""
        _default, _out, complete = self._confirm_regenerate(
            {"reshaping": False, "why": "Wording only."}
        )

        complete.assert_called_once()
        system_prompt, user_content = complete.call_args[0][:2]
        self.assertEqual(complete.call_args[1]["label"], "plan_verdict")
        self.assertIn("block structure", system_prompt)
        self.assertIn("-a sentence that has since been reworded", user_content)
        self.assertIn("Base (", user_content)
        self.assertIn("strategy", user_content)

    def test_a_keep_verdict_is_shown_and_the_default_stays_no(self):
        default, out, _ = self._confirm_regenerate(
            {"reshaping": False, "why": "Wording only; the blocks stand."}
        )

        self.assertFalse(default)
        self.assertIn("Coach: keep the plan. Wording only; the blocks stand.", out)
        self.assertIn("-a sentence that has since been reworded", out)

    def test_a_reshaping_verdict_flips_the_default_to_yes(self):
        """A coach that says "re-shaping" and a question defaulting to No would be two
        answers on one screen (§10)."""
        default, out, _ = self._confirm_regenerate(
            {"reshaping": True, "why": "The strength block would move earlier."}
        )

        self.assertTrue(default)
        self.assertIn("Coach: re-shaping. The strength block would move earlier.", out)

    def test_a_failed_verdict_leaves_the_question_and_no_read(self):
        """Fail open: the network must never stand between the athlete and the
        question (§10)."""
        default, out, _ = self._confirm_regenerate(None, raises=RuntimeError("down"))

        self.assertFalse(default)
        self.assertNotIn("Coach:", out)
        self.assertIn("block structure, phase order or volume ramp", " ".join(out.split()))

    def test_a_malformed_verdict_counts_as_none(self):
        default, out, _ = self._confirm_regenerate({"reshaping": "yes please"})

        self.assertFalse(default)
        self.assertNotIn("Coach:", out)

    def test_workout_generate_proceeds_by_default_only_when_the_coach_says_keep(self):
        """Proceeding with the stale plan is the "keep" answer, so its default follows
        the coach's read the other way round from `plan generate` (§10)."""
        import io
        from contextlib import redirect_stdout
        from trainmate.cli.workouts.generate import _confirm_out_of_date_plans

        self._seed(stale=True)
        defaults = {}
        for reshaping in (False, True):
            # The verdict is cached against the edit, and the edit does not change
            # between these two runs — so the cache is cleared to ask again (§7).
            test_db.save_reshape_verdict(self._macro()["id"], "", None)
            with patch("trainmate.coach.engine.openrouter_client") as client, \
                    patch("trainmate.runtime.prompt") as prompt, \
                    redirect_stdout(io.StringIO()):
                client.complete.return_value = {"reshaping": reshaping, "why": "x"}
                prompt.confirm.return_value = False
                _confirm_out_of_date_plans(
                    self._days_out(0), self._days_out(6), None, force=False
                )
            defaults[reshaping] = prompt.confirm.call_args[1]["default"]
        self.assertEqual(defaults, {False: True, True: False})

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
