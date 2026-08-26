"""The athlete timezone: how a name resolves, what "today" reads, and the `timezone`
command (DESIGN_user_timezone.md)."""
import os
import unittest
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.helpers import clear_all_tables, rebind_test_db, run_cli

TEST_DB_PATH = os.path.join(os.path.dirname(__file__), "test_trainmate_clock.db")

from trainmate import clock
from trainmate.db import Database
from trainmate.util import fmt_timestamp, today_date, today_str

# 26 hours apart, so the calendar date in one is never the calendar date in the other —
# whatever instant the test runs at.
FAR_EAST = "Pacific/Kiritimati"   # UTC+14
FAR_WEST = "Etc/GMT+12"           # UTC-12

test_db = Database(db_path=TEST_DB_PATH)
rebind_test_db(test_db)


class ClockTestCase(unittest.TestCase):
    """Shared fixture: an isolated database and a clock cache that never leaks out."""

    @classmethod
    def setUpClass(cls):
        if os.path.exists(TEST_DB_PATH):
            os.remove(TEST_DB_PATH)
        global test_db
        test_db = Database(db_path=TEST_DB_PATH)
        rebind_test_db(test_db)

    @classmethod
    def tearDownClass(cls):
        # Every other test module reads today_date(); leaving a zone stored here would
        # follow them into the same process.
        clock.clear_timezone()
        if os.path.exists(TEST_DB_PATH):
            try:
                os.remove(TEST_DB_PATH)
            except OSError:
                pass

    def setUp(self):
        clear_all_tables(test_db)
        clock.reset_cache()
        self.addCleanup(clock.clear_timezone)


class TestResolve(ClockTestCase):
    """Naming a zone: what is accepted and what the refusal says (§4)."""

    def test_exact_iana_name_is_returned_unchanged(self):
        self.assertEqual(clock.resolve("Europe/Paris"), "Europe/Paris")
        self.assertEqual(clock.resolve("UTC"), "UTC")

    def test_matching_ignores_case_and_surrounding_space(self):
        self.assertEqual(clock.resolve("  europe/paris "), "Europe/Paris")
        self.assertEqual(clock.resolve("AMERICA/new_york"), "America/New_York")

    def test_a_partial_name_is_refused_but_lists_the_zones_containing_it(self):
        with self.assertRaises(ValueError) as caught:
            clock.resolve("york")
        self.assertIn("America/New_York", str(caught.exception))

    def test_a_name_matching_nothing_says_what_a_name_looks_like(self):
        with self.assertRaises(ValueError) as caught:
            clock.resolve("Mars/Olympus_Mons")
        self.assertIn("not a known timezone", str(caught.exception))

    def test_an_empty_name_is_refused(self):
        with self.assertRaises(ValueError):
            clock.resolve("   ")


class TestStoredZoneDrivesTheDate(ClockTestCase):
    """The stored zone is what every date computation reads (§1)."""

    def test_no_stored_zone_follows_the_machine(self):
        self.assertIsNone(clock.stored_name())
        self.assertIsNone(clock.active_zone())
        self.assertEqual(today_date(), datetime.now().astimezone().date())

    def test_today_is_read_in_the_stored_zone(self):
        clock.set_timezone(FAR_EAST)
        self.assertEqual(today_date(), datetime.now(ZoneInfo(FAR_EAST)).date())
        self.assertEqual(today_str(), today_date().strftime("%Y-%m-%d"))

    def test_two_zones_a_day_apart_give_two_different_todays(self):
        clock.set_timezone(FAR_EAST)
        east = today_date()
        clock.set_timezone(FAR_WEST)
        west = today_date()
        self.assertNotEqual(east, west)
        self.assertIn(east - west, (timedelta(days=1), timedelta(days=2)))

    def test_setting_a_zone_drops_the_cached_one(self):
        clock.set_timezone(FAR_EAST)
        self.assertEqual(today_date(), datetime.now(ZoneInfo(FAR_EAST)).date())
        clock.set_timezone("UTC")
        self.assertEqual(today_date(), datetime.now(timezone.utc).date())

    def test_clearing_the_zone_returns_to_the_machine(self):
        clock.set_timezone(FAR_EAST)
        self.assertTrue(clock.clear_timezone())
        self.assertIsNone(clock.active_zone())
        self.assertFalse(clock.clear_timezone())

    def test_a_zone_this_machine_does_not_know_warns_and_falls_back(self):
        # A hand-edited settings row — `timezone set` cannot write one (§3).
        test_db.set_setting(clock.TIMEZONE_SETTING, "Mars/Olympus_Mons")
        clock.reset_cache()
        self.assertIsNone(clock.active_zone())
        self.assertEqual(today_date(), datetime.now().astimezone().date())

    def test_a_refused_name_writes_nothing(self):
        clock.set_timezone("Europe/Paris")
        with self.assertRaises(ValueError):
            clock.set_timezone("Mars/Olympus_Mons")
        self.assertEqual(clock.stored_name(), "Europe/Paris")


