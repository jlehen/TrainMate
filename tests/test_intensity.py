"""Intensity-distribution tests — see DESIGN_intensity_distribution.md §11.

Four things carry the design and are worth pinning: the completed-weeks divisor at a
block's first six days and across a partial tail (§4), the coverage formula with a
meterless ride in the set (§7), the canonical `cycling` mapping that stops one athlete's
riding splitting across two rows (§6.1), and the per-sport delta suppression that keeps a
changed sport mix from reading as a -100% swing (§4.1).
"""
import unittest

from trainmate import intensity
from trainmate.sports import canonical_sport


def act(date, sport, duration_sec, hr=None, power=None, rpe=None):
    """A completed-activity row, zone columns filled the way `garmin/sync.py` writes
    them: zeros for HR when nothing was recorded, not NULLs."""
    row = {
        "date": date, "activity_type": sport, "duration_sec": duration_sec, "rpe": rpe,
        "tss": None,
    }
    for i in range(1, 6):
        row[f"zone{i}_sec"] = (hr or [0] * 5)[i - 1]
    for i in range(1, 8):
        row[f"power_zone{i}_sec"] = (power or [0] * 7)[i - 1]
    return row


def fetch_from(activities):
    def fetch(start, end):
        return [a for a in activities if start <= a["date"] <= end]
    return fetch


def flat(text):
    """Whitespace-collapsed, so an assertion on a phrase doesn't depend on where the
    renderer chose to wrap it."""
    return " ".join(text.split())


BLOCK = {
    "name": "Base 2", "focus": "aerobic volume",
    "start_date": "2026-06-01", "end_date": "2026-06-28",
}


class TestCompletedWeeksDivisor(unittest.TestCase):
    """§4: only whole 7-day weeks from the block's own start divide a rate, and the
    partial tail is excluded from BOTH sides of the division."""

    def test_first_six_days_have_no_rate(self):
        # Day 6 of the block: nothing has completed a week, so there is no divisor.
        self.assertIsNone(
            intensity.rate_window("2026-06-01", "2026-06-28", "2026-06-06")
        )

    def test_seventh_day_yields_exactly_one_week(self):
        window = intensity.rate_window("2026-06-01", "2026-06-28", "2026-06-08")
        self.assertEqual(window, ("2026-06-01", "2026-06-07", 1))

    def test_partial_tail_is_excluded_from_the_window(self):
        # 16 days elapsed -> 2 completed weeks, and the window STOPS at day 14. The
        # numerator must drop the tail too, or the rate divides 16 days of work by 2.
        window = intensity.rate_window("2026-06-01", "2026-06-28", "2026-06-17")
        self.assertEqual(window, ("2026-06-01", "2026-06-14", 2))

    def test_finished_block_counts_its_last_day(self):
        # Measured one day past the end, all 28 days are over -> 4 weeks, not 3.
        window = intensity.rate_window("2026-06-01", "2026-06-28", "2026-06-29")
        self.assertEqual(window, ("2026-06-01", "2026-06-28", 4))

    def test_weeks_run_from_the_block_start_not_calendar_mondays(self):
        # 2026-06-04 is a Thursday; its weeks are Thursday-to-Wednesday.
        window = intensity.rate_window("2026-06-04", "2026-07-01", "2026-06-12")
        self.assertEqual(window, ("2026-06-04", "2026-06-10", 1))

    def test_too_young_block_reports_raw_minutes_and_says_so(self):
        acts = [act("2026-06-02", "running", 3600, hr=[300, 3000, 200, 100, 0])]
        text = intensity.block_report(BLOCK, "2026-06-05", fetch_from(acts))
        self.assertIn("RAW minutes", text)
        self.assertIn("too young for a per-week rate", flat(text))
        self.assertNotIn("per week over", text)

    def test_block_that_has_not_started_reports_nothing(self):
        # get_active_mesocycle falls back to the next FUTURE block; the helper must not
        # render an empty table for training that has not happened (§8).
        self.assertIsNone(
            intensity.block_report(BLOCK, "2026-05-20", fetch_from([]))
        )

    def test_rate_divides_by_completed_weeks_only(self):
        # 4h of Z2 inside the first two complete weeks, plus 2h in the partial tail that
        # must not enter the numerator: the rate is 2h/wk, not 3h/wk.
        acts = [
            act("2026-06-03", "running", 7200, hr=[0, 7200, 0, 0, 0]),
            act("2026-06-10", "running", 7200, hr=[0, 7200, 0, 0, 0]),
            act("2026-06-16", "running", 7200, hr=[0, 7200, 0, 0, 0]),
        ]
        text = intensity.block_report(BLOCK, "2026-06-17", fetch_from(acts))
        self.assertIn("Z2 aerobic 2h00", text)


