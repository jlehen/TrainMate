"""The revision history a Calendar event carries (DESIGN_calendar_lineage.md).

Written end to end — a real lineage in the log, through `sync_workout`, out as the event
body Google would receive — because the whole point of the feature is that the athlete
reads the log on a phone, and the parts that could silently disagree (which revisions are
in, what a void is allowed to print, when the event goes stale) all sit on that path.
"""
import os
import unittest
from unittest.mock import MagicMock, patch

from tests.helpers import rebind_test_db, save_workout
from trainmate import calendar_lineage
from trainmate.calendar_state import calendar_signature, calendar_status
from trainmate.db import Database
from trainmate.google_calendar import calendar_syncer
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_trainmate_cal_lineage.db")

ZONES = [None, 40 * 60, 90 * 60, None, None, None, None]


def entries(description):
    """The history block split into its entries, keyed by their `[n/total]` marker."""
    if "History · " not in description:
        return {}
    block = description.split("History · ", 1)[1]
    block = block.split("\n\n", 1)[1] if "\n\n" in block else ""
    found, current = {}, None
    for line in block.split("\n"):
        if line.startswith("["):
            current = line.split("]")[0] + "]"
            found[current] = line + "\n"
            continue
        if current:
            found[current] += line + "\n"
    return found


class TestCalendarLineage(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        cls.db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(cls.db)

    @classmethod
    def tearDownClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)

    def setUp(self):
        from tests.helpers import clear_all_tables
        clear_all_tables(self.db)

    # ------------------------------------------------------------------ fixtures

    def _plan(self, **overrides):
        """One generated session, the head of everything below."""
        fields = dict(
            date="2026-08-31", sport_type="cycling", title="Long ride",
            description="3h steady endurance.", duration_minutes=180, tss=210, rpe=8,
            planned_zone_currency="power", planned_zone_sec=ZONES,
        )
        fields.update(overrides)
        with self.db.workout_change(kind="generate") as change:
            change.append(**fields)
        return self.db.get_workout(fields["date"], fields["sport_type"])["id"]

    def _description(self, lineage_id):
        """The event body `sync_workout` would send for that session."""
        workout = self.db.get_workout_by_id(lineage_id)
        service = MagicMock()
        service.events().insert().execute.return_value = {"id": "evt-1"}
        service.events().update().execute.return_value = {"id": "evt-1"}
        with patch.object(calendar_syncer, "service", service):
            calendar_syncer.sync_workout(workout)
        # A session with a stored event id is updated, not inserted.
        calls = (
            service.events().insert.call_args_list
            + service.events().update.call_args_list
        )
        body = [call for call in calls if call.kwargs.get("body")][0].kwargs["body"]
        return body.get("description", "")

    # ------------------------------------------------------------------ tests

    def test_an_adapted_session_carries_its_earlier_form_in_full(self):
        lineage = self._plan()
        with self.db.workout_change(kind="adapt", summary="HRV suppressed") as change:
            change.append(
                date="2026-08-31", sport_type="cycling", title="Long ride",
                description="90min easy, keep it conversational.",
                duration_minutes=90, tss=95, rpe=4, lineage_id=lineage,
                reason="Eased: sleep debt across the week",
                planned_zone_currency="power",
                planned_zone_sec=[30 * 60, 60 * 60, None, None, None, None, None],
            )

        desc = self._description(lineage)

        # The current form still owns the top of the event.
        self.assertTrue(desc.startswith("Duration: 90m | TSS: 95 | RPE: 4"))
        self.assertIn("90min easy, keep it conversational.", desc)
        self.assertIn("Reason:\nEased: sleep debt across the week", desc)

        self.assertIn("History · 1 earlier revision, newest first", desc)
        planned = entries(desc)["[1/2]"]
        self.assertIn("Planned", planned)
        self.assertIn("2026-08-31 Mon · Long ride", planned)
        self.assertIn("Duration: 180m | TSS: 210 | RPE: 8", planned)
        self.assertIn("Target: ", planned)
        self.assertIn("3h steady endurance.", planned)

    def test_entries_run_newest_first(self):
        lineage = self._plan()
        for minutes, note in ((120, "First easing"), (90, "Second easing")):
            with self.db.workout_change(kind="adapt") as change:
                change.append(
                    date="2026-08-31", sport_type="cycling", title="Long ride",
                    description=f"{minutes}min ride.", duration_minutes=minutes,
                    tss=minutes, rpe=5, lineage_id=lineage, reason=note,
                )

        desc = self._description(lineage)
        self.assertIn("History · 2 earlier revisions, newest first", desc)
        self.assertLess(desc.index("[2/3]"), desc.index("[1/3]"))
        self.assertIn("Second easing", desc.split("History · ", 1)[0] + "")
        self.assertIn("First easing", entries(desc)["[2/3]"])

    def test_footer_sits_above_the_history(self):
        # The lifecycle + id lines describe the session as it stands today, so they read
        # directly under the current prescription rather than past the earlier forms
        # (DESIGN_calendar_lineage.md §5).
        lineage = self._plan()
        with self.db.workout_change(kind="adapt") as change:
            change.append(
                date="2026-08-31", sport_type="cycling", title="Long ride",
                description="90min easy.", duration_minutes=90, tss=95, rpe=4,
                lineage_id=lineage, reason="Eased",
            )

        desc = self._description(lineage)
        self.assertLess(desc.index("90min easy."), desc.index("Planned: "))
        self.assertLess(desc.index("Planned: "), desc.index(f"Workout: {lineage}"))
        self.assertLess(desc.index(f"Workout: {lineage}"), desc.index("History · "))

    def test_a_session_that_never_changed_has_no_history(self):
        desc = self._description(self._plan())
        self.assertNotIn("History", desc)
        self.assertNotIn(calendar_lineage.SEPARATOR, desc)

    def test_a_moved_session_names_the_date_it_left(self):
        lineage = self._plan()
        # The shape `workout swap` writes: void the source first, then the copy that
        # carries the lineage to the destination (DESIGN_workout_revisions.md §4).
        with self.db.workout_change(kind="swap") as change:
            change.void(
                date="2026-08-31", sport_type="cycling",
                reason="Swapped from 2026-08-31 to 2026-09-02",
            )
            change.append(
                date="2026-09-02", sport_type="cycling", title="Long ride",
                description="3h steady endurance.", duration_minutes=180, tss=210, rpe=8,
                lineage_id=lineage, reason="Swapped from 2026-08-31 to 2026-09-02",
            )

        desc = self._description(lineage)
        vacated = entries(desc)["[2/3]"]
        self.assertIn("Moved away", vacated)
        self.assertIn("2026-08-31 Mon", vacated)
        self.assertIn("Swapped from 2026-08-31 to 2026-09-02", vacated)
        # And the form it had before the move is still there, at its old date.
        self.assertIn("2026-08-31 Mon", entries(desc)["[1/3]"])

    def test_a_void_entry_states_no_prescription(self):
        """A void copies the departing session's columns forward, so it still carries a
        duration and a target. Printing them would prescribe a day that holds nothing."""
        lineage = self._plan()
        with self.db.workout_change(kind="rm") as change:
            change.void(
                date="2026-08-31", sport_type="cycling", reason="Work trip",
            )
        revision = self.db.revision_before_live_void(lineage)
        with self.db.workout_change(kind="restore") as change:
            change.restore(revision)

        cancelled = entries(self._description(lineage))["[2/3]"]
        self.assertIn("Cancelled", cancelled)
        self.assertIn("Reason: Work trip", cancelled)
        self.assertNotIn("Duration:", cancelled)
        self.assertNotIn("Target:", cancelled)
        self.assertNotIn("3h steady endurance.", cancelled)

    def test_the_change_summary_shows_only_when_it_adds_something(self):
        lineage = self._plan()
        with self.db.workout_change(kind="adapt", summary="Recovery is lagging") as change:
            change.append(
                date="2026-08-31", sport_type="cycling", title="Long ride",
                description="90min easy.", duration_minutes=90, tss=95, rpe=4,
                lineage_id=lineage, reason="Eased: 180m -> 90m",
            )
        with self.db.workout_change(kind="adapt", summary="Same note") as change:
            change.append(
                date="2026-08-31", sport_type="cycling", title="Long ride",
                description="60min easy.", duration_minutes=60, tss=60, rpe=3,
                lineage_id=lineage, reason="Same note",
            )
        # One more, so neither of the two under test is the revision being rendered.
        with self.db.workout_change(kind="adapt", summary="Holding the easy week") as ch:
            ch.append(
                date="2026-08-31", sport_type="cycling", title="Long ride",
                description="45min easy.", duration_minutes=45, tss=40, rpe=3,
                lineage_id=lineage, reason="Eased again",
            )

        found = entries(self._description(lineage))
        self.assertIn("Change: Recovery is lagging", found["[2/4]"])
        self.assertNotIn("Change:", found["[3/4]"])

    def test_a_long_lineage_is_truncated_and_says_how_much(self):
        lineage = self._plan()
        body = "Steady endurance. " * 40
        for minutes in range(179, 149, -1):
            with self.db.workout_change(kind="adapt") as change:
                change.append(
                    date="2026-08-31", sport_type="cycling", title="Long ride",
                    description=f"{minutes}min. {body}", duration_minutes=minutes,
                    tss=minutes, rpe=5, lineage_id=lineage, reason=f"Eased to {minutes}m",
                )

        desc = self._description(lineage)
        history = calendar_lineage.SEPARATOR + desc.split(calendar_lineage.SEPARATOR, 1)[1]
        self.assertLess(len(history), calendar_lineage.HISTORY_BUDGET + 1000)
        self.assertIn("earlier revisions not shown.", desc)
        # What survives is the newest end of the lineage, and the oldest is what went.
        self.assertIn("[30/31]", desc)
        self.assertNotIn("[1/31]", desc)

    def test_a_long_prescription_squeezes_the_history_not_itself(self):
        """The history is rendered into the room the rest of the event left, so the
        session the athlete is about to go and do is never what gets cut (§7)."""
        prescription = "Ride steady. " * 380          # ~4900 characters
        lineage = self._plan()
        for minutes in (150, 120, 90):
            with self.db.workout_change(kind="adapt") as change:
                change.append(
                    date="2026-08-31", sport_type="cycling", title="Long ride",
                    description=f"{minutes}min. {prescription}",
                    duration_minutes=minutes, tss=minutes, rpe=5,
                    lineage_id=lineage, reason=f"Eased to {minutes}m",
                )

        desc = self._description(lineage)
        self.assertLessEqual(len(desc), calendar_lineage.MAX_DESCRIPTION)
        # The current prescription survives whole...
        self.assertIn(f"90min. {prescription}".strip(), desc)
        # ...and the footer with it, because the history yielded first.
        self.assertIn("Workout: ", desc)
        self.assertIn("not shown.", desc)

    def test_appending_a_revision_makes_the_event_stale(self):
        """The history is a function of the whole lineage, so the freshness hash has to
        move when the lineage grows — `revision_id` is what carries that (§6). Spans
        db/workouts.py and calendar_state.py, so the test does too (AGENTS.md)."""
        lineage = self._plan()
        self.db.mark_workout_pushed(
            lineage, "evt-1", calendar_signature(self.db.get_workout_by_id(lineage))
        )
        self.assertEqual(calendar_status(self.db.get_workout_by_id(lineage)), "synced")

        # A revision that changes nothing the old field list watched: same title, same
        # description, same duration and TSS — only the intensity target moves.
        with self.db.workout_change(kind="generate") as change:
            change.append(
                date="2026-08-31", sport_type="cycling", title="Long ride",
                description="3h steady endurance.", duration_minutes=180, tss=210, rpe=8,
                lineage_id=lineage, planned_zone_currency="power",
                planned_zone_sec=[10 * 60, 60 * 60, 110 * 60, None, None, None, None],
            )

        self.assertEqual(calendar_status(self.db.get_workout_by_id(lineage)), "stale")
        self.assertIn("History · 1 earlier revision", self._description(lineage))

    def test_a_session_outside_the_log_renders_without_history(self):
        """`sync_workout` is also handed dicts that never came from a lineage — the
        Calendar must not fall over on one."""
        self.assertIsNone(calendar_lineage.for_workout({"date": "2026-08-31"}))
        self.assertIsNone(calendar_lineage.for_workout({"id": 999, "revision_id": None}))


