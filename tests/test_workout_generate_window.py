"""The commitment window: what `workout generate` does to the sessions the athlete has
already been told about (DESIGN_plan_change_continuity.md §4, §5.5, §8).

Everything here goes through the real propose/apply pair against a canned model reply,
because the point of the design is what lands in the log and on the Calendar — not what
the coach said. The preview is asserted from the proposal for the same reason: it is
built from what apply will write.
"""
import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests.helpers import clear_all_tables, pin_clock, rebind_test_db, save_workout
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_workout_generate_window.db")

from trainmate.db import Database
from trainmate.sports import canonical_sport

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)

TODAY = "2026-09-09"


def _days_out(n: int) -> str:
    return (
        datetime.strptime(TODAY, "%Y-%m-%d").date() + timedelta(days=n)
    ).strftime("%Y-%m-%d")


class WindowTestCase(unittest.TestCase):
    """A plan, a fixed today, and one canned `workout generate` reply."""

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
        obj_id = test_db.add_objective(
            title="Autumn Marathon", target_date=_days_out(90), sport_type="running",
        )
        test_db.save_macrocycle(
            objective_id=obj_id, strategy="build", goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": "Base", "start_date": _days_out(-20),
                         "end_date": _days_out(60), "focus": "aerobic"}],
        )

    # --- fixtures -----------------------------------------------------------------

    @staticmethod
    def window(days):
        test_db.set_setting("workout_commitment_days", str(days))

    @staticmethod
    def ride(date_str, title="Long ride", **extra):
        return save_workout(
            test_db, date=date_str, sport_type="cycling", title=title,
            description=f"[{title}]\n90 min steady.", duration_minutes=90, rpe=5,
            tss=95, **extra,
        )

    @staticmethod
    def long_run(date_str, title="Long run", **extra):
        return save_workout(
            test_db, date=date_str, sport_type="running", title=title,
            description=f"[{title}]\n60 min.", duration_minutes=60, rpe=6, tss=60,
            **extra,
        )

    @staticmethod
    def rest(date_str, **extra):
        return save_workout(
            test_db, date=date_str, sport_type="rest", title="Rest Day",
            description="[Rest Day]\nNo session planned for this day.",
            duration_minutes=0, rpe=0, tss=0, **extra,
        )

    @staticmethod
    def session(date_str, sport="running", title="Tempo run", minutes=45, **extra):
        entry = {
            "date": date_str, "sport_type": sport, "title": title,
            "description": f"[{title}]\n{minutes} min.", "duration_minutes": minutes,
            "rpe": 6, "tss": 50,
        }
        entry.update(extra)
        return entry

    def generate(self, *entries, start=None, end=None, note=None, apply=True):
        """The whole flow against a canned reply. Returns the proposal."""
        from trainmate import runtime
        response = {"reasoning": "why", "workouts": list(entries)}
        if note:
            response["athlete_note"] = note
        with patch("trainmate.runtime.calendar_syncer"), \
                patch("trainmate.coach.engine.openrouter_client") as client:
            client.complete.return_value = response
            proposal = runtime.coach_service.workout_generate(
                start_date=start, end_date=end
            )
            self.prompt_system = client.complete.call_args.args[0]
            self.prompt_user_content = client.complete.call_args.args[1]
            if apply:
                runtime.coach_service.workout_generate_apply(proposal)
        return proposal

    def live(self, date_str):
        """The live sessions on one date, sport-ordered."""
        rows = test_db.get_workouts(start_date=date_str, end_date=date_str)
        return sorted(rows, key=lambda w: canonical_sport(w["sport_type"]))

    def line(self, proposal, date_str):
        return next(l for l in proposal.standing if l.date == date_str)


