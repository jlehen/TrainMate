"""Honoring a constraint the daily adapt cannot reach (DESIGN_constraint_reschedule.md).

Covers the §5 window arithmetic, the §7 write model (an adapt sibling, with the two-sided
clamp and no `adapted_at` stamp), and the §10 whole-window preview that makes the confirm
honest.
"""
import io
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from tests.helpers import clear_all_tables, pin_clock, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_accommodate.db")

from trainmate.db import Database
import trainmate.db
import trainmate.coach

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

from trainmate.coach import coach_service, honoring
from trainmate.coach.proposals import AccommodationPass

# trainmate_cli re-exports names from trainmate.cli.workouts, so it must be imported first.
import trainmate_cli  # noqa: F401
from trainmate.cli.workouts import accommodate as accommodate_cli
from trainmate.cli.workouts import revisions as revisions_cli

TODAY = "2026-06-01"


class AccommodateCase(unittest.TestCase):
    """Shared fixture: a frozen clock, one wide block, and a stubbed LLM."""

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
        pin_clock(self, TODAY)
        self._save_plan([{
            "name": "Base", "start_date": "2026-01-01",
            "end_date": "2026-12-31", "focus": "Aerobic base",
        }])

    @staticmethod
    def _clear_plans():
        with test_db._get_connection() as conn:
            for table in ("mesocycles", "macrocycles", "objectives"):
                conn.execute(f"DELETE FROM {table}")
            conn.commit()

    @staticmethod
    def _save_plan(mesocycles, target_date="2026-12-31"):
        obj_id = test_db.add_objective(
            title="Goal", target_date=target_date, sport_type="running", priority=1,
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="General preparation.",
            goals_hash="bg", constraints_hash="bg", mesocycles=mesocycles,
        )
        return obj_id

    def _accommodate(self, decision, start, end, constraint_ids=None):
        """Runs one pass with the LLM stubbed, returning the proposal."""
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = decision
            with redirect_stdout(io.StringIO()):
                proposal = coach_service.workout_accommodate(
                    start, end, constraint_ids=constraint_ids
                )
            self.prompt = client.complete.call_args[0]
        return proposal


class TestWindowArithmetic(AccommodateCase):
    """§5 — the constraint's own dates, widened by the spill margin, clipped to tomorrow."""

    def test_the_window_is_the_constraint_widened_by_the_spill_margin(self):
        self.assertEqual(
            honoring.constraint_window("2026-06-10", "2026-06-20", TODAY),
            ("2026-06-08", "2026-06-22"),
        )

    def test_a_window_already_under_way_is_clipped_to_tomorrow(self):
        # Today is adapt's, judged with the full metrics picture; a metric-blind command
        # must not race it there, and the past cannot be re-planned at all.
        self.assertEqual(
            honoring.constraint_window("2026-05-20", "2026-06-10", TODAY),
            ("2026-06-02", "2026-06-12"),
        )

    def test_a_constraint_ending_today_has_no_window_left(self):
        self.assertIsNone(
            honoring.constraint_window("2026-05-28", TODAY, TODAY)
        )

    def test_the_spill_margin_is_configurable(self):
        with patch.dict(trainmate.coach.config.data,
                        {"coach": {"accommodate_spill_days": 0}}):
            self.assertEqual(
                honoring.constraint_window("2026-06-10", "2026-06-20", TODAY),
                ("2026-06-10", "2026-06-20"),
            )


