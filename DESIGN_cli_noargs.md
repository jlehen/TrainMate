# CLI no-argument behavior

## The problem

Running a command with no arguments does different things depending on the
command: some act (`workout list`), some preview-then-confirm (`workout adapt`),
and some refuse because they need a target (`workout swap`). Each is defensible
alone, but the mix is unpredictable until learned, and the "refuse" case used to
bury the one useful line under argparse's full usage block.

## The convention

A bare command may **act** if — and only if — it can pick a sensible default
target AND it won't write anything without showing you first. That splits every
command into three predictable buckets:

| Bucket | No-args behavior | Examples |
| --- | --- | --- |
| Read-only | Just act | `list`, `show`, `status` |
| Mutating, preview-then-confirm | Act on a sensible default, show the preview, gate the write behind a confirm | `workout adapt` (defaults to today), `plan generate` (defaults to the nearest goal) |
| Mutating, immediate / no natural default | Print one line naming what's missing | `swap`, `goal add`, `constraint set` |

The real axis is not "does it mutate?" but "does a bare run do something
irreversible, or produce a reversible preview?" The confirm gate is what makes a
defaulting mutator safe, so those commands act rather than refuse.

## §a — Missing-required-argument errors lead with the missing line

`WrapAwareArgumentParser.error` (trainmate/cli/argparse_ext.py) intercepts the
argparse "the following arguments are required: …" message and prints just that
line plus a `-h` pointer, instead of the multi-line usage block argparse would
otherwise print first. Every other error (bad choice, bad value) keeps the full
usage block. The override lives on the root parser class, which argparse reuses
for every subparser, so it applies at every command level.

## §b — Preview-then-confirm commands name the defaulted target

When `workout adapt` falls back to today, or `plan generate` falls back to the
nearest active goal, it echoes the chosen target up front (dimmed) so a bare run
is never silent about what it decided to operate on. The write still happens only
after the existing confirm prompt.