class TestTheAnswers(WindowTestCase):
    """Each of the four answers, and what apply writes for it (§4.5)."""

    def test_keep_writes_nothing_and_the_event_carries_on(self):
        lineage = self.ride(_days_out(2))
        before = test_db.get_workout(_days_out(2), "cycling")
        proposal = self.generate(
            {"date": _days_out(2), "sport_type": "cycling", "keep": True}
        )
        after = test_db.get_workout(_days_out(2), "cycling")
        self.assertEqual(after["revision_id"], before["revision_id"])
        self.assertEqual(after["id"], lineage)
        self.assertEqual(self.line(proposal, _days_out(2)).outcome, "kept")

    def test_revise_is_a_revision_on_the_same_lineage(self):
        lineage = self.ride(_days_out(2))
        self.generate(self.session(
            _days_out(2), sport="cycling", title="Easy spin", minutes=60,
            change_reason="never two hard days in a row",
        ))
        after = test_db.get_workout(_days_out(2), "cycling")
        self.assertEqual(after["id"], lineage, "same session, revised")
        self.assertEqual(after["title"], "Easy spin")
        self.assertEqual(after["duration_minutes"], 60)
        # The old form is still in the lineage, which is what the event's History shows.
        forms = [r["title"] for r in test_db.get_lineage_revisions(lineage)]
        self.assertEqual(forms, ["Long ride", "Easy spin"])

    def test_change_reason_reaches_the_rows_reason_column(self):
        self.ride(_days_out(2))
        self.generate(self.session(
            _days_out(2), sport="cycling", title="Easy spin",
            change_reason="never two hard days in a row",
        ))
        after = test_db.get_workout(_days_out(2), "cycling")
        self.assertEqual(
            after["modification_reason"], "never two hard days in a row"
        )

    def test_replaces_moves_the_session_and_its_lineage(self):
        lineage = self.ride(_days_out(2))
        self.generate(self.session(
            _days_out(4), sport="cycling", title="Long ride", minutes=90,
            replaces={"date": _days_out(2), "sport_type": "cycling"},
            change_reason="away on Friday",
        ))
        self.assertEqual([w["sport_type"] for w in self.live(_days_out(2))], ["rest"])
        landed = test_db.get_workout(_days_out(4), "cycling")
        self.assertEqual(landed["id"], lineage, "the event follows the session")
        self.assertEqual(landed["modification_reason"], "away on Friday")

    def test_a_sport_change_keeps_the_lineage_too(self):
        lineage = self.long_run(_days_out(2))
        self.generate(self.session(
            _days_out(2), sport="cycling", title="Easy ride", minutes=60,
            replaces={"date": _days_out(2), "sport_type": "running"},
            change_reason="no running while the knee settles",
        ))
        live = self.live(_days_out(2))
        self.assertEqual([w["sport_type"] for w in live], ["cycling"])
        self.assertEqual(live[0]["id"], lineage)

    def test_drop_is_a_rest_day_on_the_same_lineage_with_the_reason(self):
        lineage = self.ride(_days_out(2))
        proposal = self.generate({
            "date": _days_out(2), "sport_type": "cycling", "drop": True,
            "change_reason": "never two hard days in a row",
        })
        live = self.live(_days_out(2))
        self.assertEqual(len(live), 1, "exactly one live row on the day")
        self.assertEqual(live[0]["sport_type"], "rest")
        self.assertEqual(live[0]["id"], lineage)
        self.assertEqual(
            live[0]["modification_reason"], "never two hard days in a row"
        )
        # The ride is underneath it, which is what the event's History shows.
        self.assertIn(
            "Long ride", [r["title"] for r in test_db.get_lineage_revisions(lineage)]
        )
        self.assertEqual(self.line(proposal, _days_out(2)).outcome, "revised")

    def test_a_session_the_coach_does_not_mention_is_kept_and_flagged(self):
        self.ride(_days_out(2))
        before = test_db.get_workout(_days_out(2), "cycling")
        proposal = self.generate(self.session(_days_out(5)))
        after = test_db.get_workout(_days_out(2), "cycling")
        self.assertEqual(after["revision_id"], before["revision_id"])
        line = self.line(proposal, _days_out(2))
        self.assertEqual(line.outcome, "kept")
        self.assertFalse(line.mentioned)

    def test_a_full_entry_on_a_rest_only_date_replaces_the_rest_day(self):
        """A rest day and a session on the same date cannot both be true, so the coach is
        not offered the choice (§4.5)."""
        lineage = self.rest(_days_out(3))
        self.generate(self.session(
            _days_out(3), change_reason="your profile asks for four sessions a week",
        ))
        live = self.live(_days_out(3))
        self.assertEqual([w["title"] for w in live], ["Tempo run"])
        self.assertEqual(live[0]["id"], lineage, "the rest day's event, retitled")

    def test_a_wording_only_revision_appends_nothing_and_reads_as_kept(self):
        """The no-op rule suppresses it in the write path, so the preview must read the
        comparison and not the answer (§4.5, DESIGN_workout_revisions.md §9)."""
        lineage = self.ride(_days_out(2))
        before = test_db.get_workout(_days_out(2), "cycling")
        proposal = self.generate({
            "date": _days_out(2), "sport_type": "cycling", "title": "Long ride",
            "description": "[Long ride]\n90 min steady.", "duration_minutes": 90,
            "rpe": 5, "tss": 95, "change_reason": "rewritten with more form cues",
        })
        after = test_db.get_workout(_days_out(2), "cycling")
        self.assertEqual(after["revision_id"], before["revision_id"])
        self.assertEqual(len(test_db.get_lineage_revisions(lineage)), 1)
        self.assertEqual(self.line(proposal, _days_out(2)).outcome, "kept")


