"""The run journal: the writer, the run bracket, retention, and `tm journal`.

The three kinds DESIGN_logging.md §11 asks for — a unit test on the writer, a
behavioural test on the bracket, and a structural test on the silent swallows — plus the
reader and the command that renders it.
"""
import ast
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

from trainmate import journal
from trainmate.config import config
from trainmate.prompt import PromptCancelled
from trainmate.util import default_wrap_width, visible_len
from tests.helpers import bind_test_db, run_cli
from tests import test_db_path

TEST_DB_PATH = test_db_path("test_journal.db")


def setUpModule():
    """`tm journal` renders every stamp through the athlete's zone, which is a settings
    row, so the reader needs a database even though the writer never opens one (§4)."""
    bind_test_db(TEST_DB_PATH)


class JournalTestCase(unittest.TestCase):
    """Points `logging.dir` at a scratch directory for the life of one test."""

    def setUp(self) -> None:
        self.log_dir = tempfile.mkdtemp(prefix="tm-journal-test-")
        self.addCleanup(shutil.rmtree, self.log_dir, True)
        block = config.data.setdefault("logging", {})
        previous = dict(block)
        self.addCleanup(lambda: (block.clear(), block.update(previous)))
        block["dir"] = self.log_dir
        journal.reset()
        self.addCleanup(journal.reset)

    def lines(self):
        """Every raw line written today, in order."""
        path = journal.today_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [line for line in handle.read().split("\n") if line]

    def records(self):
        return [json.loads(line) for line in self.lines()]

    def _seed_runs(self):
        """The two runs most of the listing tests read: one failed, one that called out."""
        journal.start_run(["plan", "generate", "-g", "2"], source="push")
        journal.name_run("plan", "generate")
        journal.llm_call(
            label="plan_generate", model="anthropic/claude-opus-5", ms=88400, ok=True,
            total_tokens=210412, path="logs/llm_exchanges/x_plan_generate.md",
        )
        journal.end_run("failed", exit_code=1, error="KeyError: 'mesocycles'",
                        traceback_text="Traceback (most recent call last):\n  boom")
        journal.start_run(["workout", "adapt", "-m", "legs heavy"], source="bot")
        journal.name_run("workout", "adapt")
        journal.llm_call(
            label="workout_adaptation", model="google/gemini-3.5-flash", ms=24118,
            ok=True, total_tokens=38104,
        )
        journal.end_run("ok")
        journal.reset()

    def _seed(self, argv, path=None, outcome="ok", lvl=None, source="cli",
              msg="the calendar did not answer"):
        """One finished run of `argv`, named as the dispatcher would name it."""
        journal.start_run(argv, source=source)
        if path:
            journal.name_run(*path.split(" "))
        if lvl:
            journal.note(msg, lvl=lvl)
        journal.end_run(outcome)
        journal.reset()


class TestWriter(JournalTestCase):
    """One record is one line, bounded, and never takes the command down (§4.3)."""

    def test_a_record_round_trips(self):
        journal.record("note", "Auto-syncing Garmin", lvl="warn", days=3)
        (rec,) = self.records()
        self.assertEqual(rec["ev"], "note")
        self.assertEqual(rec["msg"], "Auto-syncing Garmin")
        self.assertEqual(rec["lvl"], "warn")
        self.assertEqual(rec["d"], {"days": 3})
        self.assertEqual(rec["run"], journal.NO_RUN)
        self.assertTrue(rec["ts"].endswith("Z"))

    def test_a_none_valued_field_is_dropped_rather_than_written_as_null(self):
        journal.record("note", "x", days=None)
        self.assertNotIn("d", self.records()[0])

    def test_the_day_file_is_named_for_the_utc_day_not_the_athletes(self):
        # §4: naming the file from the athlete's zone would build and migrate the
        # database, on `tm help` and in the path that must survive it being unreachable.
        journal.record("note", "x")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        self.assertEqual(
            os.listdir(journal.runs_dir()), [f"{today}.jsonl"]
        )

    def test_a_multiline_message_is_still_one_line(self):
        journal.record("note", "first\nsecond\nthird")
        self.assertEqual(len(self.lines()), 1)
        self.assertEqual(self.records()[0]["msg"], "first\nsecond\nthird")

    def test_an_over_long_traceback_is_elided_in_the_middle(self):
        head, tail = "HEAD" + "a" * 4000, "b" * 4000 + "TAIL"
        journal.record("run.end", "failed", lvl="error", traceback=head + tail)
        line = self.lines()[0]
        self.assertLessEqual(len(line.encode("utf-8")), journal.MAX_RECORD_BYTES)
        trace = self.records()[0]["d"]["traceback"]
        self.assertTrue(trace.startswith("HEAD"))
        self.assertTrue(trace.endswith("TAIL"))
        self.assertIn("elided", trace)

    def test_an_over_long_message_is_cut_and_the_record_stays_parseable(self):
        journal.record("note", "x" * 40000)
        line = self.lines()[0]
        self.assertLessEqual(len(line.encode("utf-8")), journal.MAX_RECORD_BYTES)
        self.assertTrue(self.records()[0]["msg"].endswith("…[cut]"))

    def test_a_debug_record_is_dropped_at_the_default_level(self):
        journal.debug("internal", "swallowed")
        self.assertEqual(self.lines(), [])
        config.data["logging"]["level"] = "debug"
        journal.debug("internal", "swallowed")
        self.assertEqual(self.records()[0]["lvl"], "debug")

    def test_an_unwritable_directory_neither_raises_nor_repeats_itself(self):
        blocker = os.path.join(self.log_dir, "blocker")
        with open(blocker, "w") as handle:
            handle.write("not a directory")
        config.data["logging"]["dir"] = blocker
        buffer = io.StringIO()
        with patch.object(sys, "stderr", buffer):
            journal.record("note", "one")
            journal.record("note", "two")
            journal.record("note", "three")
        self.assertEqual(len(buffer.getvalue().strip().split("\n")), 1)

    def test_seq_counts_within_the_run_not_the_file(self):
        journal.start_run(["status"])
        journal.record("note", "a")
        journal.end_run("ok")
        journal.start_run(["help"])
        journal.end_run("ok")
        seqs = [(rec["run"], rec["seq"]) for rec in self.records()]
        first = seqs[0][0]
        self.assertEqual([s for r, s in seqs if r == first], [0, 1, 2])
        self.assertEqual([s for r, s in seqs if r != first], [0, 1])