class TestHistoryBlock(unittest.TestCase):
    """The renderer alone, over rows shaped like the log's."""

    def _revision(self, revision_id, **overrides):
        row = {
            "id": revision_id, "date": "2026-08-31", "title": "Long ride",
            "description": "3h steady endurance.", "duration_minutes": 180,
            "tss": 210, "rpe": 8, "void": 0, "reason": None, "kind": "generate",
            "change_created_at": "2026-08-01T06:00:00+00:00", "change_summary": None,
            "planned_zone_currency": None,
        }
        row.update(overrides)
        return row

    def test_a_lone_revision_has_no_history(self):
        self.assertIsNone(calendar_lineage.history_block([self._revision(1)], 1))

    def test_the_rendered_revision_is_not_repeated_in_its_own_history(self):
        rows = [self._revision(1), self._revision(2, kind="adapt")]
        block = calendar_lineage.history_block(rows, 2)
        self.assertIn("[1/2]", block)
        self.assertNotIn("[2/2]", block)

    def test_an_unknown_kind_renders_as_itself(self):
        block = calendar_lineage.history_block(
            [self._revision(1, kind="teleport"), self._revision(2)], 2
        )
        self.assertIn("teleport", block)

    def test_a_multiline_description_stays_indented(self):
        rows = [
            self._revision(1, description="Warm up.\nMain set.\nCool down."),
            self._revision(2),
        ]
        block = calendar_lineage.history_block(rows, 2)
        for line in ("Warm up.", "Main set.", "Cool down."):
            self.assertIn(calendar_lineage._INDENT + line, block)


if __name__ == "__main__":
    unittest.main()
