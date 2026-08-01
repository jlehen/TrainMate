# CLI no-argument behavior

## The problem

Running a command with no arguments does different things depending on the
command: some act (`workout list`), some preview-then-confirm (`workout adapt`),
and some refuse because they need a target (`workout swap`). Each is defensible
alone, but the mix is unpredictable until learned, and the "refuse" case used to
answer with argparse's bare usage block, which names the missing argument
without saying what belongs in it.

## The convention

A bare command may **act** if — and only if — it can pick a sensible default
target AND it won't write anything without showing you first. That splits every
command into three predictable buckets:

| Bucket | No-args behavior | Examples |
| --- | --- | --- |
| Read-only | Just act | `list`, `show`, `status` |
| Mutating, preview-then-confirm | Act on a sensible default, show the preview, gate the write behind a confirm | `workout adapt` (defaults to today), `plan generate` (defaults to the nearest goal) |
| Mutating, immediate / no natural default | Print the command's help, then the line naming what's missing | `workout swap`, `goal add`, `constraint add` |

The real axis is not "does it mutate?" but "does a bare run do something
irreversible, or produce a reversible preview?" The confirm gate is what makes a
defaulting mutator safe, so those commands act rather than refuse.

## §a — A missing argument prints the command's help, then the missing line

`WrapAwareArgumentParser.error` (trainmate/cli/argparse_ext.py) intercepts the
argparse "the following arguments are required: …" message and answers it with
the command's own `-h` output, followed by that line — last, where the eye lands
after a block of text and where the shell prompt puts it next to what you type.

Pointing at `-h` was enough while the mandatory fields were named flags: the
message said `--reason` and that *was* the answer. Now that mandatory means
positional (§a2), the same message names a slot — `target2`, `reason` — without
saying what belongs in it or in what order, so it raises a question the help
already answers. Printing it beats making the athlete re-type the command to get
it, and the case is narrow enough not to be noisy: it fires only when arguments
are missing outright.

Every other error (bad choice, bad value) keeps argparse's short usage block —
there the shape is already known and only one token is wrong. Both go to stderr,
which the Telegram front-end merges into stdout, so the athlete sees the same
thing the terminal does. The override lives on the root parser class, which
argparse reuses for every subparser, so it applies at every command level.

## §a2 — Mandatory arguments are positional, optional ones are flags

A required `--flag` is a contradiction the athlete pays for twice: it is longer to
type on the one path that always has to be taken, and it makes the usage line lie
about which arguments are the command's subject. So the rule is mechanical:

**If a command cannot run without it, it is a positional. If it can, it is a flag.**

`goal add TITLE DATE SPORT…`, `constraint add TITLE`, `context add METRIC [TEXT…]`,
`learnings edit ID TEXT`, `workout add DATE SPORT TITLE`, `workout rm ID REASON`,
`workout swap TARGET1 TARGET2 REASON`, `benchmark record SPORT --<anchor> VALUE`.
Everything those commands can do without stays a flag, `--desc` and `--priority`
included. The same field is a positional where it is mandatory and a flag where it
is not: `add` takes `TITLE` positionally, `edit` takes `--title` because an edit
that only moves a date must not have to restate the title.

Two shapes are deliberately *not* covered, because the mandatory thing is a choice
among flags rather than a value: `benchmark record`'s anchor flags (`--ftp 250`,
`--lthr 165` — the flag names the metric, so exactly one is required and the value
rides along) and `plan feedback`'s `--macro | --meso ID`. Collapsing either into
positionals would trade a self-documenting `-h` listing for argument order the
athlete has to remember.

Enforcement is argparse's, not the handlers': a missing positional is reported by
§a before any handler runs, so no command re-checks for its own mandatory input,
and none prompts for one either — a prompt is for a decision (§b's confirms), never
for an argument the invocation should have carried.

## §b — Preview-then-confirm commands name the defaulted target

When `workout adapt` falls back to today, or `plan generate` falls back to the
nearest active goal, it echoes the chosen target up front (dimmed) so a bare run
is never silent about what it decided to operate on. The write still happens only
after the existing confirm prompt.

## §c — Help lists commands by usefulness, not registration order

argparse renders sub-commands in the order they are registered, which is an
authoring artifact, not a use-frequency ranking. So every help surface — the
native `-h`/`--help` listing, the recursive `help` tree, and the `{...}` usage
metavar — used to lead with whatever happened to be added first (e.g. `help`,
`shell`) rather than the commands an athlete reaches for daily (`status`,
`workout`).

`COMMAND_ORDER` (trainmate_cli.py) is the single source of truth: one list per
command level, keyed by the parent's canonical name (`""` for the top level),
each listing that level's *visible* sub-commands most-useful first. After the
tree is assembled, `sort_command_tree` (trainmate/cli/argparse_ext.py) walks it
once and reorders each sub-parsers action's `_choices_actions` (the listing +
`help` tree) and `_visible_names` (the usage metavar) to match, keeping each
command's aliases grouped with it. Names absent from the list sort stably to the
end; hidden `advanced=True` commands are untouched (they carry no choice action
and appear only under `--all`, appended after the visible set). Ranking is
editorial — reorder the lists to change what leads.