class TestScope(AccommodateCase):
    """§5 — what the pass may see and what it refuses."""

    def test_a_straddling_constraint_is_one_pass_with_both_blocks_in_context(self):
        self._clear_plans()
        self._save_plan([
            {"name": "Base 2", "start_date": "2026-05-01", "end_date": "2026-06-14",
             "focus": "Aerobic volume"},
            {"name": "Build 1", "start_date": "2026-06-15", "end_date": "2026-07-31",
             "focus": "Threshold"},
        ])
        cid = test_db.add_constraint(
            title="Away", start_date="2026-06-10", end_date="2026-06-20", rest=0,
        )
        self._accommodate({"change_needed": False, "reason": "ok"},
                          "2026-06-08", "2026-06-22", [cid])
        system_prompt = self.prompt[0]
        # One pass, with the name and focus of BOTH blocks, so the model knows which side
        # of the seam displaced load belongs on.
        self.assertIn("Base 2", system_prompt)
        self.assertIn("Build 1", system_prompt)
        self.assertIn("Threshold", system_prompt)

    def test_a_neighbouring_constraint_in_the_spill_margin_reaches_the_prompt(self):
        honoring = test_db.add_constraint(
            title="Away", start_date="2026-06-10", end_date="2026-06-12", rest=0,
        )
        test_db.add_constraint(
            title="Surgery, no training", start_date="2026-06-14",
            end_date="2026-06-20", rest=1,
        )
        test_db.save_workout("2026-06-14", "running", "Tempo", "40min",
                             duration_minutes=40, rpe=6, tss=45)
        proposal = self._accommodate(
            {"change_needed": False, "reason": "ok"},
            "2026-06-08", "2026-06-14", [honoring],
        )
        # A spill day may belong to a neighbouring rest window, and a pass that cannot see
        # that constraint would move load onto it.
        self.assertIn("Surgery, no training", self.prompt[0])
        # ...and the rest pre-pass reaches it too, deterministically.
        self.assertEqual(
            [(w["date"], w["sport_type"]) for w in proposal.workouts],
            [("2026-06-14", "rest")],
        )

    def test_it_refuses_when_no_block_covers_the_window(self):
        # The strict reader answers empty here; the governing readers would have handed
        # back a neighbouring block — right for generate, wrong here.
        self._clear_plans()
        self._save_plan([{
            "name": "Base", "start_date": "2026-01-01", "end_date": "2026-03-31",
            "focus": "Aerobic base",
        }], target_date="2026-03-31")
        with self.assertRaises(ValueError) as ctx:
            with redirect_stdout(io.StringIO()):
                coach_service.workout_accommodate("2026-06-08", "2026-06-22")
        self.assertIn("plan generate", str(ctx.exception))

    def test_a_window_straddling_the_plans_end_is_evaluated_in_full(self):
        """A session in conflict past the plan's end is still cleared (§5): the
        constraint's own dates are authority enough over its own days. And a pass that
        saw the whole window covers the constraint (§8) — safe to stamp, because
        `workout generate` builds around every stored constraint when the plan is later
        extended, stamped or not."""
        self._clear_plans()
        self._save_plan([{
            "name": "Base", "start_date": "2026-01-01", "end_date": "2026-06-15",
            "focus": "Aerobic base",
        }], target_date="2026-06-15")
        test_db.save_workout("2026-06-20", "running", "Easy run", "40min",
                             duration_minutes=40, rpe=3, tss=25)
        cid = test_db.add_constraint(
            title="Away", start_date="2026-06-12", end_date="2026-06-25", rest=1,
        )
        proposal = self._accommodate(
            {"change_needed": False, "reason": "ok"}, "2026-06-10", "2026-06-27", [cid]
        )
        self.assertEqual(proposal.range_end, "2026-06-27")
        # The rest pre-pass reaches the ungoverned tail deterministically.
        self.assertIn(("2026-06-20", "rest"),
                      [(w["date"], w["sport_type"]) for w in proposal.workouts])
        self.assertEqual(proposal.covered_constraint_ids, (cid,))


