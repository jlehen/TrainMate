import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from tests.helpers import clear_all_tables, pin_clock, unstamp_schema, rebind_test_db

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_db.db")

from trainmate.db import (
    Database, normalize_sports, valid_confidence, learning_is_dormant,
    derive_confidence, confidence_rank, step_down, RETIRE_PROPOSAL,
)
import trainmate.db
from trainmate.db.objectives import goal_state

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class TestDatabase(unittest.TestCase):
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

    def test_completed_activities_crud(self):
        test_db.save_completed_activity(
            activity_id="act_123",
            date="2026-06-03",
            start_time="2026-06-03 08:00:00",
            activity_name="Morning Run",
            activity_type="running",
            duration_sec=3600.0,
            distance_km=10.0,
            elevation_gain_m=100.0,
            avg_hr=150,
            max_hr=170,
            rpe=7,
            tss=60.0,
        )

        activities = test_db.get_completed_activities(
            start_date="2026-06-01", end_date="2026-06-05"
        )
        self.assertEqual(len(activities), 1)
        act = activities[0]
        self.assertEqual(act["activity_id"], "act_123")
        self.assertEqual(act["activity_name"], "Morning Run")
        self.assertEqual(act["rpe"], 7)
        self.assertEqual(act["tss"], 60.0)

        # Upsert: same activity_id updates the row
        test_db.save_completed_activity(
            activity_id="act_123",
            date="2026-06-03",
            start_time="2026-06-03 08:00:00",
            activity_name="Morning Run Updated",
            activity_type="running",
            duration_sec=3600.0,
            distance_km=10.0,
            elevation_gain_m=100.0,
            avg_hr=150,
            max_hr=170,
            rpe=8,
            tss=65.0,
        )

        activities = test_db.get_completed_activities()
        self.assertEqual(len(activities), 1)
        self.assertEqual(activities[0]["activity_name"], "Morning Run Updated")
        self.assertEqual(activities[0]["rpe"], 8)
        self.assertEqual(activities[0]["tss"], 65.0)

    def test_workout_workload_fields(self):
        w_id = test_db.save_workout(
            date="2026-06-03",
            sport_type="running",
            title="Tempo Run",
            description="30 mins at LTHR",
            duration_minutes=45,
            rpe=7,
            tss=50,
        )

        workout = test_db.get_workout_by_id(w_id)
        self.assertIsNotNone(workout)
        self.assertEqual(workout["duration_minutes"], 45)
        self.assertEqual(workout["rpe"], 7)
        self.assertEqual(workout["tss"], 50)
        # Original load snapshot is seeded from the inserted values.
        self.assertEqual(workout["original_duration_minutes"], 45)
        self.assertEqual(workout["original_rpe"], 7)
        self.assertEqual(workout["original_tss"], 50)

    def test_original_load_snapshot_preserved_across_adaptation(self):
        # The planned-load snapshot is captured once and survives later easings, so the
        # plan can always compare the current load against where the session started.
        test_db.save_workout(
            date="2026-06-04",
            sport_type="running",
            title="Intervals",
            description="6x3min",
            duration_minutes=60,
            rpe=8,
            tss=90,
        )
        # First adaptation: load is walked down; originals must hold their planned values.
        test_db.save_workout(
            date="2026-06-04",
            sport_type="running",
            title="Intervals",
            description="3x3min",
            duration_minutes=40,
            rpe=6,
            tss=55,
            adapted_at="2026-06-03T09:00:00+00:00",
        )
        w = test_db.get_workout("2026-06-04", "running")
        self.assertEqual(w["duration_minutes"], 40)
        self.assertEqual(w["original_duration_minutes"], 60)
        self.assertEqual(w["original_rpe"], 8)
        self.assertEqual(w["original_tss"], 90)

        # Second adaptation: still pinned to the original plan, not the intermediate cut.
        test_db.save_workout(
            date="2026-06-04",
            sport_type="running",
            title="Intervals",
            description="easy 20min",
            duration_minutes=20,
            rpe=4,
            tss=25,
            adapted_at="2026-06-03T10:00:00+00:00",
        )
        w = test_db.get_workout("2026-06-04", "running")
        self.assertEqual(w["original_duration_minutes"], 60)
        self.assertEqual(w["original_rpe"], 8)
        self.assertEqual(w["original_tss"], 90)

    def test_macrocycles_cascade_delete(self):
        obj_id = test_db.add_objective(
            title="Berlin Marathon",
            target_date="2026-09-27",
            sport_type="running",
            priority=1,
        )

        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))

        macro_id = test_db.save_macrocycle(
            objective_id=obj_id,
            strategy="aerobic focus plan",
            goals_hash="hashgoals123",
            constraints_hash="hashconstraints456",
            mesocycles=[
                {"name": "Base Building", "start_date": "2026-06-01",
                 "end_date": "2026-06-28", "focus": "Aerobic threshold"},
                {"name": "Specific prep", "start_date": "2026-06-29",
                 "end_date": "2026-07-26", "focus": "Intensity increase"},
            ],
        )
        self.assertIsNotNone(macro_id)

        macro = test_db.get_macrocycle_for_objective(obj_id)
        self.assertEqual(macro["strategy"], "aerobic focus plan")

        mesocycles = test_db.get_mesocycles_for_macrocycle(macro["id"])
        self.assertEqual(len(mesocycles), 2)
        self.assertEqual(mesocycles[0]["name"], "Base Building")
        self.assertEqual(mesocycles[1]["name"], "Specific prep")

        test_db.delete_objective(obj_id)
        self.assertIsNone(test_db.get_macrocycle_for_objective(obj_id))
        self.assertEqual(len(test_db.get_mesocycles_for_macrocycle(macro_id)), 0)

    # --- Coach learnings (Phase 2 enrichment) ---

    def test_learning_enrichment_defaults_and_fields(self):
        lid = test_db.add_learning(
            "Handles long runs well", sports="running", confidence="moderate"
        )
        learnings = test_db.get_learnings()
        self.assertEqual(len(learnings), 1)
        learning = learnings[0]
        self.assertEqual(learning["id"], lid)
        self.assertEqual(learning["text"], "Handles long runs well")
        self.assertEqual(learning["sports"], "running")
        self.assertEqual(learning["confidence"], "moderate")
        self.assertFalse(learning["dormant"])
        # A fresh record's last reinforcement is its creation.
        self.assertEqual(learning["last_reinforced_at"], learning["created_at"])

        # Omitted scope/confidence fall back to general/tentative.
        test_db.add_learning("Generic note")
        generic = test_db.get_learnings()[-1]
        self.assertEqual(generic["sports"], "general")
        self.assertEqual(generic["confidence"], "tentative")

    # Five distinct calendar weeks (each normalizes to its own Monday).
    WEEKS = ["2026-05-04", "2026-05-12", "2026-05-20", "2026-05-28", "2026-06-05"]

    def test_derive_confidence_pure_function(self):
        # Defaults: moderate at 3 net weeks, established at 5.
        self.assertEqual(derive_confidence(0, 0), "tentative")   # no basis -> floor
        self.assertEqual(derive_confidence(2, 0), "tentative")
        self.assertEqual(derive_confidence(3, 0), "moderate")
        self.assertEqual(derive_confidence(5, 0), "established")
        self.assertEqual(derive_confidence(6, 1), "established")  # net 5
        # Contradiction nets down; only contradiction (not an empty basis) proposes retire.
        self.assertEqual(derive_confidence(2, 2), RETIRE_PROPOSAL)  # net 0, contradicted
        self.assertEqual(derive_confidence(5, 3), "tentative")      # net 2
        self.assertEqual(confidence_rank("tentative"), 1)
        self.assertEqual(step_down("established"), "moderate")
        self.assertEqual(step_down("tentative"), RETIRE_PROPOSAL)

    def test_add_derives_confidence_from_distinct_weeks(self):
        # Citing 3 distinct weeks -> moderate; the LLM sets no confidence.
        test_db.apply_learning_deltas([
            {"op": "add", "text": "Recovers fast",
             "sports": "Running, Cycling", "evidence": self.WEEKS[:3]},
        ])
        learning = test_db.get_learnings()[0]
        lid = learning["id"]
        self.assertEqual(learning["sports"], "running,cycling")
        self.assertEqual(learning["confidence"], "moderate")
        self.assertIsNone(learning["proposed_confidence"])

        # Reinforcing with two more distinct weeks reaches established (net 5).
        test_db.apply_learning_deltas([
            {"op": "reinforce", "id": lid, "evidence": self.WEEKS[3:5]},
        ])
        self.assertEqual(test_db.get_learnings()[0]["confidence"], "established")

        # Retire deletes the record (basis cascades).
        test_db.apply_learning_deltas([{"op": "retire", "id": lid}])
        self.assertEqual(len(test_db.get_learnings()), 0)

    def test_the_merge_counts_what_it_could_not_act_on(self):
        """Skipping a malformed delta is right; hiding the skip is not — a caller has to be
        able to tell "authored nothing" from "understood nothing"
        (DESIGN_backward_evaluation.md §13)."""
        lid = test_db.add_learning("Tolerates volume")
        tally = test_db.apply_learning_deltas([
            {"op": "add", "text": "Recovers fast"},          # applied
            {"op": "reinforce", "id": lid},                   # applied (no new week is fine)
            {">op": "add", ">text": "Key came back mangled"},  # skipped: no recognized op
            {"op": "revise", "id": 9999, "text": "x"},        # skipped: hallucinated id
            {"op": "add", "text": "   "},                     # skipped: no text
            {"op": "retire"},                                 # skipped: no id
            "not even an object",                             # skipped: not a dict
        ])
        self.assertEqual(tally, {"applied": 2, "skipped": 5})
        self.assertEqual(
            {l["text"] for l in test_db.get_learnings()},
            {"Tolerates volume", "Recovers fast"},
        )

    def test_the_merge_tally_is_empty_when_there_is_nothing_to_do(self):
        self.assertEqual(test_db.apply_learning_deltas([]), {"applied": 0, "skipped": 0})

    def test_evidence_dedup_blocks_inflation(self):
        """Re-citing counted weeks is a structural no-op: confidence cannot ratchet and
        recency is not refreshed (DESIGN_evidence_based_confidence.md §6)."""
        test_db.apply_learning_deltas([
            {"op": "add", "text": "Tolerates volume", "evidence": self.WEEKS[:3]},
        ])
        lid = test_db.get_learnings()[0]["id"]
        before = test_db.get_learnings()[0]["last_reinforced_at"]

        # Re-citing the SAME three weeks: no new basis, no recency refresh, no upgrade.
        test_db.apply_learning_deltas([
            {"op": "reinforce", "id": lid, "evidence": self.WEEKS[:3]},
        ])
        again = test_db.get_learnings()[0]
        self.assertEqual(again["confidence"], "moderate")
        self.assertEqual(again["last_reinforced_at"], before)
        # Basis still holds exactly three supporting weeks.
        self.assertEqual(len(test_db.get_learning_evidence(lid)), 3)

    def test_deltas_with_hallucinated_id_are_skipped(self):
        """An op referencing a non-existent learning is skipped, not allowed to abort the
        whole transaction on the evidence FK. A valid add in the same batch still lands."""
        test_db.apply_learning_deltas([
            {"op": "reinforce", "id": 999, "evidence": ["2026-06-01"]},
            {"op": "contradict", "id": 999, "evidence": ["2026-06-08"]},
            {"op": "revise", "id": 999, "text": "ghost"},
            {"op": "add", "text": "Real one", "evidence": ["2026-06-01"]},
        ])
        learnings = test_db.get_learnings()
        self.assertEqual(len(learnings), 1)
        self.assertEqual(learnings[0]["text"], "Real one")

    def test_week_validation_drops_out_of_window(self):
        # Only WEEKS[0] is in the analysed window; the other cited week is dropped.
        test_db.apply_learning_deltas(
            [{"op": "add", "text": "Windowed", "evidence": [self.WEEKS[0], self.WEEKS[4]]}],
            available_weeks=[self.WEEKS[0]],
        )
        lid = test_db.get_learnings()[0]["id"]
        basis = test_db.get_learning_evidence(lid)
        self.assertEqual(len(basis), 1)
        self.assertEqual(basis[0]["week_commencing"], "2026-05-04")  # Monday of WEEKS[0]

    def test_contradiction_proposes_demotion_then_demote_keep(self):
        # Reach established (5 supporting weeks).
        test_db.apply_learning_deltas([
            {"op": "add", "text": "Back-to-back hard days fine", "sports": "cycling",
             "evidence": self.WEEKS},
        ])
        lid = test_db.get_learnings()[0]["id"]
        self.assertEqual(test_db.get_learnings()[0]["confidence"], "established")

        # Contradict in 3 distinct weeks -> net 2 -> derived tentative < established.
        test_db.apply_learning_deltas([
            {"op": "contradict", "id": lid,
             "evidence": ["2026-06-15", "2026-06-22", "2026-06-29"]},
        ])
        learning = test_db.get_learnings()[0]
        # Live confidence is NOT lowered; a downgrade is only proposed.
        self.assertEqual(learning["confidence"], "established")
        self.assertEqual(learning["proposed_confidence"], "tentative")

        # keep() dismisses + neutralizes the -1 rows, restoring derived == stored.
        test_db.keep_learning(lid)
        learning = test_db.get_learnings()[0]
        self.assertIsNone(learning["proposed_confidence"])
        self.assertEqual(learning["confidence"], "established")
        self.assertFalse(any(
            e["polarity"] < 0 for e in test_db.get_learning_evidence(lid)
        ))

    def test_demote_applies_proposed_level(self):
        test_db.apply_learning_deltas([
            {"op": "add", "text": "X", "evidence": self.WEEKS},
        ])
        lid = test_db.get_learnings()[0]["id"]
        test_db.apply_learning_deltas([
            {"op": "contradict", "id": lid,
             "evidence": ["2026-06-15", "2026-06-22", "2026-06-29"]},
        ])
        self.assertEqual(test_db.get_learnings()[0]["proposed_confidence"], "tentative")
        result = test_db.demote_learning(lid)
        self.assertEqual(result, "tentative")
        learning = test_db.get_learnings()[0]
        self.assertEqual(learning["confidence"], "tentative")
        self.assertIsNone(learning["proposed_confidence"])

    def test_reinforce_with_new_week_revives_dormant(self):
        lid = test_db.add_learning("Tentative observation")  # 21-day budget
        old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        with test_db._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?", (old, lid)
            )
        self.assertTrue(test_db.get_learnings()[0]["dormant"])

        # A genuinely new supporting week refreshes recency and revives it.
        test_db.apply_learning_deltas([
            {"op": "reinforce", "id": lid, "evidence": ["2026-06-01"]},
        ])
        self.assertFalse(test_db.get_learnings()[0]["dormant"])

    def test_staleness_proposes_and_auto_applies(self):
        lid = test_db.add_learning("Aging note", confidence="moderate")  # 60-day budget
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        with test_db._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?", (old, lid)
            )
        # Interactive sweep: proposes one level down, leaves live level intact.
        test_db.derive_staleness_proposals(auto=False)
        learning = test_db.get_learnings()[0]
        self.assertEqual(learning["confidence"], "moderate")
        self.assertEqual(learning["proposed_confidence"], "tentative")

        # Auto sweep would have applied it directly; verify on a fresh aged learning.
        lid2 = test_db.add_learning("Another aging note", confidence="moderate")
        with test_db._get_connection() as conn:
            conn.execute(
                "UPDATE coach_learnings SET last_reinforced_at=? WHERE id=?", (old, lid2)
            )
        test_db.derive_staleness_proposals(auto=True)
        l2 = next(l for l in test_db.get_learnings() if l["id"] == lid2)
        self.assertEqual(l2["confidence"], "tentative")
        self.assertIsNone(l2["proposed_confidence"])

    def test_grandfather_seeds_basis_to_sustain_level(self):
        """A learning predating the evidence model keeps its level after recompute, because
        the migration seeds a synthetic supporting basis sized to sustain it (§9)."""
        # Simulate a pre-evidence row: insert directly with no basis, then run the migration.
        now = datetime.now(timezone.utc).isoformat()
        with test_db._get_connection() as conn:
            conn.execute(
                "INSERT INTO coach_learnings (text, sports, confidence, created_at, "
                "updated_at, last_reinforced_at) VALUES "
                "('Legacy established', 'general', 'established', ?, ?, ?)",
                (now, now, now)
            )
            lid = conn.execute(
                "SELECT id FROM coach_learnings WHERE text='Legacy established'"
            ).fetchone()[0]
        test_db._grandfather_learning_evidence()
        # 5 distinct synthetic supporting weeks -> recompute keeps 'established'.
        basis = test_db.get_learning_evidence(lid)
        self.assertEqual(len({e["week_commencing"] for e in basis}), 5)
        test_db.recompute_all_confidence()
        learning = next(l for l in test_db.get_learnings() if l["id"] == lid)
        self.assertEqual(learning["confidence"], "established")
        self.assertIsNone(learning["proposed_confidence"])

    def test_analysis_cache_upsert_and_retention(self):
        """One row per horizon; saving again overwrites the slot. `reconstruction`
        round-trips through JSON (DESIGN_backward_evaluation.md §5.1)."""
        self.assertIsNone(test_db.get_analysis_cache("long"))

        recon = {"inferred_macrocycle": {"overall_focus": "Base"},
                 "physiological_insights": ["HRV stable"]}
        test_db.save_analysis_cache(
            "long", "fp-1", "2026-01-01", "2026-03-31", recon
        )
        row = test_db.get_analysis_cache("long")
        self.assertEqual(row["fingerprint"], "fp-1")
        self.assertEqual(row["window_start"], "2026-01-01")
        self.assertEqual(row["reconstruction"], recon)

        # Re-saving the same horizon overwrites (one-row-per-horizon retention).
        test_db.save_analysis_cache("long", "fp-2", "2026-01-01", "2026-04-30", {"x": 1})
        row = test_db.get_analysis_cache("long")
        self.assertEqual(row["fingerprint"], "fp-2")
        self.assertEqual(row["reconstruction"], {"x": 1})

        # Horizons are independent slots.
        test_db.save_analysis_cache("short", "fp-s", "2026-04-01", "2026-04-30", {"y": 2})
        self.assertEqual(test_db.get_analysis_cache("short")["fingerprint"], "fp-s")
        self.assertEqual(test_db.get_analysis_cache("long")["fingerprint"], "fp-2")

        # wipe_metrics also clears the reconstruction cache (evidence-derived).
        test_db.wipe_metrics()
        self.assertIsNone(test_db.get_analysis_cache("long"))
        self.assertIsNone(test_db.get_analysis_cache("short"))

    def test_wipe_analysis_cache_is_the_wipe_path(self):
        """`wipe_analysis_cache()` clears every horizon slot, and `wipe_garmin_data()`
        goes through it rather than inlining its own DELETE — including for a windowed
        wipe, since a reconstruction covers a whole window, not per-day rows
        (DESIGN_backward_evaluation.md §5.1)."""
        test_db.save_analysis_cache("long", "fp-l", "2026-01-01", "2026-03-31", {"a": 1})
        test_db.save_analysis_cache("short", "fp-s", "2026-03-01", "2026-03-31", {"b": 2})
        test_db.wipe_analysis_cache()
        self.assertIsNone(test_db.get_analysis_cache("long"))
        self.assertIsNone(test_db.get_analysis_cache("short"))

        with mock.patch.object(
            test_db, "wipe_analysis_cache", wraps=test_db.wipe_analysis_cache
        ) as wiper:
            test_db.wipe_garmin_data("2026-03-01", "2026-03-31")
        wiper.assert_called_once_with()

    def test_daily_context_upsert_get_delete(self):
        """Context signals upsert by event id (edits replace in place), query by date
        window, and delete on cancellation (DESIGN_calendar_context_ingest.md §5)."""
        test_db.upsert_daily_context_by_event(
            google_event_id="evt-a", date="2026-06-13", metric="alcohol",
            value=2.0, text="2 drinks", updated="2026-06-13T20:00:00Z",
        )
        test_db.upsert_daily_context_by_event(
            google_event_id="evt-b", date="2026-06-14", metric="stress",
            value=None, text="rough day", updated=None,
        )
        rows = test_db.get_daily_context(start_date="2026-06-13", end_date="2026-06-14")
        self.assertEqual([(r["metric"], r["value"]) for r in rows],
                         [("alcohol", 2.0), ("stress", None)])

        # Re-ingesting the same event id corrects in place (no duplicate row).
        test_db.upsert_daily_context_by_event(
            google_event_id="evt-a", date="2026-06-13", metric="alcohol",
            value=3.0, text="3 drinks", updated="2026-06-13T21:00:00Z",
        )
        rows = test_db.get_daily_context()
        self.assertEqual(len(rows), 2)
        alcohol = next(r for r in rows if r["google_event_id"] == "evt-a")
        self.assertEqual((alcohol["value"], alcohol["text"]), (3.0, "3 drinks"))

        # Window excludes out-of-range dates.
        self.assertEqual(test_db.get_daily_context(start_date="2026-06-14"),
                         [r for r in rows if r["date"] == "2026-06-14"])

        # Cancellation deletes the row.
        test_db.delete_daily_context_by_event("evt-a")
        remaining = test_db.get_daily_context()
        self.assertEqual([r["google_event_id"] for r in remaining], ["evt-b"])

        # wipe_metrics clears context alongside the evidence it contextualizes.
        test_db.wipe_metrics()
        self.assertEqual(test_db.get_daily_context(), [])

    def _seed_garmin_and_context(self):
        """Seeds two dated metric/activity/context rows plus both sync watermarks."""
        for d in ("2026-01-10", "2026-02-10"):
            test_db.save_metric_cache(d, rhr=50, hrv=60, sleep_score=80, stress=30)
            test_db.save_completed_activity(
                f"act-{d}", d, None, "Run", "running", 3600, 10, 50, 140, 160, None, 50
            )
        test_db.upsert_daily_context_by_event(
            google_event_id="g-jan", date="2026-01-10", metric="alcohol",
            value=2.0, text="2 drinks",
        )
        test_db.upsert_daily_context_by_event(
            google_event_id="g-feb", date="2026-02-10", metric="alcohol",
            value=3.0, text="3 drinks",
        )
        test_db.set_sync_state(through_date="2026-02-10", last_pull_utc="t", key="garmin")
        test_db.set_sync_state(
            through_date=None, last_pull_utc="t", key="calendar_context", sync_token="tok"
        )

    def test_wipe_garmin_data_scope_and_watermark(self):
        """--garmin wipes only Garmin evidence; a full wipe also resets the garmin/coach
        watermarks but never the calendar token, and a dated wipe leaves watermarks be."""
        self._seed_garmin_and_context()

        # Dated wipe removes only in-range Garmin rows; context + watermarks untouched.
        test_db.wipe_garmin_data("2026-02-01", "2026-02-28")
        self.assertEqual(
            [m["date"] for m in test_db.get_metrics_cache()], ["2026-01-10"]
        )
        self.assertEqual(
            [a["date"] for a in test_db.get_completed_activities()], ["2026-01-10"]
        )
        self.assertEqual(len(test_db.get_daily_context()), 2)  # calendar untouched
        self.assertIsNotNone(test_db.get_sync_state("garmin"))  # watermark kept

        # Full Garmin wipe clears the rest + the garmin watermark, sparing the calendar.
        test_db.wipe_garmin_data()
        self.assertEqual(test_db.get_metrics_cache(), [])
        self.assertIsNone(test_db.get_sync_state("garmin"))
        self.assertIsNotNone(test_db.get_sync_state("calendar_context"))
        self.assertEqual(len(test_db.get_daily_context()), 2)

    def test_wipe_calendar_context_scope_and_token(self):
        """--calendar wipes daily context and always resets the Calendar token (full or
        dated), but leaves Garmin evidence and its watermark alone."""
        self._seed_garmin_and_context()

        # Dated wipe drops only the in-range context row, yet still resets the token so
        # the next pull re-syncs in full.
        test_db.wipe_calendar_context("2026-01-01", "2026-01-31")
        self.assertEqual(
            [c["date"] for c in test_db.get_daily_context()], ["2026-02-10"]
        )
        self.assertIsNone(test_db.get_sync_state("calendar_context"))
        self.assertEqual(len(test_db.get_metrics_cache()), 2)  # Garmin untouched
        self.assertIsNotNone(test_db.get_sync_state("garmin"))

        # Full calendar wipe clears the remaining context row.
        test_db.wipe_calendar_context()
        self.assertEqual(test_db.get_daily_context(), [])

    def test_sync_state_token_is_per_key(self):
        """The calendar_context row carries a sync_token; the garmin row keeps its
        date watermark. Distinct keys never clobber each other's columns."""
        test_db.set_sync_state(through_date="2026-06-14", last_pull_utc="t1", key="garmin")
        test_db.set_sync_state(
            through_date=None, last_pull_utc="t2", key="calendar_context",
            sync_token="tok-123",
        )
        garmin = test_db.get_sync_state(key="garmin")
        ctx = test_db.get_sync_state(key="calendar_context")
        self.assertEqual(garmin["through_date"], "2026-06-14")
        self.assertIsNone(garmin["sync_token"])
        self.assertEqual(ctx["sync_token"], "tok-123")
        self.assertIsNone(ctx["through_date"])

        # Updating the token preserves the per-key isolation.
        test_db.set_sync_state(
            through_date=None, last_pull_utc="t3", key="calendar_context",
            sync_token="tok-456",
        )
        self.assertEqual(test_db.get_sync_state(key="calendar_context")["sync_token"],
                         "tok-456")
        self.assertEqual(test_db.get_sync_state(key="garmin")["through_date"], "2026-06-14")

    def test_learning_helpers(self):
        self.assertEqual(normalize_sports(None), "general")
        self.assertEqual(normalize_sports(""), "general")
        self.assertEqual(normalize_sports("Running, Cycling"), "running,cycling")
        self.assertEqual(normalize_sports(["Running", " Hiking "]), "running,hiking")

        self.assertEqual(valid_confidence("established"), "established")
        self.assertIsNone(valid_confidence("bogus"))

        base = datetime.now(timezone.utc)
        # Higher confidence survives longer: established budget is 180 days.
        fresh = {"confidence": "established",
                 "last_reinforced_at": (base - timedelta(days=100)).isoformat()}
        stale = {"confidence": "established",
                 "last_reinforced_at": (base - timedelta(days=200)).isoformat()}
        self.assertFalse(learning_is_dormant(fresh, base))
        self.assertTrue(learning_is_dormant(stale, base))


