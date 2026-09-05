"""The run journal: an operational record of what the app did, beside what it printed.

See DESIGN_logging.md. One JSON object per line in ``logs/runs/YYYY-MM-DD.jsonl``, and
one ``run.start``/``run.end`` pair bracketing every command (§3).

Two properties hold this module together, and both are load-bearing:

* **It never raises** (§4.3). A command must not die because its log could not be
  written, so every write is wrapped and a failure warns at most once per process.
* **It asks the database for nothing** (§4). The day in the file name and every ``ts``
  come from the system clock in UTC, never from ``trainmate.clock``: resolving the
  athlete's zone opens and migrates the database, which would happen on ``tm help`` and
  inside the one code path whose job is to survive the database being unreachable. The
  athlete's day comes back at display time, in ``cli/journal.py``.
"""
import json
import os
import re
import secrets
import shlex
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple

from trainmate.config import CONFIG_PATH, config

# The closed event vocabulary (§4.2). `ev` is the machine handle you filter on; `msg` is
# the sentence you read. Severity is `lvl`, not a second set of names — a warning is
# `note` at "warn" — so this list does not grow one entry per adjective.
EVENTS = (
    "run.start", "run.end", "note", "llm.call", "garmin.pull",
    "calendar.write", "db.write", "bot.event", "internal",
)

# Four levels because §5 has four sources of events and no fifth.
LEVELS = ("debug", "info", "warn", "error")
_LEVEL_RANK = {name: rank for rank, name in enumerate(LEVELS)}

# Three outcomes, because an exit code conflates three different things (§3): a domain
# refusal is not a failure, and neither is a deliberate cancel.
OUTCOMES = ("ok", "cancelled", "failed")

# One record is one line is one os.write(), so the line has to be bounded (§4.3). The
# traceback's tail gets the larger share: Python prints the innermost frames last.
MAX_RECORD_BYTES = 8192
_TRACEBACK_HEAD_BYTES = 1024
_TRACEBACK_TAIL_BYTES = 3072
_ELISION = "\n  ... [elided] ...\n"
_CUT = "…[cut]"

# The run id a record carries when there is no open run — the web app's error-only
# records (§13), and anything written before the bracket opens.
NO_RUN = "-"

_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.jsonl$")
# The one filename shape retention will delete inside the exchange directory (§10). Both
# generations of exchange name start with it; only the leading stamp is ever read.
_EXCHANGE_FILE = re.compile(r"^(\d{8})_\d{6}_")

PRUNE_STAMP = ".pruned"


@dataclass
class Run:
    """One command from parse to exit, and the rollup its ``run.end`` reports."""
    id: str
    argv: List[str]
    source: str
    parent: Optional[str]
    started: float                  # time.monotonic(), for the wall-clock duration
    command: Optional[str] = None   # canonical name, once the argv has been parsed
    seq: int = 0
    llm_calls: int = 0
    llm_tokens: int = 0
    warns: int = 0
    errors: int = 0
    # This run's own ``run.start``, built and not yet written; None once it is on
    # disk, which is the usual state (§3).
    pending: Optional[Tuple[Dict[str, Any], datetime]] = None


# The open runs, innermost last. Nesting only ever happens in `tm shell`, and every
# surface that writes is single-threaded by construction — the CLI, the REPL, the bot's
# event loop — so a plain list is enough and a contextvar is not (§3).
_stack: List[Run] = []

# A record written with no run open still needs a sequence; this is its counter.
_orphan_seq = 0

# Whether the one-per-process write failure has already been reported (§4.3).
_warned = False


# --- where it lives -------------------------------------------------------------

def runs_dir() -> str:
    """The journal directory: ``<logging.dir>/runs``."""
    return os.path.join(config.logging_dir, "runs")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(moment: datetime) -> str:
    """UTC ISO with milliseconds, Z-suffixed."""
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _day_path(moment: datetime) -> str:
    return os.path.join(runs_dir(), moment.strftime("%Y-%m-%d") + ".jsonl")


def _min_rank() -> int:
    return _LEVEL_RANK.get(config.logging_level, _LEVEL_RANK["info"])


# --- writing --------------------------------------------------------------------