class TestReader(JournalTestCase):
    """A line that is not valid JSON is skipped, not raised on (§4.3)."""

    def test_a_torn_line_is_skipped_and_its_neighbours_survive(self):
        journal.record("note", "before")
        with open(journal.today_path(), "a", encoding="utf-8") as handle:
            handle.write('{"ts":"2026-08-26T00:00:00.000Z","run":"aaaa\n')
            handle.write("not json at all\n")
            handle.write("[1, 2, 3]\n")
        journal.record("note", "after")
        msgs = [rec["msg"] for rec in journal.iter_records()]
        self.assertEqual(msgs, ["before", "after"])

    def test_the_window_opens_one_file_either_side_of_what_was_asked(self):
        # A local day straddles two UTC files, so the reader widens by a day (§4).
        for day in ("2026-08-01", "2026-08-02", "2026-08-03", "2026-08-04", "2026-08-05"):
            path = os.path.join(journal.runs_dir(), f"{day}.jsonl")
            os.makedirs(journal.runs_dir(), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(json.dumps({"ts": day, "ev": "note", "msg": day}) + "\n")
        opened = journal.day_paths(date(2026, 8, 3), date(2026, 8, 3))
        self.assertEqual(
            [os.path.basename(p) for p in opened],
            ["2026-08-02.jsonl", "2026-08-03.jsonl", "2026-08-04.jsonl"],
        )

    def test_a_file_that_is_not_a_day_is_never_opened(self):
        os.makedirs(journal.runs_dir(), exist_ok=True)
        with open(os.path.join(journal.runs_dir(), "notes.txt"), "w") as handle:
            handle.write("hand-written\n")
        self.assertEqual(journal.day_paths(), [])


class TestLlmDurations(JournalTestCase):
    """The wait estimate's data source (DESIGN_output_verbosity.md §8)."""

    def _write_day(self, day: str, calls) -> None:
        """One day file carrying `calls` as `(label, model, ms, ok)` tuples."""
        os.makedirs(journal.runs_dir(), exist_ok=True)
        path = os.path.join(journal.runs_dir(), f"{day}.jsonl")
        with open(path, "w", encoding="utf-8") as handle:
            for label, model, ms, ok in calls:
                handle.write(json.dumps({
                    "ts": f"{day}T09:00:00.000Z", "run": "aaaa", "ev": "llm.call",
                    "msg": label,
                    "d": {"label": label, "model": model, "ms": ms, "ok": ok},
                }) + "\n")

    def _days_ago(self, count: int) -> str:
        return (datetime.now(timezone.utc).date() - timedelta(days=count)).isoformat()

    def test_only_this_label_and_model_count(self):
        self._write_day(self._days_ago(1), [
            ("workout_adapt", "m1", 40000, True),
            ("workout_adapt", "m2", 90000, True),   # another model
            ("plan_generate", "m1", 99000, True),   # another command
        ])
        self.assertEqual(journal.llm_durations("workout_adapt", "m1"), [40000])

    def test_a_failed_call_is_not_a_sample(self):
        # A call that died on a timeout says nothing about how long a working one takes.
        self._write_day(self._days_ago(1), [
            ("workout_adapt", "m1", 40000, True),
            ("workout_adapt", "m1", 600000, False),
        ])
        self.assertEqual(journal.llm_durations("workout_adapt", "m1"), [40000])

    def test_newest_first_across_days_and_within_one(self):
        self._write_day(self._days_ago(2), [("workout_adapt", "m1", 10000, True)])
        self._write_day(self._days_ago(1), [
            ("workout_adapt", "m1", 20000, True),
            ("workout_adapt", "m1", 30000, True),
        ])
        self.assertEqual(
            journal.llm_durations("workout_adapt", "m1"), [30000, 20000, 10000]
        )

    def test_the_scan_stops_at_the_limit(self):
        for age in range(1, 6):
            self._write_day(self._days_ago(age), [("workout_adapt", "m1", age, True)])
        self.assertEqual(journal.llm_durations("workout_adapt", "m1", limit=2), [1, 2])

    def test_a_day_outside_the_window_is_not_read(self):
        self._write_day(self._days_ago(40), [("workout_adapt", "m1", 40000, True)])
        self.assertEqual(journal.llm_durations("workout_adapt", "m1", days=30), [])

    def test_no_model_given_takes_every_model(self):
        self._write_day(self._days_ago(1), [
            ("workout_adapt", "m1", 40000, True),
            ("workout_adapt", "m2", 90000, True),
        ])
        self.assertEqual(
            sorted(journal.llm_durations("workout_adapt")), [40000, 90000]
        )

    def test_a_torn_line_costs_only_itself(self):
        os.makedirs(journal.runs_dir(), exist_ok=True)
        day = self._days_ago(1)
        self._write_day(day, [("workout_adapt", "m1", 40000, True)])
        with open(os.path.join(journal.runs_dir(), f"{day}.jsonl"), "a") as handle:
            handle.write("{not json\n")
        self.assertEqual(journal.llm_durations("workout_adapt", "m1"), [40000])


class TestRunBracket(JournalTestCase):
    """Every command opens and closes a run, and says how it ended (§3, §5.4)."""

    def _runs(self):
        """(run id -> [record...]) for everything written."""
        out = {}
        for rec in journal.iter_records():
            out.setdefault(rec["run"], []).append(rec)
        return out

    def _ends(self):
        return [rec for rec in journal.iter_records() if rec["ev"] == "run.end"]

    def test_every_start_has_an_end(self):
        run_cli(["help"])
        run_cli(["journal", "-n", "1"])
        starts = [r for r in journal.iter_records() if r["ev"] == "run.start"]
        ends = self._ends()
        self.assertEqual(len(starts), 2)
        self.assertEqual({r["run"] for r in starts}, {r["run"] for r in ends})

    def test_run_start_names_the_command_without_touching_the_database(self):
        run_cli(["help"])
        (start,) = [r for r in journal.iter_records() if r["ev"] == "run.start"]
        self.assertEqual(start["msg"], "help")
        self.assertEqual(start["d"]["argv"], ["help"])
        self.assertEqual(start["d"]["source"], "test")
        self.assertIn("pid", start["d"])
        # The model is deliberately absent: resolving it would build the database (§3).
        self.assertNotIn("model", start["d"])

    def test_run_end_names_the_canonical_command_whatever_prefix_was_typed(self):
        run_cli(["j", "-n", "1"])          # `j` is an unambiguous prefix of `journal`
        (end,) = self._ends()
        self.assertEqual(end["d"]["cmd"], "journal")

    def test_a_command_that_raises_records_its_traceback_and_fails(self):
        import trainmate_cli
        with patch.object(trainmate_cli, "_dispatch", side_effect=KeyError("mesocycles")):
            run_cli(["status"])
        (end,) = self._ends()
        self.assertEqual(end["d"]["outcome"], "failed")
        self.assertEqual(end["lvl"], "error")
        self.assertEqual(end["d"]["error"], "KeyError: 'mesocycles'")
        self.assertIn("KeyError", end["d"]["traceback"])

    def test_a_cancelled_command_is_cancelled_and_not_failed(self):
        import trainmate_cli
        with patch.object(trainmate_cli, "_dispatch", side_effect=PromptCancelled()):
            run_cli(["status"])
        (end,) = self._ends()
        self.assertEqual(end["d"]["outcome"], "cancelled")
        self.assertEqual(end["d"]["exit"], 130)

    def test_a_domain_refusal_and_an_argparse_exit_both_read_as_ok(self):
        run_cli(["goal"])          # a command group invoked bare: argparse exits 1
        (end,) = self._ends()
        self.assertEqual(end["d"]["outcome"], "ok")
        self.assertEqual(end["d"]["exit"], 1)

    def test_three_lines_in_a_shell_make_four_runs_with_one_parent(self):
        import trainmate_cli
        typed = iter(["help", "help", "help"])

        def _input(_prompt=""):
            try:
                return next(typed)
            except StopIteration:
                raise EOFError

        # main() directly rather than helpers.run_cli: that helper pins input() to one
        # constant answer, which the REPL would read as the same line forever.
        with patch("builtins.input", _input), patch.object(sys, "stdout", io.StringIO()):
            trainmate_cli.main(["shell"])
        starts = [r for r in journal.iter_records() if r["ev"] == "run.start"]
        self.assertEqual(len(starts), 4)
        (shell,) = [s for s in starts if s["msg"] == "shell"]
        children = [s for s in starts if s["msg"] == "help"]
        self.assertEqual(len(children), 3)
        for child in children:
            self.assertEqual(child["d"]["parent"], shell["run"])
            self.assertEqual(child["d"]["source"], "repl")

    def test_a_spawned_process_inherits_the_parent_run_and_its_own_source(self):
        journal.start_run(["tm-bot"], source="bot")
        env = journal.child_env({"TRAINMATE_PARENT_RUN": "stale"}, "push")
        self.assertEqual(env["TRAINMATE_PARENT_RUN"], journal.current_id())
        self.assertEqual(env["TRAINMATE_SOURCE"], "push")
        journal.end_run("ok")
        # With no run open, an inherited parent is dropped rather than passed on.
        self.assertNotIn("TRAINMATE_PARENT_RUN", journal.child_env(dict(env), "cli"))

    def test_the_rollup_counts_what_the_run_wrote(self):
        journal.start_run(["plan", "generate"])
        journal.note("careful", lvl="warn")
        journal.llm_call(
            label="plan_generate", model="m", ms=10, ok=True, total_tokens=1200
        )
        journal.end_run("ok")
        (end,) = self._ends()
        self.assertEqual(end["d"]["warns"], 1)
        self.assertEqual(end["d"]["llm_calls"], 1)
        self.assertEqual(end["d"]["tokens"], 1200)


class TestRetention(JournalTestCase):
    """The sweep deletes by name shape, and runs at most once a UTC day (§10)."""

    def _seed(self, runs=(), exchanges=()):
        os.makedirs(journal.runs_dir(), exist_ok=True)
        os.makedirs(config.llm_logs_dir, exist_ok=True)
        for name in runs:
            open(os.path.join(journal.runs_dir(), name), "w").close()
        for name in exchanges:
            open(os.path.join(config.llm_logs_dir, name), "w").close()

    def test_only_files_past_their_retention_and_of_the_right_shape_are_deleted(self):
        config.data["logging"]["retain_days"] = 5
        config.data["logging"]["retain_exchange_days"] = 5
        old = (datetime.now(timezone.utc).date() - timedelta(days=30))
        fresh = (datetime.now(timezone.utc).date() - timedelta(days=1))
        self._seed(
            runs=[f"{old}.jsonl", f"{fresh}.jsonl", "notes.txt"],
            exchanges=[
                f"{old:%Y%m%d}_120000_1_a1b2c3d4_plan.md",
                f"{fresh:%Y%m%d}_120000_1_plan.md",
                "hand-written.md",
            ],
        )
        self.assertEqual(journal.prune(), (1, 1))
        self.assertEqual(
            sorted(os.listdir(journal.runs_dir())), sorted([f"{fresh}.jsonl", "notes.txt"])
        )
        self.assertEqual(
            sorted(os.listdir(config.llm_logs_dir)),
            sorted([f"{fresh:%Y%m%d}_120000_1_plan.md", "hand-written.md"]),
        )

    def test_the_stamp_file_gates_the_sweep_to_once_a_day(self):
        config.data["logging"]["retain_days"] = 5
        old = datetime.now(timezone.utc).date() - timedelta(days=30)
        self._seed(runs=[f"{old}.jsonl"])
        journal.start_run(["probe"])
        journal.end_run("ok")                       # sweeps, and stamps the day
        self.assertFalse(os.path.exists(os.path.join(journal.runs_dir(), f"{old}.jsonl")))
        self._seed(runs=[f"{old}.jsonl"])
        journal.start_run(["probe"])
        journal.end_run("ok")                       # already stamped: no second sweep
        self.assertTrue(os.path.exists(os.path.join(journal.runs_dir(), f"{old}.jsonl")))

    def test_only_the_outermost_run_ending_considers_a_sweep(self):
        journal.start_run(["shell"])
        journal.start_run(["help"])
        journal.end_run("ok")
        stamp = os.path.join(journal.runs_dir(), journal.PRUNE_STAMP)
        self.assertFalse(os.path.exists(stamp))
        journal.end_run("ok")
        self.assertTrue(os.path.exists(stamp))


class TestJournalCommand(JournalTestCase):
    """`tm journal` reads the files back in the athlete's terms (§7)."""

    def test_the_listing_shows_one_row_per_run_with_its_outcome(self):
        self._seed_runs()
        _code, out, _err = run_cli(["journal", "-v"])
        self.assertIn("plan generate -g 2", out)
        self.assertIn("FAILED", out)
        self.assertIn("workout adapt", out)
        self.assertIn("1 · 210k", out)

    def test_a_run_with_no_end_reads_as_a_question_mark(self):
        journal.start_run(["plan", "generate"], source="bot")
        journal.reset()          # killed before it could write a run.end
        _code, out, _err = run_cli(["journal"])
        self.assertIn("?", out)

    def test_an_id_prefix_opens_the_detail_view(self):
        self._seed_runs()
        run_id = [
            rec["run"] for rec in journal.iter_records()
            if rec["ev"] == "run.start" and rec["msg"].startswith("plan")
        ][0]
        _code, out, _err = run_cli(["journal", run_id[:4]])
        self.assertIn(f"run {run_id}", out)
        self.assertIn("llm.call", out)
        self.assertIn("x_plan_generate.md", out)
        self.assertIn("Traceback (most recent call last):", out)

    def test_an_ambiguous_prefix_lists_what_it_matched_rather_than_guessing(self):
        with patch("trainmate.journal.secrets.token_hex",
                   side_effect=["ab121234", "ab125678"]):
            for argv in (["status"], ["help"]):
                journal.start_run(argv)
                journal.end_run("ok")
        journal.reset()
        _code, out, _err = run_cli(["journal", "ab12"])
        self.assertIn("matches 2 runs", out)
        self.assertIn("ab121234", out)
        self.assertIn("ab125678", out)

    def test_prune_is_a_sub_command_and_not_read_as_a_run_id(self):
        self._seed_runs()
        _code, out, _err = run_cli(["journal", "prune"])
        self.assertIn("Pruned", out)

    def test_the_cost_rollup_groups_by_model_and_by_command(self):
        self._seed_runs()
        _code, out, _err = run_cli(["journal", "--cost"])
        self.assertIn("anthropic/claude-opus-5", out)
        self.assertIn("210,412", out)
        self.assertIn("plan generate", out)
        self.assertIn("248,516", out)          # the total row

    def test_the_filters_narrow_to_one_source_and_to_trouble(self):
        self._seed_runs()
        _code, out, _err = run_cli(["journal", "-v", "--source", "bot"])
        self.assertIn("workout adapt", out)
        self.assertNotIn("plan generate", out)
        _code, out, _err = run_cli(["journal", "-v", "--failed"])
        self.assertIn("plan generate", out)
        self.assertNotIn("workout adapt", out)


class TestJournalListing(JournalTestCase):
    """What the default listing leaves out, and what the footer says about it (§7.1)."""

    def test_a_run_that_only_looked_is_left_out_until_a_asks_for_it(self):
        self._seed(["workout", "list"], "workout list")
        self._seed(["wo", "a"], "workout adapt")
        _code, out, _err = run_cli(["journal"])
        self.assertIn("wo a", out)                       # the line as it was typed
        self.assertNotIn("workout list", out)
        self.assertIn("1 read-only run(s) hidden", out)
        _code, out, _err = run_cli(["journal", "-a"])
        self.assertIn("workout list", out)

    def test_a_view_that_went_wrong_is_never_hidden(self):
        self._seed(["workout", "list"], "workout list", lvl="warn")
        _code, out, _err = run_cli(["journal"])
        self.assertIn("workout list", out)
        self.assertIn("warn", out)
        self.assertIn("the calendar did not answer", out)

    def test_a_help_run_is_left_out_whatever_it_asked_about(self):
        # `-h` exits inside argparse, so the run is never named: the argv is the tell.
        self._seed(["workout", "adapt", "-h"])
        _code, out, _err = run_cli(["journal"])
        self.assertNotIn("workout adapt", out)
        self.assertIn("1 read-only run(s) hidden", out)

    def test_naming_a_command_lists_it_views_included(self):
        self._seed(["wo", "li"], "workout list")
        _code, out, _err = run_cli(["journal", "--command", "workout list"])
        self.assertIn("wo li", out)
        self.assertNotIn("hidden", out)

    def test_the_command_column_is_clipped_to_the_screen_unless_v_asks(self):
        note = "the intervals ran long and the second round was short, " * 4
        self._seed(["workout", "adapt", "-m", note], "workout adapt")
        _code, out, _err = run_cli(["journal"])
        self.assertNotIn(note, out)
        self.assertIn("…", out)
        self.assertIn("command lines clipped", out)
        for line in out.split("\n"):
            self.assertLessEqual(visible_len(line), default_wrap_width())
        _code, out, _err = run_cli(["journal", "-v"])
        self.assertIn(note, out)
        self.assertNotIn("command lines clipped", out)

    def test_the_legend_glosses_the_outcomes_on_screen_and_no_others(self):
        self._seed_runs()
        _code, out, _err = run_cli(["journal"])
        self.assertIn("END  ok = finished", out)
        self.assertIn("FAILED = raised", out)
        self.assertNotIn("stopped with Ctrl-C", out)     # nothing was cancelled
        self.assertIn("LLM  model calls · tokens", out)


class TestJournalReasons(JournalTestCase):
    """A row that is not `ok` says why, on the same screen (§7.3)."""

    def test_a_run_that_warned_says_what_it_warned_about(self):
        self._seed(["data", "pull"], "data pull", lvl="warn",
                   msg="Garmin sync failed, continuing with cached data")
        _code, out, _err = run_cli(["journal"])
        self.assertIn("Garmin sync failed, continuing with cached data", out)

    def test_the_first_warning_is_the_one_shown(self):
        journal.start_run(["data", "pull"])
        journal.name_run("data", "pull")
        journal.note("the calendar did not answer", lvl="warn")
        journal.note("and neither did Garmin", lvl="warn")
        journal.end_run("ok")
        journal.reset()
        _code, out, _err = run_cli(["journal"])
        self.assertIn("the calendar did not answer", out)
        self.assertNotIn("and neither did Garmin", out)

    def test_a_failed_run_names_the_exception_and_not_a_warning_on_the_way(self):
        journal.start_run(["plan", "generate"])
        journal.name_run("plan", "generate")
        journal.note("the calendar did not answer", lvl="warn")
        journal.end_run("failed", exit_code=1, error="KeyError: 'mesocycles'")
        journal.reset()
        _code, out, _err = run_cli(["journal"])
        self.assertIn("KeyError: 'mesocycles'", out)
        self.assertNotIn("the calendar did not answer", out)

    def test_a_quiet_listing_prints_no_reasons_at_all(self):
        self._seed(["data", "pull"], "data pull")
        _code, out, _err = run_cli(["journal"])
        self.assertNotIn("warnings clipped", out)
        self.assertEqual(out.count("data pull"), 1)     # the row, and no line under it

    def test_a_long_warning_is_clipped_to_the_screen_until_v_asks(self):
        self._seed(["data", "pull"], "data pull", lvl="warn", msg=(
            "2 activities had low HR-zone coverage and no RPE; their load is an "
            "underestimate:\n    - 2026-08-27 Warm-up\n    - 2026-08-26 Evening Ride"
        ))
        _code, out, _err = run_cli(["journal"])
        self.assertIn("warnings clipped (-v for the full text)", out)
        self.assertNotIn("Evening Ride", out)
        for line in out.split("\n"):
            self.assertLessEqual(visible_len(line), default_wrap_width())
        _code, out, _err = run_cli(["journal", "-v"])
        self.assertIn("- 2026-08-26 Evening Ride", out)  # the list keeps its own shape
        self.assertNotIn("warnings clipped", out)
        for line in out.split("\n"):
            self.assertLessEqual(visible_len(line), default_wrap_width())


class TestOutputVerbs(JournalTestCase):
    """step/warn/fail print where they always did, and record what they printed (§5.1)."""

    def test_step_prints_like_an_aside_and_journals_it(self):
        from trainmate.util import step
        buffer = io.StringIO()
        with patch.object(sys, "stdout", buffer):
            step("Auto-syncing Garmin 2026-08-22..2026-08-24...")
        self.assertIn("Auto-syncing Garmin", buffer.getvalue())
        (rec,) = self.records()
        self.assertEqual(rec["ev"], "note")
        self.assertEqual(rec["lvl"], "info")

    def test_warn_and_fail_carry_their_prefix_and_level(self):
        from trainmate.util import fail, warn
        buffer = io.StringIO()
        with patch.object(sys, "stdout", buffer):
            warn("Garmin sync failed. Continuing with cached data.")
            fail("Google Calendar event delete failed: boom")
        printed = buffer.getvalue()
        self.assertIn("Warning: Garmin sync failed.", printed)
        self.assertIn("Error: Google Calendar event delete failed", printed)
        self.assertEqual([rec["lvl"] for rec in self.records()], ["warn", "error"])

    def test_a_journalled_message_carries_no_colour_codes(self):
        from trainmate.util import cmd, warn
        with patch("trainmate.util.is_color_enabled", return_value=True):
            with patch.object(sys, "stdout", io.StringIO()):
                warn("run " + cmd("data pull") + " in a terminal")
        self.assertEqual(self.records()[0]["msg"], "run 'data pull' in a terminal")


class TestPromptAnswers(JournalTestCase):
    """What the athlete answered, on the run that asked (DESIGN_logging.md §5.6).

    A declined `workout generate` finishes normally, so `run.end` says `ok` exactly as an
    applied one does; these records are the only thing that tells the two apart."""

    def setUp(self) -> None:
        super().setUp()
        from trainmate.prompt import TtyPrompt
        self.prompt = TtyPrompt()

    def _answers(self):
        return [rec for rec in self.records() if rec["ev"] == "note"]

    def test_a_declined_question_is_recorded_on_the_run_that_asked_it(self):
        run_id = journal.start_run(["workout", "generate"], source="cli")
        with patch("builtins.input", return_value="n"):
            answered = self.prompt.confirm("Schedule these 7 workout(s)?")
        journal.end_run("ok")
        self.assertFalse(answered)
        (rec,) = self._answers()
        self.assertEqual(rec["run"], run_id)
        self.assertEqual(rec["lvl"], "info")
        self.assertEqual(rec["msg"], "Schedule these 7 workout(s)? → no")
        self.assertIs(rec["d"]["answer"], False)

    def test_declining_leaves_the_outcome_ok_so_the_note_is_the_only_signal(self):
        journal.start_run(["workout", "generate"], source="cli")
        with patch("builtins.input", return_value="n"):
            self.prompt.confirm("Regenerate?")
        journal.end_run("ok")
        end = [rec for rec in self.records() if rec["ev"] == "run.end"][0]
        self.assertEqual(end["d"]["outcome"], "ok")
        self.assertEqual(end["d"]["warns"], 0)
        self.assertIs(self._answers()[0]["d"]["answer"], False)

    def test_no_input_is_marked_defaulted_and_not_read_as_a_decision(self):
        """EOF is cron or a pipe, not an athlete saying no (`TtyPrompt.confirm`)."""
        journal.start_run(["workout", "generate"], source="push")
        with patch("builtins.input", side_effect=EOFError):
            self.prompt.confirm("Regenerate?", default=False)
        journal.end_run("ok")
        (rec,) = self._answers()
        self.assertIs(rec["d"]["answer"], False)
        self.assertIs(rec["d"]["defaulted"], True)

    def test_an_answered_question_carries_no_defaulted_field_at_all(self):
        journal.start_run(["workout", "generate"], source="cli")
        with patch("builtins.input", return_value="y"):
            self.prompt.confirm("Regenerate?")
        journal.end_run("ok")
        self.assertNotIn("defaulted", self._answers()[0]["d"])

    def test_the_question_is_flattened_to_one_line_and_stripped_of_colour(self):
        from trainmate.util import yellow
        journal.start_run(["workout", "generate"], source="cli")
        with patch("trainmate.util.is_color_enabled", return_value=True):
            with patch("builtins.input", return_value="y"):
                self.prompt.confirm(yellow("Proceed anyway?\nThis rebuilds  the week."))
        journal.end_run("ok")
        self.assertEqual(
            self._answers()[0]["msg"], "Proceed anyway? This rebuilds the week. → yes"
        )

    def test_a_choice_records_the_value_it_resolved_to(self):
        from trainmate.prompt import Choice
        choices = [Choice("demote", "Demote it"), Choice("keep", "Keep it")]
        journal.start_run(["data", "reflect"], source="cli")
        with patch.object(sys, "stdout", io.StringIO()):
            with patch("builtins.input", return_value="2"):
                chosen = self.prompt.choose("Apply the demotion?", choices, default="skip")
        journal.end_run("ok")
        self.assertEqual(chosen, "keep")
        self.assertEqual(self._answers()[0]["d"]["answer"], "keep")

    def test_a_cancelled_prompt_names_the_question_that_was_still_open(self):
        """`cancelled` says a run stopped; this says what it stopped on (§5.6)."""
        from trainmate.prompt import JsonPrompt, PromptCancelled
        answer = json.dumps({"v": 1, "id": "p1", "cancelled": True}) + "\n"
        prompt = JsonPrompt(out=io.StringIO(), inp=io.StringIO(answer))
        journal.start_run(["plan", "generate"], source="bot")
        with self.assertRaises(PromptCancelled):
            prompt.confirm("Apply this new periodization strategy?")
        journal.end_run("cancelled", exit_code=130)
        (rec,) = self._answers()
        self.assertEqual(rec["msg"], "Apply this new periodization strategy? → cancelled")
        self.assertIs(rec["d"]["cancelled"], True)
        self.assertNotIn("answer", rec["d"])

    def test_a_front_end_that_sends_no_answer_is_defaulted_not_a_no(self):
        from trainmate.prompt import JsonPrompt
        answer = json.dumps({"v": 1, "id": "p1"}) + "\n"
        prompt = JsonPrompt(out=io.StringIO(), inp=io.StringIO(answer))
        journal.start_run(["plan", "generate"], source="bot")
        prompt.confirm("Apply this new periodization strategy?", default=False)
        journal.end_run("ok")
        self.assertIs(self._answers()[0]["d"]["defaulted"], True)

    def test_the_answer_lands_on_the_innermost_run_not_the_shell_around_it(self):
        """A decision belongs to the command that asked, not to `tm shell` (§3)."""
        journal.start_run(["shell"], source="cli")
        inner = journal.start_run(["plan", "generate"], source="shell")
        with patch("builtins.input", return_value="y"):
            self.prompt.confirm("Apply this new periodization strategy?")
        journal.end_run("ok")
        journal.end_run("ok")
        self.assertEqual(self._answers()[0]["run"], inner)

    def test_free_text_answers_are_never_journalled(self):
        """§4.4 keeps the athlete's own words out of the log; only argv carries them."""
        journal.start_run(["data", "pull"], source="cli")
        with patch("builtins.input", return_value="hungover from the wedding"):
            self.prompt.ask_text("How did it go")
        journal.end_run("ok")
        self.assertEqual(self._answers(), [])


class TestEveryQuestionGoesThroughTheBroker(unittest.TestCase):
    """A question asked with a bare `input()` is a decision the journal never sees.

    The broker records every answer in one place (§5.6), which only holds while it is the
    only thing that asks. Keyed on the shape — a call to `input` anywhere under the
    command and coach trees — so a handler written tomorrow is covered tomorrow
    (AGENTS.md). The REPL's line reader and the Garmin MFA code sit outside both trees:
    neither is a question about the athlete's training."""

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    def test_no_command_or_coach_module_calls_input_directly(self):
        root = os.path.join(self.ROOT, "trainmate")
        offenders = []
        for tree in ("cli", "coach"):
            for folder, _dirs, files in os.walk(os.path.join(root, tree)):
                for name in sorted(files):
                    if not name.endswith(".py"):
                        continue
                    path = os.path.join(folder, name)
                    with open(path, encoding="utf-8") as handle:
                        parsed = ast.parse(handle.read(), filename=path)
                    for node in ast.walk(parsed):
                        if not isinstance(node, ast.Call):
                            continue
                        if isinstance(node.func, ast.Name) and node.func.id == "input":
                            offenders.append(f"{os.path.relpath(path, root)}:{node.lineno}")
        self.assertFalse(
            offenders,
            "these ask the athlete a question the journal cannot record — go through "
            "runtime.prompt instead: " + ", ".join(offenders),
        )


class TestReadOnlyVerbs(unittest.TestCase):
    """Every verb the listing hides has to still name a command (DESIGN_logging.md §7.1).

    The set is what `journal` filters on, and it is read at display time against a name
    the parser produced — so a command renamed or retired here leaves an entry that
    silently matches nothing, and its runs quietly come back. Keyed on the real tree, so
    a `show` added under a new group tomorrow needs no edit here."""

    def test_every_read_only_verb_names_a_command_in_the_tree(self):
        import trainmate_cli
        from trainmate.cli.argparse_ext import _subparsers_action
        from trainmate.cli.journal import READ_ONLY_VERBS

        parser, _named = trainmate_cli.build_parser()

        def names(level):
            action = _subparsers_action(level)
            if action is None:
                return set()
            found = set(action.canonical_names)
            for name in action.canonical_names:
                found |= names(action.choices[name])
            return found

        unknown = sorted(READ_ONLY_VERBS - names(parser))
        self.assertFalse(
            unknown,
            "these verbs no longer name a command, so the runs they used to hide are "
            "back in the listing: " + ", ".join(unknown),
        )


class TestNoSilentSwallows(unittest.TestCase):
    """A broad handler whose body is exactly `pass` is an accident, not a decision.

    Keyed on the shape rather than a list of names, so a file written tomorrow is covered
    tomorrow (AGENTS.md, DESIGN_logging.md §11). A handler that means to stay quiet says
    so with `journal.debug(...)`; a handler that names a narrow exception is exempt,
    because the type is the documentation of what was expected, and one whose body is a
    bare `return`/`continue` is exempt too, because the value it hands back is something
    the caller sees. `pass` is the only shape with no effect outside itself.
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    ENTRY_POINTS = ("trainmate_cli.py", "trainmate_bot.py", "trainmate_web.py")

    def _sources(self):
        for name in self.ENTRY_POINTS:
            yield os.path.join(self.ROOT, name)
        package = os.path.join(self.ROOT, "trainmate")
        for dirpath, _dirs, files in os.walk(package):
            if "__pycache__" in dirpath:
                continue
            for name in files:
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)

    def test_no_broad_handler_swallows_an_exception_in_silence(self):
        offenders = []
        for path in self._sources():
            with open(path, encoding="utf-8") as handle:
                tree = ast.parse(handle.read())
            for node in ast.walk(tree):
                if not isinstance(node, ast.ExceptHandler):
                    continue
                caught = node.type
                broad = caught is None or (
                    isinstance(caught, ast.Name)
                    and caught.id in ("Exception", "BaseException")
                )
                if not broad:
                    continue
                if len(node.body) == 1 and isinstance(node.body[0], ast.Pass):
                    offenders.append(f"{os.path.relpath(path, self.ROOT)}:{node.lineno}")
        self.assertFalse(
            offenders,
            "these handlers swallow an exception with no record of it; say so with "
            "journal.debug(...) instead of `pass`: " + ", ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