class TestWriteModel(AccommodateCase):
    """§7 — an adapt sibling: in-place, two-sided clamp, and never an easing."""

    def _one_move(self):
        test_db.save_workout("2026-06-10", "cycling", "Long ride", "[Long ride]\n3h Z2",
                             duration_minutes=180, rpe=5, tss=140)
        cid = test_db.add_constraint(
            title="Away Wednesday", start_date="2026-06-10", end_date="2026-06-10", rest=0,
        )
        return cid, {
            "change_needed": True,
            "reason": "Moved the long ride off the travel day.",
            "revised_workouts": [
                {"date": "2026-06-11", "sport_type": "cycling", "title": "Long ride",
                 "description": "[Long ride]\n3h Z2", "duration_minutes": 180,
                 "rpe": 5, "tss": 140, "change_reason": "Moved — away Wednesday."},
                {"date": "2026-06-10", "sport_type": "cycling", "title": "Rest",
                 "description": "[Rest]\nTravel day.", "duration_minutes": 0,
                 "rpe": 0, "tss": 0, "change_reason": "Long ride moved to Thursday."},
            ],
        }

    def test_a_move_leaves_adapted_at_unstamped(self):
        cid, decision = self._one_move()
        proposal = self._accommodate(decision, "2026-06-08", "2026-06-12", [cid])
        self.assertFalse(proposal.stamp_adapted_at)
        with redirect_stdout(io.StringIO()):
            coach_service.workout_revision_apply(proposal)
        # The trigger is not fatigue at all, so a reschedule must never make tomorrow's
        # genuine easing look like a compounded one — including the session it MOVED,
        # which has no same-sport predecessor on its new date.
        for date in ("2026-06-10", "2026-06-11"):
            row = test_db.get_workout(date, "cycling")
            self.assertIsNone(row["adapted_at"], date)

    def test_an_adapt_still_stamps_adapted_at(self):
        # The opt-out rides on the proposal, so the shared path is unchanged for adapt.
        test_db.save_workout("2026-06-10", "cycling", "Long ride", "3h",
                             duration_minutes=180, rpe=5, tss=140)
        from trainmate.coach.proposals import RevisionProposal
        with redirect_stdout(io.StringIO()):
            coach_service.workout_revision_apply(RevisionProposal(
                reason="Eased.",
                workouts=[{"date": "2026-06-10", "sport_type": "cycling",
                           "title": "Easy ride", "description": "1h",
                           "original_description": "1h", "modification_reason": "Eased.",
                           "adaptation_summary": "Eased.", "google_event_id": None,
                           "duration_minutes": 60, "rpe": 3, "tss": 40,
                           "benchmark_type": None}],
                new_constraints=[], range_start="2026-06-10", range_end="2026-06-12",
            ))
        self.assertIsNotNone(test_db.get_workout("2026-06-10", "cycling")["adapted_at"])

    def test_a_proposal_dated_today_is_dropped_by_the_two_sided_clamp(self):
        test_db.save_workout(TODAY, "running", "Tempo", "40min",
                             duration_minutes=40, rpe=6, tss=45)
        cid = test_db.add_constraint(
            title="Away", start_date="2026-06-10", end_date="2026-06-10", rest=0,
        )
        proposal = self._accommodate({
            "change_needed": True,
            "reason": "Rebalanced.",
            "revised_workouts": [
                {"date": TODAY, "sport_type": "running", "title": "Easy run",
                 "description": "20min", "duration_minutes": 20, "rpe": 2, "tss": 15,
                 "change_reason": "Freshen up for the week."},
            ],
        }, "2026-06-08", "2026-06-12", [cid])
        # Adapt clamps only the TOP of its range, because its window starts at the
        # evaluation date. This window starts tomorrow, so both sides are clamped — and
        # that filter is the only thing enforcing §5's "adapt owns today".
        self.assertEqual(proposal.workouts, [])

    def test_a_vacated_date_is_re_filled(self):
        cid, decision = self._one_move()
        proposal = self._accommodate(decision, "2026-06-08", "2026-06-12", [cid])
        with redirect_stdout(io.StringIO()):
            coach_service.workout_revision_apply(proposal)
        vacated = test_db.get_workout("2026-06-10", "cycling")
        self.assertEqual(vacated["title"], "Rest")
        self.assertIn("moved", vacated["modification_reason"].lower())

    def test_a_benchmark_moves_intact_and_its_replacement_is_not_the_test(self):
        test_db.save_workout("2026-06-10", "cycling", "FTP test", "20min test",
                             duration_minutes=60, rpe=9, tss=90, benchmark_type="ftp")
        cid = test_db.add_constraint(
            title="Away Wednesday", start_date="2026-06-10", end_date="2026-06-10", rest=0,
        )
        proposal = self._accommodate({
            "change_needed": True,
            "reason": "Test moved off the travel day.",
            "revised_workouts": [
                {"date": "2026-06-11", "sport_type": "cycling", "title": "FTP test",
                 "description": "20min test", "duration_minutes": 60, "rpe": 9, "tss": 90,
                 "benchmark_type": "ftp", "change_reason": "Moved — away Wednesday."},
                {"date": "2026-06-10", "sport_type": "cycling", "title": "Rest",
                 "description": "Travel day.", "duration_minutes": 0, "rpe": 0, "tss": 0,
                 "benchmark_type": None, "change_reason": "Test moved to Thursday."},
            ],
        }, "2026-06-08", "2026-06-12", [cid])
        with redirect_stdout(io.StringIO()):
            coach_service.workout_revision_apply(proposal)
        self.assertEqual(
            test_db.get_workout("2026-06-11", "cycling")["benchmark_type"], "ftp"
        )
        # The flag belongs to the TEST, not to the slot.
        self.assertIsNone(test_db.get_workout("2026-06-10", "cycling")["benchmark_type"])