class TestCoverage(unittest.TestCase):
    """§7: one formula, both currencies, one shared denominator."""

    def test_meterless_ride_dilutes_power_coverage_not_hr(self):
        # Two hours of riding: both recorded HR, only one had a power meter. Power
        # coverage must be ~50% of BIKE TIME — deriving it against HR time instead
        # would put two different denominators under one word.
        acts = [
            act("2026-06-02", "cycling", 3600, hr=[0, 3600, 0, 0, 0],
                power=[0, 3600, 0, 0, 0, 0, 0]),
            act("2026-06-03", "cycling", 3600, hr=[0, 3600, 0, 0, 0]),
        ]
        rows = intensity.zone_rows(acts)
        by_currency = {r.currency: r for r in rows}
        self.assertAlmostEqual(by_currency["hr"].coverage, 1.0, places=3)
        self.assertAlmostEqual(by_currency["power"].coverage, 0.5, places=3)

    def test_no_currency_row_without_recorded_seconds(self):
        # Strength training never grows a `[pwr] 0%` row.
        rows = intensity.zone_rows(
            [act("2026-06-02", "strength_training", 3600, hr=[600, 2400, 300, 0, 0])]
        )
        self.assertEqual([r.currency for r in rows], ["hr"])

    def test_low_coverage_reads_as_not_recorded(self):
        # An hour ridden, ten minutes inside any zone: the effort sat below Z1.
        rows = intensity.zone_rows(
            [act("2026-06-02", "running", 3600, hr=[600, 0, 0, 0, 0])]
        )
        self.assertAlmostEqual(rows[0].coverage, 1 / 6, places=3)

    def test_zero_zone_columns_do_not_invent_a_row(self):
        # sync.py writes zeros, not NULLs, when an activity has no average HR.
        self.assertEqual(intensity.zone_rows([act("2026-06-02", "hiking", 3600)]), [])


class TestCanonicalCycling(unittest.TestCase):
    """§6.1: one vocabulary, so an athlete's riding is not split across rows."""

    def test_every_bike_alias_folds_into_cycling(self):
        for alias in ("road_biking", "gravel_cycling", "cyclocross", "mountain_biking",
                      "bmx", "indoor_cycling", "virtual_ride", "road_cycling", "biking"):
            self.assertEqual(canonical_sport(alias), "cycling", alias)

    def test_gravel_and_road_share_one_row(self):
        rows = intensity.zone_rows([
            act("2026-06-02", "gravel_cycling", 3600, hr=[0, 3600, 0, 0, 0]),
            act("2026-06-03", "road_biking", 3600, hr=[0, 3600, 0, 0, 0]),
        ])
        self.assertEqual([r.sport for r in rows], ["cycling"])
        self.assertEqual(rows[0].seconds[1], 7200)

    def test_unknown_type_falls_through_as_its_own_row(self):
        # A miss is visible and repairable: you see the row and know what alias to add.
        rows = intensity.zone_rows(
            [act("2026-06-02", "e_bike_ride", 3600, hr=[0, 3600, 0, 0, 0])]
        )
        self.assertEqual(rows[0].sport, "e_bike_ride")