class TestTheWindow(WindowTestCase):
    """Where the window bites: the days it covers, and the span it intersects (§4.1/§4.2)."""

    def test_zero_protects_nothing(self):
        self.window(0)
        self.ride(_days_out(0))
        proposal = self.generate(self.session(_days_out(5)))
        self.assertEqual(proposal.standing, ())
        self.assertIsNone(proposal.commitment_end)
        # The ride is gone; the coverage backstop leaves an explicit rest day behind it.
        self.assertEqual([w["sport_type"] for w in self.live(_days_out(0))], ["rest"])

    def test_one_covers_today_only(self):
        self.window(1)
        self.ride(_days_out(0))
        self.ride(_days_out(1))
        proposal = self.generate()
        self.assertEqual([l.date for l in proposal.standing], [_days_out(0)])
        self.assertEqual(proposal.commitment_end, _days_out(0))

    def test_seven_covers_today_through_the_sixth_day_after(self):
        self.window(7)
        self.ride(_days_out(6))
        self.ride(_days_out(7))
        proposal = self.generate()
        self.assertEqual([l.date for l in proposal.standing], [_days_out(6)])
        self.assertEqual(proposal.commitment_end, _days_out(6))

    def test_a_forward_selected_span_leaves_the_committed_set_empty(self):
        """Without the second bound the preview would report a drop the apply, bounded to
        the span, could not make (§4.2)."""
        self.ride(_days_out(2))
        proposal = self.generate(
            self.session(_days_out(20)), start=_days_out(20), end=_days_out(25),
        )
        self.assertEqual(proposal.standing, ())
        self.assertFalse(test_db.get_workout(_days_out(2), "cycling")["removed"])

    def test_a_completed_session_today_is_preserved_and_not_re_decided(self):
        self.ride(_days_out(0), title="Morning ride")
        test_db.save_completed_activity(
            activity_id="a1", date=_days_out(0), start_time=f"{_days_out(0)}T07:00:00",
            activity_name="Morning ride", activity_type="cycling", duration_sec=5400,
            distance_km=45.0, elevation_gain_m=300.0, avg_hr=140, max_hr=165, rpe=5,
            tss=95,
        )
        proposal = self.generate(self.session(_days_out(1)))
        self.assertEqual(proposal.gen_start, _days_out(1))
        self.assertNotIn(_days_out(0), [l.date for l in proposal.standing])
        self.assertEqual(
            test_db.get_workout(_days_out(0), "cycling")["title"], "Morning ride"
        )

    def test_a_manual_session_past_the_window_is_still_answered_for(self):
        self.window(2)
        self.long_run(_days_out(15), title="Club run", source="manual")
        proposal = self.generate()
        self.assertEqual([l.date for l in proposal.standing], [_days_out(15)])
        self.assertIn("[ADDED BY THE ATHLETE]", self.prompt_user_content)

    def test_a_benchmark_past_the_window_is_not(self):
        """The coach re-places tests itself out there, from the record it already has
        (§4.2)."""
        self.window(2)
        self.ride(_days_out(15), title="FTP test", benchmark_type="ftp_20min")
        proposal = self.generate()
        self.assertEqual(proposal.standing, ())