class TestPlannedZoneColumns(unittest.TestCase):
    """DESIGN_intensity_distribution.md §9.8 — the intensity target on `workouts`."""

    @classmethod
    def setUpClass(cls):
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

    def _save(self, **kwargs):
        return test_db.save_workout(
            date="2026-06-10", sport_type="running", title="Tempo Run",
            description="[Tempo Run]\nSteady.", **kwargs
        )

    def test_round_trips_an_hr_prescription_with_6_and_7_null(self):
        self._save(
            duration_minutes=60, tss=45.0,
            planned_zone_currency="hr",
            planned_zone_sec=[300, 1800, 600, 0, 0, None, None],
        )
        row = test_db.get_workout("2026-06-10", "running")
        self.assertEqual(row["planned_zone_currency"], "hr")
        self.assertEqual(row["planned_zone2_sec"], 1800)
        self.assertIsNone(row["planned_zone6_sec"])
        self.assertIsNone(row["planned_zone7_sec"])

    def test_a_later_save_that_omits_them_preserves_the_target(self):
        self._save(planned_zone_currency="hr", planned_zone_sec=[300, 1800, 0, 0, 0])
        self._save(duration_minutes=45)
        row = test_db.get_workout("2026-06-10", "running")
        self.assertEqual(row["planned_zone_currency"], "hr")
        self.assertEqual(row["planned_zone2_sec"], 1800)

    def test_an_adapt_that_emits_them_overwrites_the_target(self):
        """A drift correction rewrites HOW a session is prescribed, so leaving its zones
        alone would leave them describing the prescription just replaced."""
        self._save(planned_zone_currency="hr", planned_zone_sec=[300, 1800, 600, 0, 0])
        self._save(planned_zone_currency="hr", planned_zone_sec=[300, 2700, 0, 0, 0])
        row = test_db.get_workout("2026-06-10", "running")
        self.assertEqual(row["planned_zone2_sec"], 2700)
        # An emitted 0 is a statement, not an omission: the correction traded the tempo
        # minutes for aerobic ones and says so. Only NULL (a zone the currency does not
        # have) is preserved.
        self.assertEqual(row["planned_zone3_sec"], 0)

    def test_a_session_saved_without_a_target_has_null_columns(self):
        self._save(duration_minutes=60)
        row = test_db.get_workout("2026-06-10", "running")
        self.assertIsNone(row["planned_zone_currency"])
        self.assertIsNone(row["planned_zone1_sec"])

    def test_the_target_is_outside_the_calendar_freshness_hash(self):
        """`CALENDAR_FIELDS` is an allowlist, so new columns are excluded by default —
        and must stay that way, or every regeneration that nudges a target by two
        minutes marks the row stale and re-pushes the event (§9.8)."""
        from trainmate.calendar_state import calendar_signature
        self._save(duration_minutes=60, planned_zone_sec=[300, 1800, 0, 0, 0],
                   planned_zone_currency="hr")
        before = calendar_signature(test_db.get_workout("2026-06-10", "running"))
        self._save(duration_minutes=60, planned_zone_sec=[420, 1680, 0, 0, 0],
                   planned_zone_currency="hr")
        after = test_db.get_workout("2026-06-10", "running")
        self.assertEqual(after["planned_zone1_sec"], 420)
        self.assertEqual(calendar_signature(after), before)