class TestZonesAndRendering(unittest.TestCase):
    """§5: every zone separately, named, with the percentage of RECORDED seconds."""

    def test_every_zone_is_reported_separately_and_named(self):
        acts = [act("2026-06-02", "running", 3600, hr=[600, 1800, 600, 480, 120])]
        text = intensity.block_report(BLOCK, "2026-06-09", fetch_from(acts))
        for label in ("Z1 recovery", "Z2 aerobic", "Z3 tempo", "Z4 threshold",
                      "Z5 VO2max+"):
            self.assertIn(label, text)
        self.assertNotIn("Z1-2", text)
        self.assertNotIn("Z4-5", text)

    def test_power_reports_all_seven_zones(self):
        acts = [act("2026-06-02", "cycling", 3600,
                    power=[300, 1800, 600, 480, 300, 100, 20])]
        text = intensity.block_report(BLOCK, "2026-06-09", fetch_from(acts))
        for label in ("Z5 VO2max", "Z6 anaerobic", "Z7 neuromuscular"):
            self.assertIn(label, text)

    def test_percentages_are_of_recorded_seconds_not_duration(self):
        # Half the hour was below Z1; the two recorded halves are still 50/50.
        rows = intensity.zone_rows(
            [act("2026-06-02", "running", 3600, hr=[0, 900, 900, 0, 0])]
        )
        cells = intensity._zone_cells(rows[0], divisor=1, with_pct=True)
        self.assertIn("(50%)", cells[1])
        self.assertIn("(50%)", cells[2])

    def test_hr_and_power_rows_carry_the_never_sum_warning(self):
        acts = [act("2026-06-02", "cycling", 3600, hr=[0, 3600, 0, 0, 0],
                    power=[0, 3600, 0, 0, 0, 0, 0])]
        text = intensity.block_report(BLOCK, "2026-06-09", fetch_from(acts))
        self.assertIn("never a total", text)

    def test_every_line_stays_inside_the_prompt_width(self):
        acts = [
            act("2026-06-02", "cycling", 5400, hr=[600, 3600, 700, 400, 100],
                power=[500, 3200, 800, 500, 200, 80, 20], rpe=6),
            act("2026-06-04", "strength_training", 3600, hr=[300, 2000, 600, 500, 30],
                rpe=8),
            act("2026-06-06", "trail_running", 7200, hr=[600, 5400, 900, 200, 60]),
        ]
        text = intensity.block_report(
            BLOCK, "2026-06-20", fetch_from(acts), current_week=True
        )
        for line in text.split("\n"):
            self.assertLessEqual(len(line), intensity.PROMPT_WIDTH, line)


class TestCurrentWeek(unittest.TestCase):
    """§9.3: raw minutes with the elapsed fraction, never extrapolated."""

    def test_current_week_is_raw_and_states_the_elapsed_fraction(self):
        acts = [
            act("2026-06-02", "running", 3600, hr=[0, 2400, 1200, 0, 0]),
            act("2026-06-16", "running", 1800, hr=[0, 600, 1200, 0, 0]),
        ]
        text = intensity.block_report(
            BLOCK, "2026-06-16", fetch_from(acts), current_week=True
        )
        self.assertIn("day 2 of 7 (29% elapsed)", flat(text))
        self.assertIn("NOT extrapolated", flat(text))
        # The week's own line is raw minutes — no percentages, nothing scaled up.
        week_line = [
            l for l in text.split("\n")
            if "Z3 tempo 20m" in l and "%" not in l
        ]
        self.assertTrue(week_line, text)

    def test_too_young_block_does_not_print_the_week_twice(self):
        # With no completed week the raw table IS the current week — same window, same
        # numbers.
        acts = [act("2026-06-02", "running", 3600, hr=[0, 3600, 0, 0, 0])]
        text = intensity.block_report(
            BLOCK, "2026-06-04", fetch_from(acts), current_week=True
        )
        self.assertIn("RAW minutes", text)
        self.assertNotIn("Current week so far", text)

    def test_current_week_window_tracks_the_block_grid(self):
        self.assertEqual(
            intensity.current_week_window("2026-06-01", "2026-06-28", "2026-06-16"),
            ("2026-06-15", "2026-06-16", 2),
        )