class TestPasses(AccommodateCase):
    """§4.1 — how a set of directives becomes the passes that will actually run, and the
    typed reasons the rest will not. On the service: deciding how many coach calls to
    make over which ranges is not a rendering question."""

    def _plan_for(self, *constraints):
        return coach_service.accommodation_plan(TODAY, list(constraints))

    def test_overlapping_spill_widened_windows_merge_into_one_pass(self):
        a = test_db.get_constraint(test_db.add_constraint(
            title="Away", start_date="2026-06-10", end_date="2026-06-12"))
        b = test_db.get_constraint(test_db.add_constraint(
            title="Course", start_date="2026-06-15", end_date="2026-06-17"))
        plan = self._plan_for(a, b)
        # 06-08..06-14 and 06-13..06-19 overlap: a second pass over shared days would
        # preview a plan the first had not yet applied.
        self.assertEqual(plan.spent, ())
        self.assertEqual(plan.ungoverned, ())
        self.assertEqual(len(plan.passes), 1)
        one = plan.passes[0]
        self.assertEqual((one.range_start, one.range_end), ("2026-06-08", "2026-06-19"))
        self.assertEqual([c["id"] for c in one.constraints], [a["id"], b["id"]])

    def test_windows_that_do_not_overlap_stay_separate_passes(self):
        a = test_db.get_constraint(test_db.add_constraint(
            title="Away", start_date="2026-06-10", end_date="2026-06-12"))
        b = test_db.get_constraint(test_db.add_constraint(
            title="Course", start_date="2026-07-01", end_date="2026-07-03"))
        plan = self._plan_for(a, b)
        self.assertEqual([(p.range_start, p.range_end) for p in plan.passes],
                         [("2026-06-08", "2026-06-14"), ("2026-06-29", "2026-07-05")])

    def test_a_constraint_with_nothing_left_of_it_is_named_not_swept(self):
        spent = test_db.get_constraint(test_db.add_constraint(
            title="Yesterday", start_date="2026-05-30", end_date=TODAY))
        plan = self._plan_for(spent)
        self.assertEqual(plan.passes, ())
        self.assertEqual([c["id"] for c in plan.spent], [spent["id"]])

    def test_a_constraint_in_a_GAP_between_blocks_is_refused_by_name(self):
        """The reason the refusal is typed rather than inferred. A gap is inside the
        plan's span, so the old `start_date > plan_end` test called it workable; the pass
        then died mid-loop claiming there was no periodization at all (§4.1)."""
        self._clear_plans()
        self._save_plan([
            {"name": "Base", "start_date": "2026-01-01", "end_date": "2026-06-30",
             "focus": "Aerobic base"},
            {"name": "Build", "start_date": "2026-09-01", "end_date": "2026-12-31",
             "focus": "Threshold"},
        ])
        in_gap = test_db.get_constraint(test_db.add_constraint(
            title="Away", start_date="2026-07-10", end_date="2026-07-15"))
        governed = test_db.get_constraint(test_db.add_constraint(
            title="Course", start_date="2026-06-10", end_date="2026-06-12"))
        plan = self._plan_for(in_gap, governed)
        self.assertEqual([c["id"] for c in plan.ungoverned], [in_gap["id"]])
        self.assertEqual([c["id"] for p in plan.passes for c in p.constraints],
                         [governed["id"]])
        # The horizon still reaches the far block, so "beyond the end" would be wrong.
        self.assertEqual(plan.plan_end, "2026-12-31")

    def test_a_window_straddling_the_plans_end_still_runs_in_full(self):
        """Governance is asked of the merged window, not of every day in it: a pass with
        any governed day runs, over the whole window (§5)."""
        self._clear_plans()
        self._save_plan([{"name": "Base", "start_date": "2026-01-01",
                          "end_date": "2026-06-15", "focus": "Aerobic base"}],
                        target_date="2026-06-15")
        straddling = test_db.get_constraint(test_db.add_constraint(
            title="Travelling", start_date="2026-06-10", end_date="2026-06-25"))
        plan = self._plan_for(straddling)
        self.assertEqual(plan.ungoverned, ())
        self.assertEqual(len(plan.passes), 1)