def record(ev: str, msg: str = "", lvl: str = "info", **d: Any) -> None:
    """Appends one event to today's file. Never raises (§4.3).

    Keyword arguments become the record's ``d`` object; a None value is dropped, so a
    caller can pass an optional field unconditionally."""
    built = _build(ev, msg, lvl, d)
    if built is None:
        return
    _flush_pending()
    _append(*built)


def _build(
    ev: str, msg: str, lvl: str, d: Dict[str, Any]
) -> Optional[Tuple[Dict[str, Any], datetime]]:
    """The record and the file-choosing moment it belongs to, or None when its level is
    below the configured floor.

    Split out of `record` for one caller: `run.start` is built when the run opens and
    written only once the run turns out to be real (§3)."""
    if _LEVEL_RANK.get(lvl, _LEVEL_RANK["info"]) < _min_rank():
        return None
    run = current()
    if run is not None and lvl == "warn":
        run.warns += 1
    if run is not None and lvl == "error":
        run.errors += 1
    moment = _utc_now()
    rec: Dict[str, Any] = {
        "ts": _ts(moment),
        "run": run.id if run is not None else NO_RUN,
        "seq": _next_seq(run),
        "lvl": lvl,
        "ev": ev,
        "msg": msg,
    }
    fields = {k: v for k, v in d.items() if v is not None}
    if fields:
        rec["d"] = fields
    return rec, moment


def _flush_pending() -> None:
    """Writes any held-back ``run.start``, outermost first, before another record lands.

    So a deferred bracket still opens ahead of everything inside it, and `seq` — assigned
    when the record was built — still reads in order (§3)."""
    for run in _stack:
        if run.pending is not None:
            pending, run.pending = run.pending, None
            _append(*pending)


def note(msg: str, lvl: str = "info", **d: Any) -> None:
    """The subject-less event: what `step`/`warn`/`fail` write (§5.1)."""
    record("note", msg, lvl=lvl, **d)


def debug(ev: str, msg: str = "", **d: Any) -> None:
    """What must not reach the athlete but must not be forgotten either — the tier the
    ten ``except Exception: pass`` sites belong to (§2.2, §5.5).

    Off unless ``logging.level`` is ``debug``: these records are noise until the day
    they are not."""
    record(ev, msg, lvl="debug", **d)