## §d — Any unambiguous command prefix is that command

### The problem

Every command used to carry a hand-picked shorthand: `w` for `workout`, `l` for
`list`, `cons` for `constraint`, `rec` for `record`. Two costs. For the athlete,
the shorthand is a second vocabulary to memorize, and it is arbitrary — `res` for
`restore` but `rb` for `rollback`; `l` means `learnings` at the top level and
`list` everywhere below. For the code, each shorthand was written twice — once in
`add_parser(aliases=[…])` and once in the dispatcher's `sub in ("restore", "res")`
— so the two could drift.

### The rule

**A token that is a prefix of exactly one command at its level *is* that command.**
`st` is `status`, `wo li` is `workout list`, `constr ed` is `constraint edit`. No
registration, no list to maintain: the command names themselves are the vocabulary.
An ambiguous prefix is refused by name — `c` prints `Ambiguous command 'c' —
matches: constraint, context` and exits 2, the same shape as argparse's own errors.
Hidden `advanced=True` commands take part in matching, since they dispatch like any
other.

That leaves exactly two reasons for an explicit alias to survive:

* it is **not a prefix** of its command — `ctx`, `lm`, `df`, `rb`, `sm`, `sa`, `use`;
* it **picks the winner** among an ambiguous prefix — `s` is `status` (not `shell`),
  `workout a` is `adapt` (not `add`), `context l` is `list` (not `list-metrics`),
  `data b` is `bootstrap` (not `backfill-tss`), `workout p` is `push` (not
  `prune-calendar`).

`rm` gets no winner: `r` is left ambiguous (with `restore`/`rollback` under `workout`,
with `record` under `benchmark`) rather than aliased. A one-letter shortcut for the
destructive command is worth less than the two characters it saves, and `rm` is
already the full name.

Everything else was deleted. Almost nothing an athlete could type before stopped
working: each retired alias is a prefix of its own command, which is what makes it
retired. The two deliberate exceptions both concern a destructive command:

* `plan d` used to delete a plan and now resolves to `plan diff` — an exact alias
  beating the natural prefix of a visible everyday command is the trap this rule
  exists to remove, and the safe direction to fail in;
* `workout r` / `benchmark r` used to remove; both now report the ambiguity instead.

### Where it lives

`_resolve_subcommand` (trainmate/cli/argparse_ext.py) resolves one token against
one level: exact canonical name, then exact alias, then unique prefix. The
sub-parsers action records `canonical_names` and `alias_of` as commands register,
so prefixes only ever match *canonical* names — which is precisely why an alias
still means what it says.

`translate_dashless_argv` calls it twice per token, deliberately: **exact** matches
resolve before dashless option keywords are considered, **prefix** matches only
after. So no spelling that meant something before means something else now;
prefixes fill in the gaps.

Three invariants keep that namespace honest as commands and options are added;
`TestCommandTreeInvariants` (tests/test_cli_dashless.py) walks the real tree and
pins each, because every violation fails silently — the wrong thing simply resolves:

* no dashless option keyword may also name or abbreviate a command at its level
  (today only the root has both options and commands, but a group-level option
  added later is checked the same way);
* no alias may shadow a *different* command's otherwise-unambiguous prefix — this
  is the `plan d` trap, stated as a rule;
* no alias may be redundant with prefix matching, so the retired shortcuts cannot
  creep back one at a time.

Because resolution emits the **canonical** name, argparse — and therefore
`args.command` / `args.subcommand` — never sees an alias or a prefix. The
dispatcher in `trainmate_cli.py` compares one canonical name per branch, so a
shorthand is now defined in exactly one place: the `aliases=` list.