class TestPreview(AccommodateCase):
    """§10 — the preview renders the whole window, not only the rows that changed."""

    def _proposal_with_untouched_days(self):
        test_db.save_workout("2026-06-09", "running", "Easy run", "40min",
                             duration_minutes=40, rpe=3, tss=25)
        test_db.save_workout("2026-06-10", "cycling", "Long ride", "3h",
                             duration_minutes=180, rpe=5, tss=140)
        cid = test_db.add_constraint(
            title="Away Wednesday", start_date="2026-06-10", end_date="2026-06-10", rest=0,
        )
        # A move that emits ONLY its destination — the §9 vacate failure.
        return self._accommodate({
            "change_needed": True,
            "reason": "Moved the ride.",
            "revised_workouts": [
                {"date": "2026-06-11", "sport_type": "cycling", "title": "Long ride",
                 "description": "3h", "duration_minutes": 180, "rpe": 5, "tss": 140,
                 "change_reason": "Moved — away Wednesday."},
            ],
        }, "2026-06-08", "2026-06-12", [cid])

    def _render(self, proposal, whole_window):
        buf = io.StringIO()
        with patch("trainmate.runtime.prompt") as prompt:
            prompt.confirm.return_value = False
            with redirect_stdout(buf):
                accommodate_cli.preview_and_confirm_revision(
                    proposal, "HEADING:", "Apply?", whole_window=whole_window
                )
        return buf.getvalue()

    def test_the_whole_window_render_shows_days_the_proposal_does_not_touch(self):
        out = self._render(self._proposal_with_untouched_days(), True)
        self.assertIn("2026-06-09", out)
        self.assertIn("(unchanged)", out)

    def test_a_move_that_emits_only_its_destination_shows_the_session_twice(self):
        out = self._render(self._proposal_with_untouched_days(), True)
        # This is the claim that makes the confirm honest: the leftover sits on a date the
        # proposal never mentions, so a changed-rows table cannot show it.
        self.assertEqual(out.count("Long ride"), 2)
        self.assertIn("2026-06-10", out)

    def test_the_changed_rows_render_shows_only_what_changed(self):
        out = self._render(self._proposal_with_untouched_days(), False)
        self.assertNotIn("2026-06-09", out)
        self.assertNotIn("(unchanged)", out)


if __name__ == "__main__":
    unittest.main()


class TestHonoringIsRecorded(AccommodateCase):
    """§8 — a pass that proposed nothing still had the constraint in scope, which is all
    `honored_at` claims. Without this the sweep re-offers it forever, one LLM call a
    time, answering "already works around this" every time."""

    def _constraint(self, start="2026-06-10", end="2026-06-20", title="Away", replan=0):
        return test_db.add_constraint(
            start_date=start, end_date=end, rest=1, title=title, description="d",
            replan=replan,
        )

    def _run_pass(self, decision, constraint_id):
        constraint = test_db.get_constraint(constraint_id)
        window = honoring.constraint_window(
            constraint["start_date"], constraint["end_date"], TODAY
        )
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = decision
            with redirect_stdout(io.StringIO()) as out:
                accommodate_cli.run_accommodation_pass(
                    AccommodationPass(window[0], window[1], (constraint,)), auto=True
                )
        return out.getvalue()

    def test_a_pass_that_proposes_nothing_still_records_the_coach_pass(self):
        cid = self._constraint()
        out = self._run_pass({"change_needed": False, "reason": "Already clear."}, cid)
        self.assertIn("already work around this", out)
        self.assertIsNotNone(test_db.get_constraint(cid)["honored_at"])
        # ...so the sweep stops offering it.
        self.assertEqual(honoring.constraints_needing_a_pass(test_db, TODAY), [])

    def test_an_applied_pass_records_it_too(self):
        cid = self._constraint()
        test_db.save_workout("2026-06-11", "running", "Tempo", "40min",
                             duration_minutes=40, rpe=6, tss=45)
        self._run_pass({
            "change_needed": True, "reason": "Moved.",
            "revised_workouts": [
                {"date": "2026-06-11", "sport_type": "rest", "title": "Rest",
                 "description": "Away.", "change_reason": "Away."},
            ],
        }, cid)
        self.assertIsNotNone(test_db.get_constraint(cid)["honored_at"])

    def test_a_plan_shaping_constraint_is_not_swept(self):
        """`replan = 1` belongs to the plan-shaping tier: `workout generate` stamps it
        when its horizon reaches it, and `_maybe_point_at_honor` already refuses to offer
        the window tier for one at add time (§3/§10)."""
        self._constraint(title="Big trip", replan=1)
        self.assertEqual(honoring.constraints_needing_a_pass(test_db, TODAY), [])