class TestTheConflictRules(WindowTestCase):
    """`replaces` is the only answer that can name two slots, so it is the only one with
    conflicts to resolve (§4.5)."""

    def test_two_entries_naming_one_target_keep_the_first(self):
        self.ride(_days_out(2))
        with patch("trainmate.util.notice"):
            self.generate(
                self.session(_days_out(4), sport="cycling", title="First",
                             replaces={"date": _days_out(2), "sport_type": "cycling"},
                             change_reason="a"),
                self.session(_days_out(5), sport="cycling", title="Second",
                             replaces={"date": _days_out(2), "sport_type": "cycling"},
                             change_reason="b"),
            )
        self.assertEqual(
            test_db.get_workout(_days_out(4), "cycling")["title"], "First"
        )
        self.assertIsNone(test_db.get_workout(_days_out(5), "cycling"))

    def test_a_destination_outside_the_span_is_refused_and_the_source_stands(self):
        self.ride(_days_out(2))
        before = test_db.get_workout(_days_out(2), "cycling")
        with patch("trainmate.util.notice"):
            self.generate(
                self.session(_days_out(40), sport="cycling", title="Long ride",
                             replaces={"date": _days_out(2), "sport_type": "cycling"},
                             change_reason="a"),
                start=_days_out(0), end=_days_out(10),
            )
        after = test_db.get_workout(_days_out(2), "cycling")
        self.assertEqual(after["revision_id"], before["revision_id"])

    def test_a_source_outside_the_standing_block_is_refused(self):
        """The run answers only for the days it was shown; the entry is still written
        where it stands."""
        self.window(2)
        self.ride(_days_out(15))
        with patch("trainmate.util.notice"):
            self.generate(
                self.session(_days_out(16), sport="cycling", title="Moved ride",
                             replaces={"date": _days_out(15), "sport_type": "cycling"},
                             change_reason="a"),
            )
        self.assertIsNotNone(test_db.get_workout(_days_out(16), "cycling"))

    def test_a_destination_whose_occupant_is_kept_is_refused_and_both_stand(self):
        self.ride(_days_out(2))
        self.long_run(_days_out(4))
        with patch("trainmate.util.notice"):
            self.generate(
                self.session(_days_out(4), sport="running", title="Moved run",
                             replaces={"date": _days_out(2), "sport_type": "cycling"},
                             change_reason="a"),
                {"date": _days_out(4), "sport_type": "running", "keep": True},
            )
        self.assertEqual(
            test_db.get_workout(_days_out(2), "cycling")["title"], "Long ride"
        )
        self.assertEqual(
            test_db.get_workout(_days_out(4), "running")["title"], "Long run"
        )

    def test_a_destination_whose_occupant_was_dropped_is_free(self):
        ride = self.ride(_days_out(2))
        self.long_run(_days_out(4))
        self.generate(
            self.session(_days_out(4), sport="running", title="Moved ride",
                         replaces={"date": _days_out(2), "sport_type": "cycling"},
                         change_reason="moved to Sunday"),
            {"date": _days_out(4), "sport_type": "running", "drop": True,
             "change_reason": "the long run goes"},
        )
        landed = test_db.get_workout(_days_out(4), "running")
        self.assertEqual(landed["title"], "Moved ride")
        # The moved session brought its own lineage with it.
        self.assertEqual(landed["id"], ride)

    def test_the_displaced_occupants_void_carries_its_own_reason(self):
        """A rest day and a session on the same date cannot both be true, so the drop's
        rest day gives way to the work — and its sentence goes on the void the athlete
        meets instead (§4.5/§5.1)."""
        self.ride(_days_out(2))
        self.long_run(_days_out(4))
        proposal = self.generate(
            self.session(_days_out(4), sport="running", title="Moved ride",
                         replaces={"date": _days_out(2), "sport_type": "cycling"},
                         change_reason="moved to Sunday"),
            {"date": _days_out(4), "sport_type": "running", "drop": True,
             "change_reason": "the long run goes"},
        )
        voided = dict(((d, s), r) for (d, s, r) in proposal.voids)
        self.assertEqual(voided[(_days_out(4), "running")], "the long run goes")
        # And the day holds exactly the one session, not a rest day beside it.
        self.assertEqual(
            [w["title"] for w in self.live(_days_out(4))], ["Moved ride"]
        )
        # The report says the run was cancelled, in the run's own words — not that it
        # "became" the ride that happened to land in its slot.
        run_line = [l for l in proposal.standing if l.sport_type == "running"][0]
        self.assertEqual(run_line.outcome, "cancelled")
        self.assertEqual(run_line.reason, "the long run goes")


class TestTheDeterministicPasses(WindowTestCase):
    """Removals no answer explains still carry a real reason (§5.5)."""

    def test_a_rest_constraint_takes_the_first_lineage_and_voids_the_rest(self):
        ride = self.ride(_days_out(2))
        run = self.long_run(_days_out(2))
        test_db.add_constraint(
            title="Travel", start_date=_days_out(2), end_date=_days_out(2), rest=1,
        )
        proposal = self.generate()
        live = self.live(_days_out(2))
        self.assertEqual([w["sport_type"] for w in live], ["rest"])
        self.assertEqual(live[0]["id"], ride, "the day keeps one event")
        self.assertIn("Travel", live[0]["modification_reason"])
        # The second session is voided under the same reason, and says so.
        voided = dict(((d, s), r) for (d, s, r) in proposal.voids)
        self.assertIn("Travel", voided[(_days_out(2), "running")])
        self.assertEqual(self.line(proposal, _days_out(2)).outcome, "revised")
        run_line = [l for l in proposal.standing if l.sport_type == "running"][0]
        self.assertEqual(run_line.outcome, "cancelled")
        self.assertIn("Travel", run_line.reason)

    def test_a_removal_nothing_explains_still_names_who_did_it(self):
        """Past the window, where the coach writes freely and answers for nothing."""
        self.window(0)
        self.ride(_days_out(2))
        proposal = self.generate(self.session(_days_out(5)))
        reasons = [r for (_d, _s, r) in proposal.voids]
        self.assertIn("Your coach replaced this day.", reasons)