class TestGoalStateIsDerived(unittest.TestCase):
    """A goal is completed because its date has passed, not because a column says so
    (DESIGN_backward_evaluation.md §12). The column records only "called off"."""

    TODAY = "2026-08-05"

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
        pin_clock(self, self.TODAY)

    def _goal(self, title: str, target: str, status: str = "active") -> int:
        return test_db.add_objective(
            title=title, target_date=target, sport_type="cycling", priority=1,
            status=status,
        )

    # ------------------------------------------------------------------ derivation

    def test_the_three_states(self):
        past = test_db.get_objective(self._goal("Last month's race", "2026-07-04"))
        ahead = test_db.get_objective(self._goal("Autumn climb", "2026-09-30"))
        called_off = test_db.get_objective(
            self._goal("Cancelled sportive", "2026-09-01", status="archived")
        )

        self.assertEqual(goal_state(past), "completed")
        self.assertEqual(goal_state(ahead), "upcoming")
        # Archived wins over the date in both directions.
        self.assertEqual(goal_state(called_off), "archived")

    def test_the_target_day_itself_still_counts_as_upcoming(self):
        """Race day is not history until it is over."""
        today = test_db.get_objective(self._goal("Race day", self.TODAY))
        self.assertEqual(goal_state(today), "upcoming")

    # ------------------------------------------------------------------ accessors

    def test_upcoming_objectives_drops_past_and_archived(self):
        self._goal("Last month's race", "2026-07-04")
        ahead = self._goal("Autumn climb", "2026-09-30")
        self._goal("Cancelled sportive", "2026-09-01", status="archived")

        self.assertEqual([o["id"] for o in test_db.upcoming_objectives()], [ahead])

    def test_preceding_objectives_reach_completed_goals(self):
        """The whole point of the lookup is the season behind the athlete — filtering it
        to `status = 'active'` hid exactly what it was for (§12)."""
        done = self._goal("Last month's race", "2026-07-04")
        self._goal("Cancelled sportive", "2026-07-05", status="archived")

        found = test_db.get_preceding_objectives("2026-09-30")

        self.assertEqual([o["id"] for o in found], [done])

    def test_preceding_objectives_still_bounded_by_the_lookback(self):
        """`coach.goals_lookback_days` (90) is a separate bound and is left alone."""
        self._goal("Last season", "2025-09-01")
        self.assertEqual(test_db.get_preceding_objectives("2026-09-30"), [])

    # ------------------------------------------------- what governs the timeline

    def _planned_goal(self, title: str, target: str) -> int:
        obj_id = self._goal(title, target)
        test_db.save_macrocycle(
            objective_id=obj_id, strategy=title, goals_hash="g", constraints_hash="c",
            mesocycles=[{"name": f"{title} block", "start_date": "2026-06-01",
                         "end_date": "2026-06-28", "focus": "base"}],
        )
        return obj_id

    def test_governing_plan_is_the_earliest_goal_still_ahead(self):
        self._planned_goal("Last month's race", "2026-07-04")
        ahead = self._planned_goal("Autumn climb", "2026-09-30")

        self.assertEqual(
            test_db.get_governing_macrocycle()["objective_id"], ahead
        )

    def test_governing_plan_falls_back_to_the_last_completed_goal(self):
        """The day after an event the workouts behind the athlete still belong to that
        plan; blanking the timeline's block labels right then would be worse than keeping
        them (§12)."""
        self._planned_goal("Spring race", "2026-06-01")
        latest = self._planned_goal("Last month's race", "2026-07-04")

        self.assertEqual(
            test_db.get_governing_macrocycle()["objective_id"], latest
        )

    def test_a_completed_goal_is_no_longer_stored(self):
        """One-off migration: the state left the column when it became derivable."""
        with test_db._get_connection() as conn:
            conn.execute(
                "INSERT INTO objectives (title, target_date, sport_type, priority, status)"
                " VALUES ('Legacy', '2026-07-04', 'cycling', 1, 'completed')"
            )
            conn.commit()

        # A database still holding this state predates the stamp, so clear it: an
        # already-migrated database legitimately skips the migration.
        unstamp_schema(test_db)
        Database(db_path=TEST_DB_PATH)          # re-init runs the migration

        row = test_db.get_objectives()[0]
        self.assertEqual(row["status"], "active")
        self.assertEqual(goal_state(row), "completed")     # unchanged where it counts


if __name__ == "__main__":
    unittest.main()