class TestTheWindowActuallyEvaluated(AccommodateCase):
    """§5/§7 — a plan ending inside a window narrows nothing: the athlete is told the
    plan ran out, and the whole window is still evaluated, in one pass, and stamped."""

    def test_a_window_running_past_the_plan_warns_and_evaluates_in_full(self):
        self._clear_plans()
        self._save_plan([{
            "name": "Base", "start_date": "2026-01-01",
            "end_date": "2026-06-15", "focus": "Aerobic base",
        }], target_date="2026-06-15")
        cid = test_db.add_constraint(
            start_date="2026-06-10", end_date="2026-06-25", rest=0,
            title="Travelling", description="d",
        )
        constraint = test_db.get_constraint(cid)
        # Through `accommodation_plan`, so the pass carries the blocks the warning and
        # the propose call both read — governance decided once (§4.2).
        plan = coach_service.accommodation_plan(TODAY, [constraint])
        (pass_,) = plan.passes
        with patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {"change_needed": False, "reason": "Clear."}
            with redirect_stdout(io.StringIO()) as out:
                accommodate_cli.run_accommodation_pass(pass_, auto=True)
        printed = out.getvalue()
        self.assertIn("The plan runs out on 2026-06-15", printed)
        # The verdict names the window's own end: every day of it was evaluated.
        verdict = printed[printed.index("already work around this") - 200:]
        self.assertIn("2026-06-27", verdict)
        # ...and a pass that saw the whole window records it, so the sweep does not
        # re-offer the constraint forever while the plan stays short (§8).
        self.assertIsNotNone(test_db.get_constraint(cid)["honored_at"])


class TestNamingTheConstraints(AccommodateCase):
    """§4 — `-c` names the subject; every other form names a window to find it in.

    This is the whole of what the separate `constraint honor` command used to be.
    """

    @staticmethod
    def _args(*argv):
        parser, _ = trainmate_cli.build_parser()
        return parser.parse_args(["workout", "accommodate", *argv])

    def _run(self, *argv):
        # `pin_clock` reaches the service and the db, not the CLI's own bound name.
        with patch.object(accommodate_cli, "_today_str", return_value=TODAY), \
                patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = {"change_needed": False, "reason": "Clear."}
            with redirect_stdout(io.StringIO()) as out:
                accommodate_cli.run_workout_accommodate(self._args(*argv))
            self.calls = client.complete.call_count
        return out.getvalue()

    def _away(self):
        return test_db.add_constraint(
            title="Away", start_date="2026-06-10", end_date="2026-06-12"
        )

    def test_a_named_constraint_runs_even_when_already_stamped(self):
        # The re-honor case: after hand-editing the window's sessions you ask again, and
        # the §8 filter that governs the sweep must not silently answer "nothing to do".
        cid = self._away()
        test_db.mark_honored(cid)
        self._run("-c", str(cid), "-y")
        self.assertEqual(self.calls, 1)

    def test_the_sweep_leaves_that_same_constraint_alone(self):
        cid = self._away()
        test_db.mark_honored(cid)
        out = self._run("-y")
        self.assertEqual(self.calls, 0)
        self.assertIn("already reflects every constraint", out)

    def test_an_unknown_id_is_named_and_nothing_runs(self):
        out = self._run("-c", "999", "-y")
        self.assertIn("No constraint with ID 999", out)
        self.assertEqual(self.calls, 0)

    def test_naming_a_constraint_and_a_window_at_once_is_refused(self):
        # They answer the same question two ways; taking both would mean picking one
        # silently.
        out = self._run("-c", str(self._away()), "-d", "2026-06-10..2026-06-20")
        self.assertIn("Use one or the other", out)
        self.assertEqual(self.calls, 0)

    def test_a_named_constraint_no_block_covers_is_pointed_at_plan_generate(self):
        # Named rather than refused mid-loop: the plan buckets it as `ungoverned` before
        # any coach call is spent, and the CLI says what to do about it (§4.1).
        self._clear_plans()
        self._save_plan([{
            "name": "Base", "start_date": "2026-01-01",
            "end_date": "2026-06-30", "focus": "Aerobic base",
        }])
        cid = test_db.add_constraint(
            title="Trip", start_date="2026-08-01", end_date="2026-08-05"
        )
        out = self._run("-c", str(cid), "-y")
        self.assertIn("no block in your plan", out)
        self.assertIn("plan generate", out)
        self.assertIn("[%d] Trip" % cid, out)
        self.assertEqual(self.calls, 0)