class TestTheEasingTag(WindowTestCase):
    """What the coach is told about a session a prior adaptation eased (§4.6)."""

    def _eased(self, date_str):
        save_workout(
            test_db, date=date_str, sport_type="running", title="Long run",
            description="[Long run]\n60 min easy.", duration_minutes=60, rpe=4, tss=45,
            original_duration_minutes=90, original_rpe=7, original_tss=95,
            modification_reason="short on time",
        )

    def test_inside_the_window_the_tag_says_what_it_was_first_prescribed_as(self):
        self._eased(_days_out(2))
        self.generate()
        self.assertIn("first prescribed as 90m, RPE 7, TSS 95", self.prompt_user_content)

    def test_outside_the_window_the_session_is_not_shown_and_a_rewrite_resets_it(self):
        self.window(1)
        self._eased(_days_out(10))
        self.assertEqual(
            test_db.get_workout(_days_out(10), "running")["adaptation_count"], 1
        )
        self.generate(self.session(_days_out(10), title="Threshold run", minutes=75))
        self.assertNotIn("first prescribed as", self.prompt_user_content)
        after = test_db.get_workout(_days_out(10), "running")
        self.assertEqual(after["title"], "Threshold run")
        self.assertEqual(after["adaptation_count"], 0, "a generate resets the tally")

    def test_a_rest_constraint_outranks_a_fresh_easing(self):
        """The easing answers "how is the athlete today"; a constraint answers "what may
        this athlete do at all", and the second wins (§4.6)."""
        self._eased(_days_out(2))
        test_db.add_constraint(
            title="Ill", start_date=_days_out(2), end_date=_days_out(2), rest=1,
        )
        self.generate({"date": _days_out(2), "sport_type": "running", "keep": True})
        live = self.live(_days_out(2))
        self.assertEqual([w["sport_type"] for w in live], ["rest"])


class TestPastConstraintsReachThePrompt(WindowTestCase):
    """A constraint that ended earlier in the block is why a week went quiet (§6.1)."""

    def test_one_that_ended_before_the_span_renders_under_the_past_heading(self):
        test_db.add_constraint(
            title="Ill", start_date=_days_out(-10), end_date=_days_out(-5), rest=0,
        )
        self.generate()
        self.assertIn("## CONSTRAINTS EARLIER IN THIS BLOCK", self.prompt_user_content)
        self.assertIn("Ill", self.prompt_user_content)

    def test_one_still_active_does_not(self):
        test_db.add_constraint(
            title="Travel", start_date=_days_out(-1), end_date=_days_out(3), rest=0,
        )
        self.generate()
        self.assertNotIn(
            "## CONSTRAINTS EARLIER IN THIS BLOCK", self.prompt_user_content
        )
        # It is still live, so it stays where a live constraint belongs.
        self.assertIn("Travel", self.prompt_system)

    def test_one_before_the_block_started_does_not(self):
        test_db.add_constraint(
            title="Old news", start_date=_days_out(-40), end_date=_days_out(-30), rest=0,
        )
        self.generate()
        self.assertNotIn("Old news", self.prompt_user_content + self.prompt_system)


class TestTheAthleteNote(WindowTestCase):
    """One line about the change as a whole, for the morning push (§6.3)."""

    def test_it_lands_on_the_change_row(self):
        self.ride(_days_out(2))
        self.generate(
            self.session(_days_out(2), sport="cycling", title="Easy spin",
                         change_reason="never two hard days in a row"),
            note="Four sessions a week now, never two hard days in a row.",
        )
        change = test_db.newest_change_with_note()
        self.assertEqual(
            change["note"], "Four sessions a week now, never two hard days in a row."
        )
        self.assertEqual(change["kind"], "generate")

    def test_a_run_that_carries_none_writes_none(self):
        self.generate(self.session(_days_out(5)))
        self.assertIsNone(test_db.newest_change_with_note())


if __name__ == "__main__":
    unittest.main()