class TestDelta(unittest.TestCase):
    """§4.1: block-over-block change, suppressed per sport rather than globally."""

    PREV = {
        "name": "Base 1", "focus": "aerobic base",
        "start_date": "2026-05-04", "end_date": "2026-05-31",
    }

    def _report(self, acts):
        return intensity.block_report(
            BLOCK, "2026-06-29", fetch_from(acts), previous=self.PREV
        )

    def test_intensity_creep_shows_as_z2_down_and_z3_up(self):
        # The failure mode the feature exists for: same weekly load, easy volume traded
        # for tempo. Both blocks are 4 weeks, so per-week rates compare directly.
        acts = []
        for week in range(4):
            acts.append(act(f"2026-05-{4 + week * 7:02d}", "running", 20700,
                            hr=[3000, 18000, 2100, 900, 300]))
            acts.append(act(f"2026-06-{1 + week * 7:02d}", "running", 20700,
                            hr=[3300, 13140, 4800, 1080, 420]))
        text = self._report(acts)
        self.assertIn("Change vs Base 1", text)
        delta = "\n".join(
            text.split("Change vs Base 1")[1].split("Structural work")[0].split("\n")
        )
        self.assertIn("Z2 aerobic -1h21 (-27%)", delta)
        self.assertIn("Z3 tempo +45m (+129%)", delta)

    def test_a_sport_absent_before_is_reported_absent_not_as_a_swing(self):
        acts = [
            act("2026-05-06", "running", 3600, hr=[0, 3600, 0, 0, 0]),
            act("2026-06-03", "running", 3600, hr=[0, 3600, 0, 0, 0]),
            act("2026-06-05", "cycling", 3600, hr=[0, 3600, 0, 0, 0]),
        ]
        text = self._report(acts)
        self.assertIn("not trained in Base 1, no comparison", text)
        self.assertNotIn("-100%", text)

    def test_a_sport_dropped_since_is_reported_absent(self):
        acts = [
            act("2026-05-06", "running", 3600, hr=[0, 3600, 0, 0, 0]),
            act("2026-05-08", "cycling", 3600, hr=[0, 3600, 0, 0, 0]),
            act("2026-06-03", "running", 3600, hr=[0, 3600, 0, 0, 0]),
        ]
        text = self._report(acts)
        self.assertIn("present in Base 1, not trained here", text)

    def test_no_delta_when_the_current_block_has_no_completed_week(self):
        acts = [act("2026-05-06", "running", 3600, hr=[0, 3600, 0, 0, 0])]
        text = intensity.block_report(
            BLOCK, "2026-06-04", fetch_from(acts + [
                act("2026-06-02", "running", 3600, hr=[0, 3600, 0, 0, 0])
            ]), previous=self.PREV
        )
        self.assertNotIn("Change vs", text)


class TestStructural(unittest.TestCase):
    """§6: no sport is routed away — the structural row sits beside the zone rows."""

    def test_hiit_strength_keeps_its_hard_minutes_in_the_zone_table(self):
        # Garmin files a HIIT kettlebell session as `indoor_cardio`, which folds into
        # strength_training. Routing it away would report ZERO hard minutes for a block
        # containing four of them.
        acts = [
            act("2026-06-02", "indoor_cardio", 3600, hr=[300, 1200, 900, 1100, 100],
                rpe=8),
        ]
        text = intensity.block_report(BLOCK, "2026-06-09", fetch_from(acts))
        self.assertIn("strength_training", text)
        self.assertIn("Z4 threshold 18m", text)
        self.assertIn("avg RPE 8.0", text)

    def test_benchmark_movement_inside_the_window_is_reported(self):
        acts = [act("2026-06-02", "cycling", 3600, hr=[0, 3600, 0, 0, 0], rpe=5)]
        benchmarks = [
            {"anchor_kind": "ftp", "date": "2026-04-01", "value": 240.0, "id": 1},
            {"anchor_kind": "ftp", "date": "2026-06-10", "value": 252.0, "id": 2},
        ]
        text = intensity.block_report(
            BLOCK, "2026-06-20", fetch_from(acts), benchmarks=benchmarks
        )
        self.assertIn("240 W -> 252 W (+5.0%)", text)

    def test_volume_and_load_line_survives_the_zone_table(self):
        acts = [act("2026-06-02", "running", 3600, hr=[0, 3600, 0, 0, 0])]
        text = intensity.block_report(BLOCK, "2026-06-09", fetch_from(acts))
        self.assertIn("Volume and load", text)
        self.assertIn("1 session, 1h00", text)

    def test_block_with_no_activities_says_so_once(self):
        text = intensity.block_report(BLOCK, "2026-06-09", fetch_from([]))
        self.assertIn("No completed activities recorded", text)
        self.assertNotIn("Coverage:", text)