class TestTimestampsAreShownLocal(ClockTestCase):
    """Instants stay UTC in the database and are converted only on display (§5)."""

    # Chosen in January so the conversion is CET (+1), whatever season the test runs in.
    STORED = "2026-01-15T23:30:00+00:00"

    def test_a_stored_utc_instant_renders_in_the_stored_zone(self):
        clock.set_timezone("Europe/Paris")
        self.assertEqual(fmt_timestamp(self.STORED), "2026-01-16 Fri 00:30")

    def test_the_same_instant_renders_differently_in_another_zone(self):
        clock.set_timezone("America/New_York")
        self.assertEqual(fmt_timestamp(self.STORED), "2026-01-15 Thu 18:30")

    def test_a_naive_stored_value_is_read_as_utc(self):
        clock.set_timezone("Europe/Paris")
        self.assertEqual(fmt_timestamp("2026-01-15T23:30:00"), "2026-01-16 Fri 00:30")

    def test_an_unparseable_value_is_passed_through(self):
        clock.set_timezone("Europe/Paris")
        self.assertEqual(fmt_timestamp("not a timestamp"), "not a timestamp")
        self.assertEqual(fmt_timestamp(None), "?")


class TestDescribe(ClockTestCase):
    """What the athlete is shown when they ask which zone is active (§4)."""

    def test_a_stored_zone_is_named(self):
        clock.set_timezone("Europe/Paris")
        self.assertEqual(clock.describe(), "Europe/Paris")

    def test_no_stored_zone_reports_the_machine_rather_than_inventing_a_name(self):
        self.assertIn("machine", clock.describe())

    def test_the_offset_label_is_the_zone_offset_at_that_instant(self):
        winter = datetime(2026, 1, 15, tzinfo=ZoneInfo("Europe/Paris"))
        summer = datetime(2026, 7, 15, tzinfo=ZoneInfo("Europe/Paris"))
        self.assertEqual(clock.offset_label(winter), "UTC+01:00")
        self.assertEqual(clock.offset_label(summer), "UTC+02:00")
        self.assertEqual(clock.offset_label(datetime(2026, 1, 15, tzinfo=timezone.utc)),
                         "UTC+00:00")


class TestTimezoneCommand(ClockTestCase):
    """The `timezone` command surface (§4)."""

    def test_a_bare_timezone_shows_the_active_one(self):
        clock.set_timezone("Europe/Paris")
        exit_code, stdout, _ = run_cli(["timezone"])
        self.assertEqual(exit_code, 0)
        self.assertIn("Europe/Paris", stdout)

    def test_set_stores_the_canonical_name(self):
        exit_code, stdout, _ = run_cli(["timezone", "set", "europe/paris"])
        self.assertEqual(exit_code, 0)
        self.assertEqual(test_db.get_setting("timezone"), "Europe/Paris")
        self.assertIn("Europe/Paris", stdout)

    def test_set_reports_an_unchanged_zone_as_unchanged(self):
        run_cli(["timezone", "set", "Europe/Paris"])
        _, stdout, _ = run_cli(["timezone", "set", "Europe/Paris"])
        self.assertIn("unchanged", stdout)

    def test_a_bad_name_fails_and_stores_nothing(self):
        exit_code, stdout, _ = run_cli(["timezone", "set", "Mars/Olympus_Mons"])
        self.assertEqual(exit_code, 1)
        self.assertIsNone(test_db.get_setting("timezone"))

    def test_reset_forgets_the_stored_zone(self):
        run_cli(["timezone", "set", "Europe/Paris"])
        exit_code, stdout, _ = run_cli(["timezone", "reset"])
        self.assertEqual(exit_code, 0)
        self.assertIsNone(test_db.get_setting("timezone"))

    def test_reset_with_nothing_stored_says_so_and_succeeds(self):
        exit_code, stdout, _ = run_cli(["timezone", "reset"])
        self.assertEqual(exit_code, 0)
        self.assertIn("No timezone stored", stdout)

    def test_the_command_takes_effect_within_the_same_process(self):
        """`timezone set` runs in the same process as the next command under the bot's
        REPL and the test harness, so the resolved zone has to be dropped on write."""
        run_cli(["timezone", "set", FAR_EAST])
        self.assertEqual(today_date(), datetime.now(ZoneInfo(FAR_EAST)).date())
        run_cli(["timezone", "set", FAR_WEST])
        self.assertEqual(today_date(), datetime.now(ZoneInfo(FAR_WEST)).date())


class TestPushWindowFollowsTheAthlete(ClockTestCase):
    """The bot's morning push is scheduled on the athlete's wall clock, not the
    machine's (§2)."""

    def test_the_scheduler_reads_the_stored_zone(self):
        import trainmate_bot
        clock.set_timezone(FAR_EAST)
        self.assertEqual(trainmate_bot.athlete_now().date(),
                         datetime.now(ZoneInfo(FAR_EAST)).date())

    def test_the_window_is_computed_in_wall_clock_terms(self):
        import trainmate_bot
        # 07:00 local, whatever zone that is: the 08:00 push is an hour out.
        now = datetime(2026, 6, 10, 7, 0, tzinfo=ZoneInfo("Europe/Paris"))
        self.assertEqual(trainmate_bot.next_push_delay(now, "08:00", "15:00"), 3600.0)
        inside = datetime(2026, 6, 10, 9, 0, tzinfo=ZoneInfo("Europe/Paris"))
        self.assertEqual(trainmate_bot.next_push_delay(inside, "08:00", "15:00"), 0.0)


if __name__ == "__main__":
    unittest.main()