def llm_call(
    label: str, model: str, ms: int, ok: bool,
    prompt_tokens: Optional[int] = None, completion_tokens: Optional[int] = None,
    total_tokens: Optional[int] = None, path: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """One model call, and the run rollup it feeds (§6). ``path`` is the exchange
    markdown file, which is the join from a run to its prompts."""
    run = current()
    if run is not None:
        run.llm_calls += 1
        run.llm_tokens += total_tokens or 0
    shown = f"{total_tokens:,} tok" if total_tokens else "tokens unknown"
    record(
        "llm.call", f"{label} · {model} · {shown} · {ms / 1000:.1f}s",
        lvl="info" if ok else "error",
        label=label, model=model, ms=ms, ok=ok,
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        tokens=total_tokens, file=path, error=error,
    )


def _next_seq(run: Optional[Run]) -> int:
    global _orphan_seq
    if run is None:
        _orphan_seq += 1
        return _orphan_seq - 1
    run.seq += 1
    return run.seq - 1


def _append(rec: Dict[str, Any], moment: datetime) -> None:
    """One record, one line, one ``os.write`` on an O_APPEND descriptor.

    On Linux that single call is atomic against other appenders — the offset lookup and
    the write happen under the inode lock — so three surfaces share the file with no
    lock of our own. Nothing is buffered in user space, because Python's buffered writer
    can split a long string across several write() calls, which is exactly how two
    processes interleave inside one line (§4.3)."""
    global _warned
    try:
        line = _bound(rec).encode("utf-8")
        os.makedirs(runs_dir(), exist_ok=True)
        fd = os.open(_day_path(moment), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except Exception as exc:
        if _warned:
            return
        _warned = True
        _report_write_failure(exc)


def _report_write_failure(exc: Exception) -> None:
    """One stderr line per process, gated like any other aside.

    The gate is not fussiness: the bot spawns the CLI with stderr=STDOUT and streams
    that buffer into the chat, so an ungated warning about the log file would arrive on
    the athlete's phone (§4.3)."""
    try:
        from trainmate.util import asides_enabled
        if asides_enabled():
            # Spelled out rather than `util.warn`, which would journal it: the journal
            # is what just failed (DESIGN_output_verbosity.md §3.5).
            print(f"Warning: journal write failed ({exc}).", file=sys.stderr)
    except Exception as inner:
        debug("internal", f"could not report a journal write failure: {inner}")


def _bound(rec: Dict[str, Any]) -> str:
    """The record as one line, capped at MAX_RECORD_BYTES (§4.3).

    A traceback is elided in the middle first, on its own budget; whatever still
    overflows comes off whichever string in the record is longest, until it fits."""
    fields = rec.get("d")
    if isinstance(fields, dict) and isinstance(fields.get("traceback"), str):
        fields["traceback"] = _elide(
            fields["traceback"], _TRACEBACK_HEAD_BYTES, _TRACEBACK_TAIL_BYTES
        )
    line = _encode(rec)
    while _size(line) > MAX_RECORD_BYTES:
        holder, key = _longest_string(rec)
        if holder is None:
            return _encode(_minimal(rec))
        over = _size(line) - MAX_RECORD_BYTES
        keep = max(0, _size(holder[key]) - over - _size(_CUT))
        holder[key] = _head(holder[key], keep) + _CUT
        line = _encode(rec)
    return line


def _encode(rec: Dict[str, Any]) -> str:
    """JSON escapes any newline inside a value, so one record is always one line."""
    return json.dumps(rec, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"


def _minimal(rec: Dict[str, Any]) -> Dict[str, Any]:
    """The record stripped to its spine, for the case nothing left is shrinkable."""
    return {k: rec[k] for k in ("ts", "run", "seq", "lvl", "ev") if k in rec}


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _head(text: str, limit: int) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _tail(text: str, limit: int) -> str:
    return text.encode("utf-8")[-limit:].decode("utf-8", "ignore")


def _elide(text: str, head: int, tail: int) -> str:
    """The first `head` and last `tail` bytes with a marker between."""
    if _size(text) <= head + tail:
        return text
    return _head(text, head) + _ELISION + _tail(text, tail)


def _longest_string(rec: Dict[str, Any]) -> Tuple[Optional[dict], Optional[str]]:
    """The (container, key) of the longest shrinkable string, or (None, None).

    ts/run/seq/lvl/ev are never candidates: they are the spine the reader indexes on."""
    best: Tuple[Optional[dict], Optional[str], int] = (None, None, 16)
    candidates: List[Tuple[dict, str]] = [(rec, "msg")]
    fields = rec.get("d")
    if isinstance(fields, dict):
        candidates.extend((fields, key) for key in fields)
    for holder, key in candidates:
        value = holder.get(key)
        if isinstance(value, str) and _size(value) > best[2]:
            best = (holder, key, _size(value))
    return best[0], best[1]


# --- the run bracket ------------------------------------------------------------

def current() -> Optional[Run]:
    """The innermost open run, or None."""
    return _stack[-1] if _stack else None


def current_id() -> Optional[str]:
    run = current()
    return run.id if run is not None else None


def start_run(
    argv, source: Optional[str] = None, parent: Optional[str] = None,
    defer: bool = False,
) -> str:
    """Opens a run and writes its ``run.start``. Returns the new run id.

    The parent is the run enclosing this one in this process (`tm shell` running a typed
    line), falling back to the id the spawning process passed down in
    TRAINMATE_PARENT_RUN — which is how the bot's morning push and the subprocess it
    launched read as one story (§3).

    ``defer`` holds the ``run.start`` until the parse names the run, so a line that
    never became a command can be dropped without a trace (§3). Only the CLI passes it.

    The model is deliberately absent: reading it builds the database, and the
    ``--llm-model`` override has not been applied yet. It belongs on ``llm.call``, the
    only place it is ever actually true."""
    argv = [str(a) for a in argv]
    run = Run(
        id=secrets.token_hex(4),
        argv=argv,
        source=source or os.environ.get("TRAINMATE_SOURCE") or "cli",
        parent=parent or current_id() or os.environ.get("TRAINMATE_PARENT_RUN") or None,
        started=time.monotonic(),
    )
    _stack.append(run)
    fields = dict(
        argv=argv, source=run.source, pid=os.getpid(),
        frontend=os.environ.get("TRAINMATE_FRONTEND") or "tty",
        parent=run.parent, config=CONFIG_PATH, db=config.db_path,
    )
    if defer:
        run.pending = _build("run.start", shlex.join(argv), "info", fields)
    else:
        record("run.start", shlex.join(argv), **fields)
    return run.id


def name_run(command: str, subcommand: Optional[str] = None) -> None:
    """Records which command the parsed argv turned out to be (§7.1), and writes a
    deferred ``run.start``.

    ``run.start`` keeps what the athlete typed, which may be any unambiguous prefix
    (DESIGN_cli_noargs.md §d); this is the canonical name `journal` filters and groups on.
    It lands on ``run.end`` because that is the first record written after the parse.

    A deferred ``run.start`` is written here too — the parse has succeeded and nothing has
    happened yet, which is what keeps §3's `?` row honest."""
    run = current()
    if run is None:
        return
    run.command = " ".join(word for word in (command, subcommand) if word)
    _flush_pending()


def end_run(
    outcome: str, exit_code: int = 0, error: Optional[str] = None,
    traceback_text: Optional[str] = None,
) -> None:
    """Closes the innermost run with its outcome and rollup (§3), then, once the
    outermost one is done, considers the daily retention sweep (§10)."""
    run = current()
    if run is None:
        return
    record(
        "run.end", f"{outcome} · {error}" if error else outcome,
        lvl="error" if outcome == "failed" else "info",
        outcome=outcome, exit=exit_code, cmd=run.command,
        ms=int((time.monotonic() - run.started) * 1000),
        llm_calls=run.llm_calls, tokens=run.llm_tokens,
        warns=run.warns, errors=run.errors,
        error=error, traceback=traceback_text,
    )
    _stack.pop()
    if not _stack:
        maybe_prune()


def drop_run() -> None:
    """Closes the innermost run leaving nothing behind, for a command line that never
    became a command: argparse printed usage or help and stopped (§3).

    Falls back to a normal ``run.end`` when the ``run.start`` is already on disk — a
    start without one reads as a run that was killed (§3)."""
    run = current()
    if run is None:
        return
    if run.pending is None:
        end_run("ok")
        return
    run.pending = None
    _stack.pop()
    if not _stack:
        maybe_prune()


def child_env(env: Dict[str, str], source: str) -> Dict[str, str]:
    """Stamps a subprocess's environment with this run as its parent and the source that
    describes the child (§3). Mutates and returns `env`.

    Both keys are set authoritatively: `env` is usually a copy of `os.environ`, which may
    already carry the values *this* process was launched with, and a stale parent is
    worse than none."""
    run_id = current_id()
    if run_id:
        env["TRAINMATE_PARENT_RUN"] = run_id
    else:
        env.pop("TRAINMATE_PARENT_RUN", None)
    env["TRAINMATE_SOURCE"] = source
    return env


def reset() -> None:
    """Drops every open run and the one-shot warning latch. For tests."""
    global _orphan_seq, _warned
    _stack.clear()
    _orphan_seq = 0
    _warned = False


# --- reading --------------------------------------------------------------------

def iter_records(
    start_day: Optional[date] = None, end_day: Optional[date] = None
) -> Iterator[Dict[str, Any]]:
    """Every record in the day files covering [start_day, end_day], oldest first.

    The window is widened by a day at each end: the caller filters on the athlete's
    local timestamps, and a local day straddles two UTC files (§4). A line that is not
    valid JSON is skipped — that one rule covers a torn line from a power cut, a
    truncated tail, and a file someone edited by hand (§4.3)."""
    for path in day_paths(start_day, end_day):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    rec = parse_record(line)
                    if rec is not None:
                        yield rec
        except OSError:
            continue


def today_path() -> str:
    """The day file records are landing in right now — what ``--follow`` tails."""
    return _day_path(_utc_now())


def day_paths(
    start_day: Optional[date] = None, end_day: Optional[date] = None
) -> List[str]:
    """The existing day files in the (widened) window, in date order."""
    low = start_day - timedelta(days=1) if start_day else None
    high = end_day + timedelta(days=1) if end_day else None
    found = []
    for name in _listdir(runs_dir()):
        match = _DAY_FILE.match(name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if (low and day < low) or (high and day > high):
            continue
        found.append((day, os.path.join(runs_dir(), name)))
    return [path for _, path in sorted(found)]


def llm_durations(
    label: str, model: Optional[str] = None, *, days: int = 30, limit: int = 20
) -> List[int]:
    """The durations in ms of the most recent successful ``llm.call`` records matching
    `label` (and `model`, when given), newest first.

    This is what lets a command say how long it is about to take
    (DESIGN_output_verbosity.md §8). Day files are read newest-first and the scan stops
    at `limit` samples, so the usual answer costs one file read rather than the whole
    retention window. Failed calls are skipped: a call that died on a timeout says
    nothing about how long a working one takes."""
    today = _utc_now().date()
    found: List[int] = []
    for path in reversed(day_paths(today - timedelta(days=days), today)):
        found.extend(reversed(_llm_durations_in(path, label, model)))
        if len(found) >= limit:
            break
    return found[:limit]


def _llm_durations_in(path: str, label: str, model: Optional[str]) -> List[int]:
    """The matching durations in one day file, oldest first. Unreadable file: no rows —
    an estimate is a courtesy and must never be the reason a command fails (§4.3)."""
    rows: List[int] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                rec = parse_record(line)
                if rec is None or rec.get("ev") != "llm.call":
                    continue
                d = rec.get("d") or {}
                if d.get("label") != label or not d.get("ok"):
                    continue
                if model is not None and d.get("model") != model:
                    continue
                ms = d.get("ms")
                if isinstance(ms, int) and ms > 0:
                    rows.append(ms)
    except OSError:
        return []
    return rows


def parse_record(line: str) -> Optional[Dict[str, Any]]:
    """One line as a record, or None when it is not one — see `iter_records`."""
    line = line.strip()
    if not line:
        return None
    try:
        rec = json.loads(line)
    except ValueError:
        return None
    return rec if isinstance(rec, dict) else None


def _listdir(directory: str) -> List[str]:
    try:
        return os.listdir(directory)
    except OSError:
        return []


# --- retention ------------------------------------------------------------------

def maybe_prune() -> None:
    """One sweep per UTC day at most, decided by the ``.pruned`` stamp file (§10).

    Not hung off the run that had to create today's journal file: the bot polls
    continuously, so that run is almost always the bot's own long-lived one, whose
    ``run.end`` is days away and may never come."""
    try:
        today = _utc_now().strftime("%Y-%m-%d")
        stamp = os.path.join(runs_dir(), PRUNE_STAMP)
        if _read_stamp(stamp) == today:
            return
        os.makedirs(runs_dir(), exist_ok=True)
        with open(stamp, "w", encoding="utf-8") as handle:
            handle.write(today)
        prune()
    except Exception as exc:
        debug("internal", f"retention sweep failed: {exc}")


def prune() -> Tuple[int, int]:
    """Deletes journal days and LLM exchanges past their retention, returning how many
    of each went (§10). Only files whose names carry the expected date shape are ever
    touched, which is also what makes this safe across the exchange directory's two
    filename generations."""
    today = _utc_now().date()
    days = _prune_runs(today - timedelta(days=config.logging_retain_days))
    exchanges = _prune_exchanges(
        today - timedelta(days=config.logging_retain_exchange_days)
    )
    return days, exchanges


def _prune_runs(cutoff: date) -> int:
    removed = 0
    for name in _listdir(runs_dir()):
        match = _DAY_FILE.match(name)
        if not match:
            continue
        try:
            day = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if day >= cutoff:
            continue
        removed += _unlink(os.path.join(runs_dir(), name))
    return removed


def _prune_exchanges(cutoff: date) -> int:
    directory = config.llm_logs_dir
    removed = 0
    for name in _listdir(directory):
        match = _EXCHANGE_FILE.match(name)
        if not match or not name.endswith(".md"):
            continue
        try:
            day = datetime.strptime(match.group(1), "%Y%m%d").date()
        except ValueError:
            continue
        if day >= cutoff:
            continue
        removed += _unlink(os.path.join(directory, name))
    return removed


def _unlink(path: str) -> int:
    try:
        os.remove(path)
        return 1
    except OSError:
        return 0


def _read_stamp(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        return ""