class TestNotes(unittest.TestCase):
    """§9.6: `HR_REST_NOTE` held two claims with different scopes joined by an 'and'.
    Strength is a property of the sport and is suppressed when that sport is off screen;
    interval work with rest happens in running, cycling and rowing alike and is not."""

    def _rows(self, *acts):
        return intensity.zone_rows(list(acts))

    def test_interval_note_travels_with_any_hr_row(self):
        rows = self._rows(act("2026-06-02", "running", 3600, hr=[0, 3600, 0, 0, 0]))
        text = flat("\n".join(intensity.format_notes(rows)))
        self.assertIn("interval work with rest", text)
        self.assertNotIn("rest between sets", text)

    def test_strength_note_only_when_a_strength_sport_is_on_screen(self):
        rows = self._rows(
            act("2026-06-02", "strength_training", 3600, hr=[0, 1800, 1800, 0, 0])
        )
        text = flat("\n".join(intensity.format_notes(rows)))
        self.assertIn("rest between sets", text)
        self.assertIn("interval work with rest", text)

    def test_indoor_cardio_folds_into_strength_and_keeps_the_note(self):
        rows = self._rows(act("2026-06-02", "indoor_cardio", 3600, hr=[0, 0, 0, 3600, 0]))
        text = flat("\n".join(intensity.format_notes(rows)))
        self.assertIn("rest between sets", text)

    def test_power_only_rows_carry_neither_hr_note(self):
        rows = self._rows(
            act("2026-06-02", "cycling", 3600, power=[0, 3600, 0, 0, 0, 0, 0])
        )
        text = flat("\n".join(intensity.format_notes(rows)))
        self.assertNotIn("interval work with rest", text)
        self.assertNotIn("rest between sets", text)

    def test_block_report_can_leave_the_notes_to_its_caller(self):
        acts = [act("2026-06-02", "running", 3600, hr=[0, 3600, 0, 0, 0])]
        with_notes = intensity.block_report(BLOCK, "2026-06-09", fetch_from(acts))
        without = intensity.block_report(
            BLOCK, "2026-06-09", fetch_from(acts), notes=False
        )
        self.assertIn("interval work with rest", flat(with_notes))
        self.assertNotIn("interval work with rest", flat(without))
        # Everything else is unchanged: only the caveats move.
        self.assertIn("Coverage:", without)


class TestPickCurrency(unittest.TestCase):
    def test_power_below_the_bar_loses_to_fuller_hr(self):
        self.assertEqual(
            intensity.pick_currency({"power": 0.6, "hr": 0.95}), "hr"
        )

    def test_power_at_the_bar_wins_even_against_fuller_hr(self):
        self.assertEqual(
            intensity.pick_currency({"power": 0.8, "hr": 0.99}), "power"
        )

    def test_power_wins_when_it_is_the_only_currency(self):
        self.assertEqual(intensity.pick_currency({"power": 0.4}), "power")

    def test_nothing_recorded_picks_nothing(self):
        self.assertIsNone(intensity.pick_currency({}))
        self.assertIsNone(intensity.pick_currency({"power": 0.0, "hr": 0.0}))


class TestSportDurations(unittest.TestCase):
    def test_a_sport_with_sessions_and_no_zone_rows_still_has_a_duration(self):
        acts = [act("2026-06-02", "yoga", 1800)]
        self.assertEqual(intensity.sport_durations(acts), {"yoga": 1800})
        self.assertEqual(intensity.zone_rows(acts), [])

    def test_aliases_fold_into_one_canonical_duration(self):
        acts = [act("2026-06-02", "gravel_cycling", 3600),
                act("2026-06-03", "road_biking", 1800)]
        self.assertEqual(intensity.sport_durations(acts), {"cycling": 5400})


class TestPromptWidthContract(unittest.TestCase):
    def test_block_report_prose_respects_the_width_it_is_given(self):
        # §9.6: `format_header` and the `Change vs` header were bare appends measuring
        # 57 and 84 characters at width=48, so the column contract was false there.
        acts = [act("2026-06-02", "running", 3600, hr=[0, 3600, 0, 0, 0]),
                act("2026-05-06", "running", 3600, hr=[0, 3000, 600, 0, 0])]
        previous = {"name": "Base 1 — Aerobic Volume Accumulation",
                    "focus": "aerobic volume accumulation across a long base",
                    "start_date": "2026-05-04", "end_date": "2026-05-31"}
        text = intensity.block_report(
            BLOCK, "2026-06-16", fetch_from(acts), previous=previous,
            notes=False, indent="", width=48,
        )
        for line in text.split("\n"):
            self.assertLessEqual(len(line), 48, msg=repr(line))
        self.assertIn("Change vs", flat(text))


if __name__ == "__main__":
    unittest.main()
