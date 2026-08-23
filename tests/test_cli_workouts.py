import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, run_cli, rebind_test_db
from trainmate.cli.common import fmt_date
from trainmate.coach.proposals import RevisionProposal, GenerateProposal
from trainmate.coach.revisions import RevisionPair

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_cli_workouts.db")

from trainmate.db import Database
import trainmate.db
import trainmate_cli

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

PROPOSED_DATE = (datetime.now(timezone.utc).date() + timedelta(days=1)).strftime("%Y-%m-%d")


def _proposal(displaced=()):
    """One proposed session, as `workout generate` hands it to the CLI for preview."""
    return GenerateProposal(
        reasoning="Reasoning",
        workouts=({
            "date": PROPOSED_DATE, "sport_type": "running", "title": "Base Run",
            "description": "45 min easy", "duration_minutes": 45, "tss": 40, "rpe": 4,
        },),
        displaced=tuple(displaced),
        gen_start=datetime.now(timezone.utc).date().strftime("%Y-%m-%d"),
    )


class TestCliWorkouts(unittest.TestCase):
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

    @patch("trainmate.runtime.garmin")
    @patch("trainmate.runtime.coach_service")
    def test_workout_commands(self, mock_coach, mock_garmin):
        adapted = {
            "date": "2026-06-03",
            "sport_type": "running",
            "title": "Steady Ride",
            "duration_minutes": 60,
            "rpe": 5,
            "tss": 40.0,
        }
        mock_coach.workout_adapt.return_value = RevisionProposal(
            reason="Metrics are green",
            workouts=[adapted],
            new_constraints=[],
            range_start="2026-06-03",
            range_end="2026-06-30",
            pairs=(RevisionPair(proposal=adapted, original=None, is_swap=False),),
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT SCHEDULE ===", stdout)
        self.assertNotIn("Description:", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "adapt", "--auto"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Evaluating daily Garmin metrics adaptation", stdout)
        self.assertIn("Metrics are green", stdout)
        self.assertIn("Adaptations applied and synced to calendar successfully.", stdout)
        mock_coach.workout_adapt.assert_called_once()
        # With no -m, the athlete message threads through as None.
        self.assertIsNone(mock_coach.workout_adapt.call_args.kwargs.get("message"))

        # -m/--message is forwarded verbatim to the service for this run.
        mock_coach.workout_adapt.reset_mock()
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "adapt", "--auto", "-m", "knee is sore, keep impact low"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_coach.workout_adapt.call_args.kwargs.get("message"),
            "knee is sore, keep impact low",
        )

        w_id = test_db.save_workout(
            date="2026-06-02",
            sport_type="running",
            title="Interval Session",
            description="5x800m",
            
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "rm", str(w_id), "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn(
            f"Workout with ID {w_id} ('Interval Session') removed successfully", stdout
        )

    def _seed_pmc_metrics(self, n_days: int) -> None:
        """n_days of metrics ending today, each carrying the PMC triple."""
        today = datetime.now().date()
        for i in range(n_days):
            test_db.save_metric_cache(
                date=(today - timedelta(days=n_days - 1 - i)).isoformat(),
                rhr=50, hrv=70, sleep_score=80, stress=20,
                ctl=62.4, atl=71.7, tsb=-8.9,
            )

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.coach_service")
    def test_adapt_reports_metric_day_count_not_values(self, mock_coach, _mock_ensure):
        # The coach still reads the full trajectory; the CLI only tells the athlete how
        # many days fed the decision and never prints the raw per-day numbers.
        mock_coach.workout_adapt.return_value = RevisionProposal(
            reason="Metrics are green", workouts=[], new_constraints=[],
            range_start="2026-06-01", range_end="2026-06-30",
        )
        self._seed_pmc_metrics(90)

        exit_code, stdout, stderr = self.run_cli(["workout", "adapt", "--lookback", "3"])

        self.assertEqual(exit_code, 0)
        self.assertNotIn("Could not load metrics trajectory", stdout)
        self.assertIn("Using 3 days of recovery metrics", stdout)
        # No raw HRV/RHR/PMC values leak into the terse summary.
        for value in ("62.4", "71.7", "-8.9"):
            self.assertNotIn(value, stdout)

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_push_command(self, mock_calendar):
        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No new or modified workouts to sync", stdout)
        mock_calendar.sync_multiple.assert_not_called()

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo Run",
            description="30 mins fast", 
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Syncing 1 workouts to Google Calendar", stdout)
        mock_calendar.sync_multiple.assert_called_once()

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_push_warns_about_stale_past_workouts(self, mock_calendar):
        """`push` defaults to today onward, so a past row left stale by a failed push
        has nothing that would re-push it. It must at least be surfaced."""
        from trainmate.calendar_state import calendar_signature
        today = datetime.now(timezone.utc).date()
        past = (today - timedelta(days=4)).strftime("%Y-%m-%d")

        wid = test_db.save_workout(
            date=past, sport_type="running", title="Old Run", description="easy",
        )
        test_db.mark_workout_pushed(
            wid, "evt-past", calendar_signature(test_db.get_workout_by_id(wid))
        )
        # A push that never landed: content moves on, signature does not.
        test_db.save_workout(
            date=past, sport_type="running", title="Old Run", description="HARD",
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertIn("1 workout before", stdout)
        self.assertIn("[STALE]", stdout)
        self.assertIn(f"workout push -d {past}..", stdout)
        # Warning only — the past row stays outside the pushed window.
        mock_calendar.sync_multiple.assert_not_called()

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_push_no_warning_when_past_is_clean(self, mock_calendar):
        """A past workout that is synced (or was never pushed) must not warn."""
        today = datetime.now(timezone.utc).date()
        past = (today - timedelta(days=4)).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=past, sport_type="running", title="Old Run", description="easy",
        )
        exit_code, stdout, stderr = self.run_cli(["workout", "push"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("still read [STALE]", stdout)

    @patch("trainmate.runtime.coach_service")
    def test_workout_swap_by_date(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        mock_coach.workout_swap_validate.return_value = []
        # The real service returns whole workout rows; the swap echo renders them in
        # the 'workout list' format, so the stubs carry the same shape.
        mock_coach.workout_swap_apply.return_value = [
            {"id": 1, "title": "Run A", "date": d2, "sport_type": "running",
             "duration_minutes": 45, "rpe": 4, "tss": 30},
            {"id": 2, "title": "Ride B", "date": d1, "sport_type": "cycling",
             "duration_minutes": 60, "rpe": 4, "tss": 30},
        ]
        a = test_db.save_workout(
            date=d1, sport_type="running", title="Run A",
            description="easy", rpe=4, tss=30,
        )
        b = test_db.save_workout(
            date=d2, sport_type="cycling", title="Ride B",
            description="easy", rpe=4, tss=30,
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", d1, d2, "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Swapped 2 workout(s) successfully", stdout)
        # Each moved session is echoed in the 'workout list' format, at its new date.
        self.assertIn(f"ID: 1 | {fmt_date(d2)} | RUNNING | Run A", stdout)
        self.assertIn(f"ID: 2 | {fmt_date(d1)} | CYCLING | Ride B", stdout)
        # Each date's workout is moved to the other date.
        ops, no_sync = mock_coach.workout_swap_apply.call_args[0]
        self.assertCountEqual(ops, [
            {"id": a, "new_date": d2},
            {"id": b, "new_date": d1},
        ])
        self.assertFalse(no_sync)

    @patch("trainmate.runtime.coach_service")
    def test_workout_swap_by_id_no_sync(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        mock_coach.workout_swap_validate.return_value = []
        mock_coach.workout_swap_apply.return_value = []
        a = test_db.save_workout(
            date=d1, sport_type="running", title="Run A",
            description="easy", rpe=4, tss=30,
        )
        b = test_db.save_workout(
            date=d2, sport_type="cycling", title="Ride B",
            description="easy", rpe=4, tss=30,
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", str(a), str(b), "Travelling", "--no-sync"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Calendar sync skipped", stdout)
        ops, no_sync = mock_coach.workout_swap_apply.call_args[0]
        self.assertCountEqual(ops, [
            {"id": a, "new_date": d2},
            {"id": b, "new_date": d1},
        ])
        self.assertTrue(no_sync)

    @patch("trainmate.runtime.coach_service")
    def test_workout_swap_warning_declined(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=3)).strftime("%Y-%m-%d")
        mock_coach.workout_swap_validate.return_value = ["Creates 3 consecutive high days"]
        a = test_db.save_workout(
            date=d1, sport_type="running", title="Run A",
            description="easy", rpe=8, tss=90,
        )
        b = test_db.save_workout(
            date=d2, sport_type="cycling", title="Ride B",
            description="easy", rpe=8, tss=90,
        )
        # Default input is "n": the swap is cancelled and never applied.
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", d1, d2, "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Swap warnings", stdout)
        self.assertIn("Swap cancelled", stdout)
        mock_coach.workout_swap_apply.assert_not_called()

    @patch("trainmate.runtime.coach_service")
    def test_workout_swap_past_date_rejected(self, mock_coach):
        today = datetime.now(timezone.utc).date()
        past = (today - timedelta(days=2)).strftime("%Y-%m-%d")
        future = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=past, sport_type="running", title="Run A",
            description="easy", rpe=4, tss=30,
        )
        test_db.save_workout(
            date=future, sport_type="cycling", title="Ride B",
            description="easy", rpe=4, tss=30,
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", past, future, "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("in the past", stdout)
        mock_coach.workout_swap_apply.assert_not_called()

    def test_workout_swap_missing_args(self):
        # Both targets and the reason are positional and mandatory, so argparse stops
        # the run before any handler and answers with the command's own help, the
        # missing line last (DESIGN_cli_noargs.md §a).
        exit_code, stdout, stderr = self.run_cli(["workout", "swap", "2026-06-10"])
        self.assertEqual(exit_code, 2)
        self.assertIn("positional arguments:", stderr)
        self.assertIn("Why the workouts are being swapped", stderr)
        self.assertIn(
            "the following arguments are required: target2, reason",
            stderr.strip().splitlines()[-1],
        )

    def test_workout_swap_mixed_date_and_id_rejected(self):
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", "2026-06-10", "7", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("not one of each", stdout)

    def test_workout_swap_invalid_target_rejected(self):
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "swap", "tomorrow", "friday", "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("neither a date", stdout)

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_rm_synced(self, mock_calendar):
        w_id = test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Synced Run",
            description="30 mins", google_event_id="mock_event_123",
        )
        exit_code, stdout, stderr = self.run_cli(
            ["workout", "rm", str(w_id), "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "Workout is synced to Google Calendar. Updating calendar event",
            stdout,
        )
        self.assertIn(f"Workout with ID {w_id} ('Synced Run') removed successfully", stdout)
        mock_calendar.sync_workout.assert_called_once()
        synced_workout = mock_calendar.sync_workout.call_args[0][0]
        self.assertEqual(synced_workout['id'], w_id)
        self.assertTrue(synced_workout['removed'])

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_rm_soft_deletes(self, mock_calendar):
        """`workout rm` marks the row removed (kept in DB), hides it from reads, and
        updates its calendar event — but it stays retrievable for the coach."""
        w_id = test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Interval Session",
            description="5x800m", google_event_id="evt-1",
        )
        exit_code, stdout, _ = self.run_cli(
            ["workout", "rm", str(w_id), "Travelling for work"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Reason: Travelling for work", stdout)

        # Row is kept, flagged removed, reason stored, and retains calendar event reference.
        row = test_db.get_workout_by_id(w_id)
        self.assertIsNotNone(row)
        self.assertTrue(row["removed"])
        self.assertEqual(row["removed_reason"], "Travelling for work")
        self.assertEqual(row["google_event_id"], "evt-1")

        # Excluded from default reads, retrievable with include_removed=True.
        self.assertEqual(test_db.get_workouts(start_date="2026-06-02", end_date="2026-06-02"), [])
        self.assertEqual(
            len(test_db.get_workouts(
                start_date="2026-06-02", end_date="2026-06-02", include_removed=True
            )),
            1,
        )

        # Removing an already-removed workout is a no-op that does not re-hit the calendar.
        mock_calendar.sync_workout.reset_mock()
        exit_code, stdout, _ = self.run_cli(
            ["workout", "rm", str(w_id), "Travelling"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("already removed", stdout)
        mock_calendar.sync_workout.assert_not_called()

        # Verify CLI list command excludes or includes the removed workout depending on --removed.
        exit_code, stdout, _ = self.run_cli(
            ["workout", "list", "-d", "2026-06-02"]
        )
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Interval Session", stdout)

        exit_code, stdout, _ = self.run_cli(
            ["workout", "list", "-d", "2026-06-02", "--removed"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Interval Session", stdout)
        self.assertIn("[REMOVED]", stdout)

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_wipe(self, mock_calendar):
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", google_event_id="ge_1",
        )
        test_db.save_workout(
            date="2026-06-03", sport_type="running", title="Run 2",
            description="30 mins", 
        )
        self.assertEqual(len(test_db.get_workouts()), 2)

        exit_code, stdout, stderr = self.run_cli(["workout", "wipe"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 2)
        mock_calendar.delete_workout_event.assert_not_called()

        exit_code, stdout, stderr = self.run_cli(["workout", "wipe"], input_value="y")
        self.assertEqual(exit_code, 0)
        self.assertIn("All workouts wiped successfully.", stdout)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_1")

        mock_calendar.reset_mock()
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", google_event_id="ge_2",
        )
        exit_code, stdout, stderr = self.run_cli(["workout", "wipe", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(len(test_db.get_workouts()), 0)
        mock_calendar.delete_workout_event.assert_called_once_with("ge_2")

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_prune_calendar(self, mock_calendar):
        # One live workout, one soft-removed (keeps its event), and two calendar
        # events no row claims — the fresh-database case.
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", google_event_id="ge_live",
        )
        removed_id = test_db.save_workout(
            date="2026-06-03", sport_type="running", title="Run 2",
            description="30 mins", google_event_id="ge_removed",
        )
        test_db.mark_workout_removed(removed_id, "not today")

        mock_calendar.list_workout_events.return_value = [
            {"id": "ge_live", "summary": "Run 1", "start": {"date": "2026-06-02"}},
            {"id": "ge_removed", "summary": "[Deleted] Run 2", "start": {"date": "2026-06-03"}},
            {"id": "ge_orphan_a", "summary": "Old Ride", "start": {"date": "2026-05-01"}},
            {"id": "ge_orphan_b", "summary": "Old Swim", "start": {"date": "2026-06-10"}},
        ]
        mock_calendar.delete_event.return_value = True

        # Dry run reports both orphans and deletes nothing.
        exit_code, stdout, _ = self.run_cli(["workout", "prune-calendar", "--dry-run"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Old Ride", stdout)
        self.assertIn("Old Swim", stdout)
        self.assertNotIn("Run 1", stdout)
        self.assertNotIn("[Deleted] Run 2", stdout)
        mock_calendar.delete_event.assert_not_called()

        # Declining the confirmation deletes nothing either.
        exit_code, stdout, _ = self.run_cli(["workout", "prune-calendar"], input_value="n")
        self.assertEqual(exit_code, 0)
        self.assertIn("Prune cancelled.", stdout)
        mock_calendar.delete_event.assert_not_called()

        # A date window bounds which orphans go.
        exit_code, stdout, _ = self.run_cli(
            ["workout", "prune-calendar", "-d", "2026-06-01..", "-y"]
        )
        self.assertEqual(exit_code, 0)
        self.assertIn("Pruned 1 orphaned Calendar event.", stdout)
        mock_calendar.delete_event.assert_called_once_with("ge_orphan_b")

        # Unbounded, the remaining orphan goes and the claimed events survive.
        mock_calendar.reset_mock()
        mock_calendar.delete_event.return_value = True
        exit_code, stdout, _ = self.run_cli(["workout", "prune-calendar", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Pruned 2 orphaned Calendar events.", stdout)
        self.assertEqual(
            sorted(c.args[0] for c in mock_calendar.delete_event.call_args_list),
            ["ge_orphan_a", "ge_orphan_b"],
        )

    @patch("trainmate.runtime.calendar_syncer")
    def test_workout_prune_calendar_nothing_to_do(self, mock_calendar):
        test_db.save_workout(
            date="2026-06-02", sport_type="running", title="Run 1",
            description="30 mins", google_event_id="ge_live",
        )
        mock_calendar.list_workout_events.return_value = [
            {"id": "ge_live", "summary": "Run 1", "start": {"date": "2026-06-02"}},
        ]
        exit_code, stdout, _ = self.run_cli(["workout", "prune-calendar", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No orphaned Calendar events", stdout)
        mock_calendar.delete_event.assert_not_called()

    @patch("trainmate.runtime.coach_service")
    def test_workout_add_echoes_list_line(self, mock_coach):
        """`add` echoes the new session in the exact 'workout list' rendering."""
        saved = {
            "id": 7, "date": "2026-06-02", "sport_type": "running",
            "title": "Tempo 6x800", "description": "intervals",
            "duration_minutes": 60, "tss": 70, "rpe": 7, "source": "manual",
            "google_event_id": "evt-1",
        }
        mock_coach.workout_add.return_value = (saved, [])
        exit_code, stdout, _ = self.run_cli([
            "workout", "add", "2026-06-02", "running", "Tempo 6x800",
            "--description", "intervals", "--duration", "60", "--tss", "70", "--rpe", "7",
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn(
            "ID: 7 | 2026-06-02 Tue | RUNNING | Tempo 6x800", stdout
        )
        self.assertIn("[MANUAL]", stdout)
        self.assertIn("60min", stdout)
        self.assertIn("TSS 70", stdout)
        self.assertIn("RPE 7", stdout)
        self.assertIn("Workout added successfully", stdout)

    def test_workout_list_filters(self):
        today_date = datetime.now(timezone.utc).date()
        today_str = today_date.strftime("%Y-%m-%d")
        tomorrow_str = (today_date + timedelta(days=1)).strftime("%Y-%m-%d")
        past_str = (today_date - timedelta(days=5)).strftime("%Y-%m-%d")
        future_str = (today_date + timedelta(days=10)).strftime("%Y-%m-%d")

        test_db.save_workout(
            date=today_str, sport_type="running", title="Today Run",
            description="30 mins", 
        )
        test_db.save_workout(
            date=tomorrow_str, sport_type="cycling", title="Tomorrow Ride",
            description="60 mins", 
        )
        test_db.save_workout(
            date=past_str, sport_type="yoga", title="Past Yoga",
            description="15 mins", 
        )
        test_db.save_workout(
            date=future_str, sport_type="strength_training", title="Future Lift",
            description="45 mins", 
        )

        goal_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date=(today_date + timedelta(days=20)).strftime("%Y-%m-%d"),
            sport_type="running",
            status="active",
        )
        test_db.save_macrocycle(
            objective_id=goal_id,
            strategy="Base strategy",
            goals_hash="ghash",
            constraints_hash="lhash",
            mesocycles=[{
                "name": "Base Building",
                "start_date": (today_date - timedelta(days=2)).strftime("%Y-%m-%d"),
                "end_date": (today_date + timedelta(days=5)).strftime("%Y-%m-%d"),
                "focus": "Aerobic conditioning",
            }],
        )

        macro = test_db.get_macrocycle_for_objective(goal_id)
        mesos = test_db.get_mesocycles_for_macrocycle(macro["id"])
        meso_id = mesos[0]["id"]

        exit_code, stdout, stderr = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "--type", "running"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertNotIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)

        exit_code, stdout, stderr = self.run_cli(["workout", "list", "-d", "2d"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "-d", tomorrow_str
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Past Yoga", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "-m", str(meso_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "-m", f"..{meso_id}"
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "-g", str(goal_id)
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Future Lift", stdout)

        # Bare -m is the current block, bounded both ends; -m ID.. keeps the end open.
        exit_code, stdout, stderr = self.run_cli(["workout", "list", "-m"])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertNotIn("Future Lift", stdout)

        exit_code, stdout, stderr = self.run_cli([
            "workout", "list", "-m", f"{meso_id}.."
        ])
        self.assertEqual(exit_code, 0)
        self.assertNotIn("Past Yoga", stdout)
        self.assertIn("Today Run", stdout)
        self.assertIn("Tomorrow Ride", stdout)
        self.assertIn("Future Lift", stdout)

    def test_workout_list_shows_repeat_adapt_count(self):
        """A session eased once reads [ADAPTED]; eased again reads [ADAPTED ×2]."""
        today_str = datetime.now(timezone.utc).date().strftime("%Y-%m-%d")
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo",
            description="orig", adaptation_summary="block too hard",
            modification_reason="eased", adapted_at="2026-06-17T08:00:00+00:00",
        )
        exit_code, stdout, _ = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("[ADAPTED]", stdout)
        self.assertNotIn("[ADAPTED ×", stdout)
        # The lifecycle line is verbose-only; the default listing is one line per workout.
        self.assertNotIn("Planned:", stdout)

        # Lifecycle line under -v: creation stamp always shown, last-adapted stamp when eased.
        exit_code, stdout_v, _ = self.run_cli(["workout", "list", "-v"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Planned:", stdout_v)
        self.assertIn("Last adapted: 2026-06-17 08:00", stdout_v)

        # Second easing of the same slot bumps the count.
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo",
            description="easier", adaptation_summary="still fatigued",
            modification_reason="eased again", adapted_at="2026-06-18T08:00:00+00:00",
        )
        exit_code, stdout, _ = self.run_cli(["workout", "list"])
        self.assertEqual(exit_code, 0)
        self.assertIn("[ADAPTED ×2]", stdout)

    def test_workout_compare(self):
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        two_days_ago = today - timedelta(days=2)
        yesterday_str = yesterday.strftime("%Y-%m-%d")
        two_days_ago_str = two_days_ago.strftime("%Y-%m-%d")
        today_str = today.strftime("%Y-%m-%d")

        # A run two days ago never done (a real miss — that day is over), a run
        # yesterday that was (will be matched), and a run today not done YET, which
        # is pending rather than missed: the day has not finished.
        test_db.save_workout(
            date=two_days_ago_str, sport_type="running", title="Skipped Run",
            description="40 mins", duration_minutes=40, rpe=5, tss=30,
        )
        test_db.save_workout(
            date=yesterday_str, sport_type="running", title="Easy Run",
            description="30 mins", duration_minutes=30, rpe=4, tss=20,
        )
        test_db.save_workout(
            date=today_str, sport_type="running", title="Tempo Run",
            description="45 mins", duration_minutes=45, rpe=7, tss=50,
        )
        # Complete yesterday's run (matching load/duration — no discrepancy expected)
        test_db.save_completed_activity(
            activity_id="act_cmp_1",
            date=yesterday_str,
            start_time=f"{yesterday_str} 08:00:00",
            activity_name="Morning Run",
            activity_type="running",
            duration_sec=1800.0,
            distance_km=5.0,
            elevation_gain_m=50.0,
            avg_hr=140,
            max_hr=160,
            rpe=4,
            tss=20.0,
        )

        exit_code, stdout, stderr = self.run_cli(["workout", "compare", "-d", "3d"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== WORKOUT COMPARE ===", stdout)
        self.assertIn("Easy Run", stdout)
        self.assertIn("Morning Run", stdout)
        self.assertIn("Tempo Run", stdout)
        # The finished day reads as a miss; today's untrained session does not.
        self.assertIn("(none — missed)", stdout)
        self.assertIn("(not yet — still ahead today)", stdout)
        self.assertIn("=== DISCREPANCIES ===", stdout)
        self.assertIn("Complete Miss! Missed planned workout 'Skipped Run'", stdout)
        self.assertNotIn("Tempo Run' (running)", stdout)

        # Date range with no data → empty message
        exit_code, stdout, stderr = self.run_cli([
            "workout", "compare", "-d", "2020-01-01..2020-01-02"
        ])
        self.assertEqual(exit_code, 0)
        self.assertIn("No planned workouts or completed activities found", stdout)

    # Colour on: `informational` holds activity dicts, so a raw gray(dict) only blows
    # up on a terminal — piped output short-circuits colorize and hides the bug.
    @patch("trainmate.util.is_color_enabled", return_value=True)
    def test_workout_compare_outside_any_plan(self, _color):
        """Activities on dates no mesocycle covers are rendered as formatted lines,
        not raw dicts."""
        yesterday_str = (
            datetime.now(timezone.utc).date() - timedelta(days=1)
        ).strftime("%Y-%m-%d")

        # No mesocycles saved -> no date is covered, and nothing is planned that day,
        # so this lands in the informational bucket.
        test_db.save_completed_activity(
            activity_id="act_cmp_info",
            date=yesterday_str,
            start_time=f"{yesterday_str} 08:00:00",
            activity_name="Off-Season Ride",
            activity_type="road_biking",
            duration_sec=3600.0,
            distance_km=30.0,
            elevation_gain_m=100.0,
            avg_hr=140,
            max_hr=170,
            rpe=None,
            tss=60.0,
        )

        exit_code, stdout, _ = self.run_cli(["workout", "compare", "-d", "2d"])
        self.assertEqual(exit_code, 0)
        self.assertIn("=== OUTSIDE ANY PLAN (informational) ===", stdout)
        self.assertIn(f"- {yesterday_str}: [road_biking] Off-Season Ride", stdout)
        self.assertNotIn("'activity_id'", stdout)

    @patch("trainmate.runtime.coach_service")
    def test_workout_batches_and_rollback(self, mock_coach):
        """`workout batches` lists archived batches and `workout rollback` picks one
        (see DESIGN_plan_rollback.md §9)."""
        exit_code, stdout, _ = self.run_cli(["workout", "batches"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No archived workouts", stdout)

        # Nothing archived yet: rollback declines without reaching the service.
        exit_code, stdout, _ = self.run_cli(["workout", "rollback"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No archived workouts to roll back to", stdout)
        mock_coach.workout_rollback.assert_not_called()

        today = datetime.now(timezone.utc).date()
        future_str = (today + timedelta(days=2)).strftime("%Y-%m-%d")
        test_db.save_workout(
            date=future_str, sport_type="running", title="Archived Run",
            description="45 mins", duration_minutes=45,
        )
        test_db.archive_future_workouts(today.strftime("%Y-%m-%d"))
        stamp = test_db.get_archived_batches()[0]["archived_at"]

        exit_code, stdout, _ = self.run_cli(["workout", "batches"])
        self.assertEqual(exit_code, 0)
        self.assertIn("#1", stdout)
        self.assertIn("1 workout(s)", stdout)
        # Everything numbered is history: the archive above emptied the live plan, so no
        # `live` row is printed alongside it.
        self.assertNotIn("in force", stdout)

        # With upcoming sessions again, they show as an unnumbered `live` row — the plan in
        # force is never one of the restorable batches (DESIGN_plan_rollback.md §9).
        test_db.save_workout(
            date=future_str, sport_type="running", title="Current Run",
            description="30 mins", duration_minutes=30,
        )
        exit_code, stdout, _ = self.run_cli(["workout", "batches"])
        self.assertEqual(exit_code, 0)
        live_row = next(ln for ln in stdout.splitlines() if "in force" in ln)
        self.assertIn("live", live_row)
        self.assertNotIn("#", live_row)  # never numbered: --batch cannot address it
        self.assertIn("#1", stdout)

        # An out-of-range batch number is refused before anything is archived.
        exit_code, stdout, _ = self.run_cli(["workout", "rollback", "--batch", "9"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No archived batch #9", stdout)
        mock_coach.workout_rollback.assert_not_called()

        mock_coach.workout_rollback.return_value = {
            "batch": stamp, "restored_workouts": 1, "archived_workouts": 0,
            "first_date": future_str, "last_date": future_str, "unhonored": [],
        }
        exit_code, stdout, _ = self.run_cli(["workout", "rollback", "-y"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Restored 1 workout(s)", stdout)
        # The CLI resolves the positional #N to the batch's timestamp key.
        self.assertEqual(
            mock_coach.workout_rollback.call_args.kwargs.get("batch"), stamp
        )
        # Calendar chatter is off by default (a count and a progress bar stand in for it)
        # and -v turns the per-event lines back on.
        self.assertFalse(mock_coach.workout_rollback.call_args.kwargs.get("verbose"))
        self.run_cli(["workout", "rollback", "-y", "-v"])
        self.assertTrue(mock_coach.workout_rollback.call_args.kwargs.get("verbose"))

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.prompt")
    @patch("trainmate.runtime.coach_service")
    def test_generate_confirms_before_replacing_live_plan(
        self, mock_coach, mock_prompt, _ensure
    ):
        """A regen is archive-and-rebuild, so an existing upcoming plan is confirmed
        before the LLM call; --force skips the prompt."""
        mock_coach.workout_generate.return_value = _proposal()
        today = datetime.now(timezone.utc).date()
        d1 = (today + timedelta(days=1)).strftime("%Y-%m-%d")
        d2 = (today + timedelta(days=5)).strftime("%Y-%m-%d")

        # Empty plan: nothing to lose, so no prompt stands between the athlete and the
        # coach — the only question asked is the apply gate, after the preview.
        mock_prompt.confirm.return_value = False
        exit_code, _, _ = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        mock_prompt.confirm.assert_called_once()
        mock_coach.workout_generate.assert_called_once()

        test_db.save_workout(
            date=d1, sport_type="running", title="Tempo", description="30 min",
        )
        test_db.save_workout(
            date=d2, sport_type="running", title="Long", description="90 min",
            source="manual",
        )

        # Declining leaves the live plan alone and never spends the LLM call.
        mock_prompt.confirm.reset_mock()
        mock_coach.workout_generate.reset_mock()
        exit_code, stdout, _ = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        # The question is wrapped for the terminal; compare on a single logical line.
        question = " ".join(mock_prompt.confirm.call_args.args[0].split())
        self.assertIn("You already have 2 upcoming workout(s) planned", question)
        self.assertIn(fmt_date(d1), question)
        self.assertIn(fmt_date(d2), question)
        self.assertIn("1 added by hand", question)
        self.assertIn("your current plan is unchanged", stdout)
        mock_coach.workout_generate.assert_not_called()

        # Accepting proceeds.
        mock_prompt.confirm.return_value = True
        exit_code, _, _ = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        mock_coach.workout_generate.assert_called_once()

        # --force skips both questions, even when confirm would decline.
        mock_prompt.confirm.reset_mock()
        mock_prompt.confirm.return_value = False
        mock_coach.workout_generate.reset_mock()
        mock_coach.workout_generate_apply.reset_mock()
        exit_code, _, _ = self.run_cli(["workout", "generate", "--force"])
        self.assertEqual(exit_code, 0)
        mock_prompt.confirm.assert_not_called()
        mock_coach.workout_generate.assert_called_once()
        mock_coach.workout_generate_apply.assert_called_once()

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.prompt")
    @patch("trainmate.runtime.coach_service")
    def test_generate_previews_the_workouts_then_asks_before_writing(
        self, mock_coach, mock_prompt, _ensure
    ):
        """The proposed sessions are shown the way `workout list` shows them, and nothing
        is written until the athlete accepts."""
        today = datetime.now(timezone.utc).date()
        displaced = {
            "id": 7, "date": today.strftime("%Y-%m-%d"), "sport_type": "running",
            "title": "Old Tempo", "description": "30 min",
        }
        proposal = _proposal(displaced=(displaced,))
        mock_coach.workout_generate.return_value = proposal

        # Declining writes nothing.
        mock_prompt.confirm.return_value = False
        exit_code, stdout, _ = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("WORKOUTS PROPOSED BY COACH", stdout)
        # Rendered by the same `workout_line` the listing uses — the date, the sport, the
        # title and the load, with no ID, since the session has no row yet.
        self.assertIn(fmt_date(PROPOSED_DATE), stdout)
        self.assertIn("RUNNING", stdout)
        self.assertIn("Base Run", stdout)
        self.assertIn("45min", stdout)
        self.assertNotIn("ID:", stdout)
        self.assertIn("Workouts discarded", stdout)
        mock_coach.workout_generate_apply.assert_not_called()

        # The apply question names what it would archive.
        question = " ".join(mock_prompt.confirm.call_args.args[0].split())
        self.assertIn("Schedule these 1 workout(s)", question)
        self.assertIn("archives the 1 session(s)", question)

        # Accepting hands the very same proposal to the writer — the preview and the
        # write cannot disagree about what is scheduled.
        mock_prompt.confirm.return_value = True
        mock_coach.workout_generate_apply.return_value = [displaced]
        exit_code, stdout, _ = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIs(mock_coach.workout_generate_apply.call_args.args[0], proposal)
        self.assertIn("Scheduled 1 workout(s)", stdout)
        # Same verbosity contract as rollback: quiet by default, per-event lines under -v.
        self.assertFalse(mock_coach.workout_generate_apply.call_args.kwargs["verbose"])
        self.run_cli(["workout", "generate", "-v"])
        self.assertTrue(mock_coach.workout_generate_apply.call_args.kwargs["verbose"])

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.prompt")
    @patch("trainmate.runtime.coach_service")
    def test_generate_with_no_proposed_sessions_asks_nothing(
        self, mock_coach, mock_prompt, _ensure
    ):
        """A coach that proposes nothing must not archive the live plan for an empty
        rebuild — there is nothing to apply, so there is nothing to ask."""
        mock_coach.workout_generate.return_value = GenerateProposal(
            reasoning="No active goals found."
        )
        mock_prompt.confirm.return_value = True
        exit_code, stdout, _ = self.run_cli(["workout", "generate"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No active goals found.", stdout)
        self.assertIn("nothing to apply", stdout)
        mock_prompt.confirm.assert_not_called()
        mock_coach.workout_generate_apply.assert_not_called()

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.prompt")
    @patch("trainmate.runtime.coach_service")
    def test_generate_force_keeps_out_of_date_plan_warning(
        self, mock_coach, mock_prompt, _ensure
    ):
        """--force proceeds past the out-of-date-plan warning without stamping the
        config hash — only an explicit confirmation accepts the stale plan."""
        mock_coach.workout_generate.return_value = _proposal()
        mock_coach.config_changed.return_value = "athlete profile changed"
        obj_id = test_db.add_objective(
            title="London Marathon", target_date="2026-09-20",
            sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="Build then taper", goals_hash="g",
            constraints_hash="c",
            mesocycles=[{
                "name": "Base", "start_date": "2026-06-01", "end_date": "2026-06-28",
                "focus": "Aerobic volume",
            }],
        )

        # Declining stops before the LLM call and leaves the plan flagged as stale.
        mock_prompt.confirm.return_value = False
        with patch.object(test_db, "update_macrocycle_config_hash") as mock_stamp:
            exit_code, stdout, _ = self.run_cli(["workout", "generate"])
            self.assertEqual(exit_code, 0)
            self.assertIn("Workout generation cancelled. Please run", stdout)
            mock_stamp.assert_not_called()
        mock_coach.workout_generate.assert_not_called()

        # --force proceeds, says why, and still leaves the warning live for next time.
        with patch.object(test_db, "update_macrocycle_config_hash") as mock_stamp:
            exit_code, stdout, _ = self.run_cli(["workout", "generate", "-f"])
            self.assertEqual(exit_code, 0)
            self.assertIn("Proceeding anyway (--force)", stdout)
            mock_stamp.assert_not_called()
        mock_coach.workout_generate.assert_called_once()

    def _goal_with_plan(self, target_days_out: int, block_days_out: int = 20):
        today_date = datetime.now(timezone.utc).date()
        goal_id = test_db.add_objective(
            title="Autumn Marathon",
            target_date=(
                today_date + timedelta(days=target_days_out)
            ).strftime("%Y-%m-%d"),
            sport_type="running", status="active",
        )
        macro_id = test_db.save_macrocycle(
            objective_id=goal_id, strategy="Build", goals_hash="g", constraints_hash="c",
            mesocycles=[{
                "name": "Base",
                "start_date": today_date.strftime("%Y-%m-%d"),
                "end_date": (
                    today_date + timedelta(days=block_days_out)
                ).strftime("%Y-%m-%d"),
                "focus": "Aerobic",
            }],
        )
        return goal_id, macro_id

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.prompt")
    @patch("trainmate.runtime.coach_service")
    def test_generate_g_is_the_horizon_not_a_plan_selector(
        self, mock_coach, mock_prompt, _ensure
    ):
        """`-g` reads like it does everywhere else in the grammar: generate through this
        goal's target date. It replaced `--until-goal`, and it no longer picks which plan
        applies — the dates do that (DESIGN_cli_selectors.md §8)."""
        mock_coach.workout_generate.return_value = _proposal()
        mock_coach.config_changed.return_value = None
        goal_id, _ = self._goal_with_plan(target_days_out=100)
        target_date = test_db.get_objective(goal_id)["target_date"]

        exit_code, _, _ = self.run_cli(["workout", "generate", "-g", str(goal_id), "-f"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_coach.workout_generate.call_args.kwargs["end_date"], target_date
        )

        # Bare -g is the active goal, the same shorthand every other command gives it.
        mock_coach.workout_generate.reset_mock()
        exit_code, _, _ = self.run_cli(["workout", "generate", "-g", "-f"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_coach.workout_generate.call_args.kwargs["end_date"], target_date
        )

        # The retired flag is gone rather than silently ignored.
        exit_code, _, stderr = self.run_cli(["workout", "generate", "--until-goal"])
        self.assertNotEqual(exit_code, 0)
        self.assertIn("unrecognized arguments", stderr)

    @patch("trainmate.cli.workouts.generate.ensure_recent_data")
    @patch("trainmate.runtime.prompt")
    @patch("trainmate.runtime.coach_service")
    def test_generate_passes_a_named_plan_through_as_the_tiebreaker(
        self, mock_coach, mock_prompt, _ensure
    ):
        """`-M ID` bounds the horizon *and* settles which plan to follow where two cover
        the same days; a bare -M names no single winner, so it does not."""
        mock_coach.workout_generate.return_value = _proposal()
        mock_coach.config_changed.return_value = None
        _, macro_id = self._goal_with_plan(target_days_out=100)

        exit_code, _, _ = self.run_cli(
            ["workout", "generate", "-M", str(macro_id), "-f"]
        )
        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_coach.workout_generate.call_args.kwargs["prefer_macro_id"], macro_id
        )

        mock_coach.workout_generate.reset_mock()
        exit_code, _, _ = self.run_cli(["workout", "generate", "-M", "-f"])
        self.assertEqual(exit_code, 0)
        self.assertIsNone(
            mock_coach.workout_generate.call_args.kwargs["prefer_macro_id"]
        )
