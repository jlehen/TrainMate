# TrainMate model comparison — 2026-08-16

> Run on 2026-08-16/17 against the author's real install. This is a snapshot of
> the experiment's report, kept in the repo for reference: the run directories it
> mentions (`out/<model>.txt`, `runs/`, the driver scripts) live in the private
> experiment tree, not here. Location and identity details are fictionalized for
> publication (matching `config.sample.yaml`); every measured number is as run.

One isolated TrainMate install per model from `llm.models` in `config.yaml`, each with its
own database, so the fifteen plans are directly comparable.

`openai/gpt-5.6-sol`, `openai/gpt-5.6-sol-pro` and `openai/gpt-5.6-terra-pro` were added
after the first twelve and run separately. They are directly comparable anyway: the run
copy's `today_date()` is pinned to 2026-08-16, so every model plans from the same Sunday.
Without that pin the later runs would have started on a Monday and every weekly column in
this README would be measuring a different thing.

## What to read

`out/<model>.txt` — one file per model, nine labelled sections:

| Section | Content |
|---|---|
| 0 | Goals in the database (`goal list`) |
| 1 | `data bootstrap` — history reconstruction, mesocycle inference, coach learnings |
| 2 | `plan generate` as printed while generating (the coach's reasoning) |
| 3 | The resulting plan (`plan show -g 1 -w`) |
| 4 | `workout generate` as printed while generating (no test constraints present) |
| 5 | The resulting workouts (`workout list -g 1 -v`) — the pre-adapt baseline |
| 6 | The two constraints added afterwards, and the full constraint list |
| 7 | `workout adapt` as printed while adapting |
| 8 | The workouts after adaptation (`workout list -g 1 -v`) |
| — | Run log: per-command exit code and wall time |

Sections 4–8 were produced in one continuous pass per model, on top of that model's own
plan from sections 1–3. The plan is **not** regenerated, so each model adapts the
periodization it designed itself. Diff section 5 against section 8 to see exactly what the
adaptation moved.

Two analyses follow below: *The plans and workouts compared* (sections 2–5) and *Reading
sections 7 and 8* (the adaptation). Both are measured from the output files. The first ends
with *The gpt-5.6 family, side by side*, which separates that family's house style from
run-to-run variation now that five of its variants are in the set.

`runs/<model>/logs/llm_exchanges/*.md` holds the full prompt and raw response for each of
the four LLM calls (bootstrap, plan, workouts, adapt), if you want to compare inputs as
well as outputs.

## How each run was built

1. A private copy of the code in `runs/<model>/`, so `config.yaml`, `trainmate.db`,
   `science/` and `logs/` all resolve inside the run directory. The code is copied rather
   than symlinked — Python resolves a symlinked entry script back to its real directory,
   which would put the database back in the real repo.
2. A fresh database, migrated by the app itself, then seeded from the real database with
   the athlete inputs only: `athlete_metrics_cache`, `athlete_baselines`,
   `completed_activities`, `daily_context`, `benchmark_results`, `constraints`, plus the
   Garmin/Calendar sync watermarks so nothing ever pulls. Not copied: the plan and workout
   tables, `coach_learnings`, `learning_evidence` and `analysis_cache` — those are what
   `data bootstrap` is meant to produce.
3. Both goals re-added through `goal add`, read from the real database so the wording,
   dates, priorities and `date_type` are exact. Goal 1 is the Eastbridge–Hillcrest climb.
4. `data bootstrap --auto --force --no-pull`
5. `plan generate -y -g 1 --no-pull`
6. `workout generate -f -g 1 --no-pull` (horizon = goal 1's target date, 2026-09-30) —
   run with **no test constraints in the database**, so the schedule knows nothing about
   the group ride or the wedding
7. `constraint add "Group ride" --start 2026-08-30`, described as a ride with friends whose
   intensity is hard to control (leaning on the competitiveness already in `config.yaml`)
8. `constraint add "Wedding" --start 2026-09-11 --end 2026-09-13`, described as three days
   with more than normal alcohol consumption
9. `workout adapt -y --no-pull` — adapting on 2026-08-16

Steps 6–9 are the actual test of `workout adapt`: the coach plans in ignorance of both
events, then has to discover and absorb them at adapt time. Sections 4–8 were regenerated
in one pass for exactly this reason — an earlier attempt adapted a schedule that had
already been adapted once, which measured the wrong thing.

Both constraints were left advisory. `constraint add` proposes a plan-shaping replan when a
directive is large enough; the prompt was declined in every run, so no plan was regenerated
and the constraints are honoured by `workout adapt` alone.

Every command ran with `--llm-model <model>`, so the model is the only variable.

## The plans and workouts compared (sections 2–5)

Every model was handed the same brief: a ~13:30 maximal climb on 2026-09-30 (indicative,
not a race), 6.5 weeks to work with, CTL 43.4 after a summer of on-off weeks, an
8 h/week target inside tight daily windows (Mon–Thu 1.3 h and one session, Fri 2 h,
Sat/Sun 3 h), a 220 W FTP anchor nobody has verified, a standing 2 strength sessions/week
floor, and a documented habit of riding easy days too hard. What follows is what each
model did with that, measured from the files rather than judged by eye.

### 1. Block order — the one real disagreement

`sustainable_training.txt` §3 says blocks run general → specific: whichever block most
resembles the event goes last. The models split on what a 13-minute all-out climb actually
*is*.

| Model | Blocks (in order) | Last block | Plan runs to |
|---|---|---|---|
| anthropic/claude-opus-4.8 | SIT primer 2w → HIIT 3w → Threshold finish 1.6w | Threshold | 09-30 |
| anthropic/claude-opus-5 | Threshold 3w → bridge deload 1w → specific HIIT 2.6w | HIIT | 09-30 |
| anthropic/claude-sonnet-5 | SIT reactivation 1w → Threshold 3w → HIIT 2.6w | HIIT | 09-30 |
| deepseek/deepseek-v4-pro | HIIT 2w → HIIT recovery 1w → Threshold 3.4w | Threshold | 09-30 |
| google/gemini-3.1-pro-preview | HIIT 3.1w → Threshold 3.4w | Threshold | 09-30 |
| google/gemini-3.7-flash | HIIT 3.1w → Threshold 3.4w | Threshold | 09-30 |
| moonshotai/kimi-k3 | HIIT 3w → Threshold 3.6w | Threshold | 09-30 |
| openai/gpt-5.6-sol | Threshold 3w → consolidation deload 1w → climb-specific HIIT 2.6w | HIIT | 09-30 |
| openai/gpt-5.6-sol-pro | Threshold 3w → consolidation 1w → climb-specific HIIT 2.6w | HIIT | 09-30 |
| openai/gpt-5.6-terra | SIT 1.4w → Threshold 3w → specific VO2max 2.1w | VO2max | 09-30 |
| openai/gpt-5.6-terra-pro | HIIT 2w → Threshold 3w → climb-specific HIIT 1.6w | HIIT | 09-30 |
| openai/gpt-5.6 | Threshold 3w → HIIT 2w → HIIT absorption 1w | absorption | **09-26** |
| qwen/qwen3.8-max | SIT 1.1w → HIIT 2w → HIIT taper 1w → Threshold 2.4w | Threshold | 09-30 |
| x-ai/grok-4.5 | SIT 2w → HIIT 2w → Threshold 2.6w | Threshold | 09-30 |
| z-ai/glm-5.2 | HIIT 2w → Threshold 3w → climb-specific consolidation 1.6w | mixed | 09-30 |

**Seven put Threshold last**, reading the climb as a hill time trial — which is exactly the
`Time Trial / Triathlon: SIT → HIIT → Threshold` ordering in the science file. grok-4.5 and
qwen3.8-max reproduce that template literally, SIT block included.

**Seven put HIIT/VO2max last**, reading a 13:30 effort as severe-domain work above maximum
steady state rather than at it. All three later gpt-5.6 variants land here. sol and sol-pro
build the identical skeleton — a 3-week Threshold block, a 1-week bridge, then a
climb-specific HIIT block — which is opus-5's structure arrived at independently;
terra-pro opens with HIIT instead and returns to it at the end.
Only opus-5 argues the case explicitly, though: it names the
divergence, justifies dropping the SIT block on residual grounds (maximal power decays in
~10 days and contributes nothing to a 13-minute climb), and keeps the neuromuscular quality
alive with 20-second accelerations inside endurance rides instead. The other three assert
the ordering without defending it.

Both readings are defensible, so this is the axis where your own judgement matters most.
What separates the field is whether the model *knew* it was making a choice.

One outright defect: **gpt-5.6's macrocycle ends 09-26**, four days before the goal it was
planning for.

### 2. Load: how much, and does it undulate

Weekly TSS across the six full weeks (Mon-start), then the mean weekly hours against the
8.0 h target:

| Model | 08-17 | 08-24 | 08-31 | 09-07 | 09-14 | 09-21 | h/wk | Deload |
|---|---|---|---|---|---|---|---|---|
| anthropic/claude-opus-4.8 | 363 | 271 | 319 | 343 | 250 | 324 | 7.2 | irregular dips |
| anthropic/claude-opus-5 | 381 | 422 | 418 | **246** | 385 | 367 | 8.0 | wk 4, −41% |
| anthropic/claude-sonnet-5 | 255 | 378 | 399 | 380 | 352 | 313 | 6.8 | none |
| deepseek/deepseek-v4-pro | 395 | 410 | **213** | 415 | 415 | 280 | 6.7 | wk 3, −48% |
| google/gemini-3.1-pro-preview | 350 | 380 | 330 | 375 | 385 | 325 | 7.6 | none (±8%) |
| google/gemini-3.7-flash | 359 | 376 | **225** | 367 | 378 | 301 | 7.5 | wk 3, −40% |
| moonshotai/kimi-k3 | 384 | 374 | **205** | 354 | 383 | 325 | 7.1 | wk 3, −45% |
| openai/gpt-5.6-sol | 369 | 373 | 315 | **206** | 376 | 306 | 6.8 | wk 4, −45% |
| openai/gpt-5.6-sol-pro | 365 | 387 | 345 | 282 | 367 | 308 | 7.5 | wk 4, −27% |
| openai/gpt-5.6-terra | 370 | 400 | 430 | 405 | 418 | 401 | 9.0 | none |
| openai/gpt-5.6-terra-pro | 360 | 369 | 380 | 390 | 344 | 373 | 8.2 | none (±7%) |
| openai/gpt-5.6 | 389 | 365 | 345 | 365 | **198** | 254 | 7.0 | wk 5, never recovers |
| qwen/qwen3.8-max | 435 | 400 | 438 | **193** | 388 | 388 | 8.1 | wk 4, −56% |
| x-ai/grok-4.5 | 337 | 289 | 367 | 319 | 359 | 380 | 8.1 | none |
| z-ai/glm-5.2 | 340 | 340 | 350 | 355 | **240** | 315 | 7.6 | wk 5, −32% |

Eight models build a genuine loading/deload rhythm — opus-5, deepseek, gemini-3.7-flash,
kimi-k3, qwen3.8-max and gpt-5.6-sol cut 40–56% in the middle of the build, gpt-5.6-sol-pro
cuts a shallower 27%, and glm-5.2 cuts 32% but leaves it until week 5. opus-4.8 dips twice
without a pattern, and gpt-5.6 drops in week 5 and never comes back up. The remaining five
ride an essentially flat line for six weeks — gpt-5.6-terra never drops below 370 TSS and
averages 9.0 h against a stated 8.0 h budget, which is the most aggressive plan in the set
for an athlete whose actual problem is not being able to string good weeks together.
gpt-5.6-terra-pro is flatter still (±7%) but pitched a sane 8.2 h.

At the other end, deepseek (6.7), sonnet-5 (6.8) and gpt-5.6-sol (6.8) plan about 15% under
the athlete's own target. Given that CTL erosion from under-filled weeks is the diagnosis,
planning short is its own kind of miss.

(The h/wk column counts every scheduled minute in the six full weeks, divided by six. Two
figures here were previously misreported — opus-4.8 as 6.8 and deepseek as 6.6 — and are
corrected above.)

Structured hard cycling stays inside the §4 cap of 0–2 per week for all fifteen.
gemini-3.7-flash sits at the bottom of that range — 1 quality session in most weeks, 0 in
one — which for a six-week build is arguably under-dosed rather than conservative.

### 3. The FTP anchor: when do you test the number everything scales off?

220 W is unverified, and every threshold target is a percentage of it.

| Model | First test | Tests | Gaps |
|---|---|---|---|
| anthropic/claude-opus-5 | 08-18 (day 3) | 1 | — |
| qwen/qwen3.8-max | 08-19 (day 4) | 1 | — |
| openai/gpt-5.6-terra | 08-25 | 2 | 21 d |
| deepseek/deepseek-v4-pro | 08-26 | 2 | 33 d |
| openai/gpt-5.6-terra-pro | 08-26 | 3 | 23, **11 d** |
| x-ai/grok-4.5 | 08-28 | 3 | 14, 17 d |
| z-ai/glm-5.2 | 08-28 | 2 | 21 d |
| anthropic/claude-opus-4.8 | 08-29 | 1 | — |
| google/gemini-3.1-pro-preview | 09-02 | 2 | 24 d |
| openai/gpt-5.6 | 09-04 | **4** | **14, 6, 5 d** |
| google/gemini-3.7-flash | 09-05 | 2 | 21 d |
| openai/gpt-5.6-sol-pro | 09-05 (day 21) | 2 | 24 d |
| openai/gpt-5.6-sol | 09-05 | 3 | **7**, 17 d |
| anthropic/claude-sonnet-5 | 09-08 (day 24) | 1 | — |
| moonshotai/kimi-k3 | 09-08 | 2 | 21 d |

Two models test in the first four days, while TSB is still positive, and recompute zones
before prescribing anything off them. Seven wait two to three and a half weeks, which means
they prescribe "195 W, ~89% FTP" sessions for weeks against a number they have already
flagged as unreliable — sonnet-5 is the extreme, running 24 days of watt-precise threshold
work before testing.

**gpt-5.6 tests four times with 6- and 5-day gaps**, against `benchmarks.txt` §1's 3–4 week
minimum, and spends the last two weeks of its specific block testing rather than training —
which is why its week of 09-14 collapses to 198 TSS. Two of the newer siblings repeat the
error on a smaller scale: gpt-5.6-sol re-tests after **7 days** and gpt-5.6-terra-pro after
11, both well inside the minimum. grok-4.5's three tests at 14/17-day spacing is a milder
version again. Retesting that fast cannot separate real adaptation from day-to-day noise,
so the zones it produces are no more trustworthy than the anchor it replaced.

### 4. Does the plan actually arrive at the goal?

The goal is a timed climb attempt. Six models schedule one; nine do not.

| Model | Climb attempt | Run-in to 09-30 |
|---|---|---|
| anthropic/claude-opus-5 | 09-27, tagged `[BENCHMARK]` | rehearsal + easy spin 09-29 explicitly "attempt day tomorrow" |
| qwen/qwen3.8-max | **09-30**, the goal date itself | only model to schedule on the target day |
| z-ai/glm-5.2 | 09-26 practice on the real climb | rest 09-29 "fresh for climb attempt" |
| google/gemini-3.1-pro-preview | 09-29 PR attempt | rest 09-28 |
| moonshotai/kimi-k3 | 09-26, conditions permitting | but an FTP test on 09-29 |
| anthropic/claude-sonnet-5 | 09-26 | but 3x6 min HIIT on 09-29 |
| anthropic/claude-opus-4.8 | none | openers 09-28, light strength 09-29 — an unnamed run-in |
| deepseek/deepseek-v4-pro | none | FTP test 09-28, rest 09-29 — arrives fresh |
| x-ai/grok-4.5 | none | FTP test 09-28, strength 09-29 |
| openai/gpt-5.6-terra | none | hard HIIT 09-27, easy 09-29 |
| openai/gpt-5.6 | none | FTP test 09-29 (RPE 9), the day before the goal |
| openai/gpt-5.6-sol | none | **20-min FTP test 09-29, RPE 10** |
| openai/gpt-5.6-sol-pro | none | **20-min FTP test 09-29, RPE 10** |
| openai/gpt-5.6-terra-pro | none | **FTP ramp test 09-29, RPE 10** |
| google/gemini-3.7-flash | none | ordinary endurance week, goal not referenced |

opus-5 is the only one that treats the attempt as a repeatable benchmark with named
fallback days, writes the Lake Arden lead-in into the session, and still leaves 09-30 open.

The sharpest finding in this section is what sits on **09-29**. Six models put a hard
session the day before the target: sonnet-5 a 3x6 HIIT, and kimi-k3, gpt-5.6, gpt-5.6-sol,
gpt-5.6-sol-pro and gpt-5.6-terra-pro an all-out FTP test — RPE 10 in four of the five.
None of those six has scheduled a climb attempt, so the plan's final act is a maximal
20-minute effort on the eve of the thing it was built for. (gemini-3.1-pro-preview also
carries an RPE 10 on 09-29, but that session *is* its climb attempt, which is the opposite
mistake to make.) Whatever freshness the six weeks accumulated is spent the day before it
was needed, and the athlete's own goal text — "no need for a big taper" — asks for a light
touch on the run-in, not none at all.

### 5. Interval progression

The titled interval sessions in order, which shows whether the build is actually a build:

| Model | Progression |
|---|---|
| google/gemini-3.1-pro-preview | 4x4 → 4x5 → 3x6 → 4x6 → 3x8 → 3x10 → 2x15 → 4x10 → 3x15 → 2x20 |
| anthropic/claude-opus-5 | 4x10 → 3x15 → 2x20 → 2x20 → 3x20 ‖ 3x4 → 4x5 → 3x6 → 2x12 |
| anthropic/claude-opus-4.8 | 4x4 → 4x5 → 3x6 → 3x5 ‖ 4x10 → 3x15 → 2x20 |
| anthropic/claude-sonnet-5 | 4x10 → 4x10 → 3x15 → 3x15 → 2x20 ‖ 4x5 → 4x5 → 4x6 → 3x6 |
| moonshotai/kimi-k3 | 4x4 → 4x4 → 4x5 → 3x6 → 3x4 ‖ 3x15 → 2x20 → 2x22 |
| openai/gpt-5.6-terra | 4x10 → 3x15 → 2x20 → 3x15 → 2x20 ‖ 4x4 → 4x5 |
| openai/gpt-5.6 | 4x10 → 2x20 → 3x15 → 3x15 → 2x20 ‖ 4x5 → 3x6 |
| openai/gpt-5.6-sol | 4x10 → 3x15 → 3x15 → 2x20 → 3x20 ‖ 4x4 → 4x5 → 4x5 → 3x6 |
| openai/gpt-5.6-sol-pro | 4x10 → 2x20 → 3x15 → 2x22 → 2x25 ‖ 4x4 → 3x5 |
| openai/gpt-5.6-terra-pro | 4x4 → 4x5 ‖ 4x10 → 3x15 → 3x15 → 2x20 ‖ 3x6 → 3x5 |
| z-ai/glm-5.2 | 4x4 → 4x5 → 4x10 → 3x15 → 3x6 → 2x3 |
| deepseek/deepseek-v4-pro | 4x5 → 4x5 → 4x5 ‖ 2x20 → 3x15 → 2x20 → 3x15 → 2x20 |
| x-ai/grok-4.5 | 4x4 → 4x5 → 3x12 → 2x20 |
| google/gemini-3.7-flash | 4x4 → 4x5 → 3x12 → 2x20 |

(‖ marks the block change. qwen3.8-max keeps interval structure in the description rather
than the title, so it does not appear here.)

gemini-3.1-pro-preview has the cleanest ramp in the set: ten quality sessions, interval
duration climbing steadily through the HIIT block and again through the Threshold block,
and its first five steps reproduce §7's worked progression (4x4 → 4x5 → 3x6 → 4x6 → 3x8)
exactly. opus-5 converges deliberately on the event — its last HIIT session is 2x12–14 min
at 100–108% FTP on the actual gradient, ridden as pacing rehearsal, which is as close to
the goal as a training session gets. At the other end, grok-4.5 and gemini-3.7-flash
schedule **four** titled interval
sessions in six weeks, and deepseek repeats 4x5 three times before jumping straight to
2x20. gpt-5.6-terra and gpt-5.6 run their threshold progression, then *restart* at 4x4 in
the final block, so intensity rises as duration collapses right before the goal.

That restart is the family's signature, and the three new variants all repeat it — sol and
sol-pro drop back to 4x4 and terra-pro to 3x6 once the climb-specific block opens. Two make
it worse by then going *backwards* inside that block: terra-pro runs 3x6 → 3x5 and sol-pro
4x4 → 3x5, so the last quality session before the goal is the smallest of the block. The
one clear improvement over the older siblings is sol-pro's threshold ramp, 4x10 → 2x20 →
3x15 → 2x22 → 2x25, which extends past the science file's 3x20 endpoint to genuine
climb-length intervals. sol-pro also schedules only two quality sessions in its whole
2.6-week specific block, which is thin for the phase meant to be the most event-like.

### 6. Following the athlete's own rules

| Model | Days over daily time budget | Weeks under the 2× strength floor | Yoga | Kettlebell | Handles the 200 m hill home |
|---|---|---|---|---|---|
| anthropic/claude-opus-5 | 0 | 0 | 4 | yes | **7 mentions** |
| anthropic/claude-sonnet-5 | 0 | 0 | 0 | yes | no |
| deepseek/deepseek-v4-pro | 0 | 0 | 1 | **no** | no |
| google/gemini-3.1-pro-preview | 0 | 0 | 0 | yes | 1 |
| openai/gpt-5.6 | 0 | 0 | 0 | rarely | no |
| openai/gpt-5.6-sol | 0 | 0 | 0 | yes | no |
| openai/gpt-5.6-sol-pro | 0 | 0 | 0 | yes | 1 |
| openai/gpt-5.6-terra-pro | 0 | 0 | 0 | yes | 4 |
| moonshotai/kimi-k3 | 0 | 0 | 0 | yes | 2 |
| google/gemini-3.7-flash | 0 | 1 | 0 | **no** | 3 |
| qwen/qwen3.8-max | **5** (worst +52 min) | 1 | 5 | yes | no |
| z-ai/glm-5.2 | 0 | 1 | 0 | yes | no |
| anthropic/claude-opus-4.8 | 0 | **2** | 5 | yes | 2 |
| openai/gpt-5.6-terra | 3 (all +2 min) | **2** | 0 | yes | 1 |
| x-ai/grok-4.5 | 0 | **2** | 1 | rarely | no |

The daily windows are respected almost universally; qwen3.8-max's only real overrun is the
09-30 climb attempt itself (130 min in a 78-min Wednesday slot), which is arguably the one
day worth breaking the rule for. The strength floor is the more revealing test, because it
is stated as a year-round longevity priority rather than a training preference: opus-4.8,
gpt-5.6-terra and grok-4.5 quietly drop to one session in two separate weeks.

Two smaller tells. **gpt-5.6 never uses the word "outdoor" in any of its 45 session
descriptions** — it appears only in the plan prose — despite the athlete asking for outdoor
weekend rides; every session reads as equipment-agnostic. gpt-5.6-sol and gpt-5.6-sol-pro
do exactly the same thing, so all three "sol"-and-base variants are silent on where the
ride happens, while gpt-5.6-terra and terra-pro say "outdoor" 9 and 15 times. Whatever
distinguishes the terra variants, it shows up here. And the 200 m hill the athlete
lives on top of, which turns every ride home into an unplanned Z3 finish, is addressed
repeatedly only by opus-5 (ride it at endurance power, explicit ≤165 W cap on filler rides)
— which is precisely the "I consistently over-do light workouts" problem in the profile.

Event specificity varies just as much. The goal describes ~1 h of Z2 around Lake Arden
before the climb, so rehearsing that lead-in is free specificity. glm-5.2 uses it most (8
references, riding the actual Lake Arden→Eastbridge route in practice sessions), followed by
opus-5, kimi-k3, gemini-3.7-flash and qwen3.8-max at 4 each.

The gpt-5.6 family is the outlier, and it is stark. Counting how often a session names the
actual objective — Hillcrest or Eastbridge — across all fifteen:

| Model | Names the climb, in sessions |
|---|---|
| z-ai/glm-5.2 | 7 |
| moonshotai/kimi-k3, anthropic/claude-sonnet-5 | 4 |
| opus-5, gemini-3.7-flash, qwen3.8-max, grok-4.5 | 3 |
| google/gemini-3.1-pro-preview | 2 |
| deepseek/deepseek-v4-pro, openai/gpt-5.6 | 1 |
| **gpt-5.6-terra, gpt-5.6-terra-pro, gpt-5.6-sol, gpt-5.6-sol-pro**, opus-4.8 | **0** |

Four of the five gpt-5.6 variants never name the climb in a single session, and the fifth
manages one passing reference — a clean family trait rather than one bad run. opus-4.8 and
deepseek land in the same place from outside the family. In terra-pro's 51 sessions the
words Hillcrest, Eastbridge, Lake Arden and "lake" do not appear once: a competent generic
threshold-and-HIIT build, pointed at nothing in particular. That matters more than it
sounds — a 4 km/200 m climb has a specific gradient and a specific 13:31 to beat, and none
of that reaches the sessions the athlete would actually read.

### 7. How much prescription you actually get

Average words per workout description, which tracks how executable a session is:

| Model | Words/session | Character |
|---|---|---|
| anthropic/claude-opus-5 | 82 | full protocol, watts, contingencies, why |
| z-ai/glm-5.2 | 59 | structured, watts, progression noted |
| google/gemini-3.7-flash | 49 | bulleted sets with watt ranges |
| moonshotai/kimi-k3 | 44 | prose, watts and RPE, execution cue |
| openai/gpt-5.6 | 38 | prose, watts, no context |
| anthropic/claude-opus-4.8 | 38 | dense but complete |
| openai/gpt-5.6-sol | 38 | prose, watts, no context |
| openai/gpt-5.6-sol-pro | 37 | prose, watts, no context |
| openai/gpt-5.6-terra | 35 | prose, watts, no context |
| x-ai/grok-4.5 | 32 | shorthand (WU/CD), watts, purpose |
| qwen/qwen3.8-max | 32 | prose, RPE-led |
| openai/gpt-5.6-terra-pro | 31 | prose, watts, no context |
| google/gemini-3.1-pro-preview | 29 | **zones, not watts** |
| deepseek/deepseek-v4-pro | 24 | RPE only, no watt targets |
| anthropic/claude-sonnet-5 | 23 | one sentence, but watts included |

Length is not quality on its own — opus-4.8 packs anchor, %FTP, work:rest and the masters
cap into a 55-word session. But the bottom of this table costs you something concrete:
gemini-3.1-pro-preview prescribes "4x4 min in Z5", so you cannot execute the session without
looking the zone up, and its recoveries are 4 min against the science file's ~2:1 work:rest
for HIIT. deepseek gives RPE with no power target at all, which on a Kickr is a step
backwards.

The five gpt-5.6 variants cluster tightly at 31–38 words with near-identical character —
watts and structure, no reason and no context — which is the clearest sign yet that they
share a house style rather than differing in care.

The same split shows in the plan text itself: macrocycle strategies run from 794 words
(opus-5) and 536 (opus-4.8) down to 122 (deepseek) and 113 (gemini-3.7-flash). The long
strategies also make the *next* call more expensive — the workout-generation prompt is
~39k tokens for opus-5 against ~21k for most others, because the plan is fed back in.

### 8. Cost and wall time

| Model | plan generate | workout generate | workout adapt | whole run |
|---|---|---|---|---|
| google/gemini-3.7-flash | 9 s | 60 s | 12 s | 102 s |
| openai/gpt-5.6-terra | 18 s | 73 s | 24 s | 158 s |
| openai/gpt-5.6-terra-pro | 38 s | 127 s | 21 s | 244 s |
| anthropic/claude-opus-4.8 | 43 s | 139 s | 21 s | 253 s |
| openai/gpt-5.6-sol | 33 s | 167 s | 12 s | 299 s |
| google/gemini-3.1-pro-preview | 24 s | 162 s | 43 s | 334 s |
| openai/gpt-5.6 | 45 s | 160 s | 61 s | 338 s |
| x-ai/grok-4.5 | 61 s | 224 s | 21 s | 385 s |
| openai/gpt-5.6-sol-pro | 72 s | 254 s | 21 s | 457 s |
| deepseek/deepseek-v4-pro | 60 s | 271 s | 113 s | 624 s |
| anthropic/claude-opus-5 | 111 s | 434 s | 85 s | 742 s |
| z-ai/glm-5.2 | 60 s | 577 s | 33 s | 755 s |
| anthropic/claude-sonnet-5 | 168 s | 522 s | 26 s | 880 s |
| qwen/qwen3.8-max | 207 s | 896 s | 301 s | 1803 s |
| moonshotai/kimi-k3 | 228 s | 1322 s | 19 s | 1900 s |

kimi-k3 and qwen3.8-max take ~30 minutes for a full run, and kimi-k3 is the only model that
needed retries (see *Re-running* below); gemini-3.7-flash finishes everything in under two
minutes but produces the thinnest plan in the set. The whole gpt-5.6 family is cheap — the
five variants span 158–457 s, all in the fastest half of the field — and the `-pro` suffix
costs almost exactly 1.5× its base variant (terra 158 → 244 s, sol 299 → 457 s).

### 9. The gpt-5.6 family, side by side

Five variants of one family are now in the set, which makes it possible to separate house
style from run-to-run variation.

| | gpt-5.6 | terra | terra-pro | sol | sol-pro |
|---|---|---|---|---|---|
| Last block | absorption | VO2max | HIIT | HIIT | HIIT |
| Plan ends | **09-26** | 09-30 | 09-30 | 09-30 | 09-30 |
| h/wk | 7.0 | **9.0** | 8.2 | 6.8 | 7.5 |
| Deload | collapses wk 5 | none | none | wk 4, −45% | wk 4, −27% |
| FTP tests | **4** (14/6/5 d) | 2 (21 d) | 3 (23/**11** d) | 3 (**7**/17 d) | 2 (24 d) |
| Max test on 09-29 | yes | no | **yes** | **yes** | **yes** |
| Over daily budget | 0 | **3** | 0 | 0 | 0 |
| Weeks under strength floor | 0 | **2** | 0 | 0 | 0 |
| Says "outdoor" | 0 | 9 | 15 | 0 | 0 |
| Names the climb | 1 | 0 | 0 | 0 | 0 |
| Words/session | 38 | 35 | 31 | 38 | 37 |
| Whole run | 338 s | 158 s | 244 s | 299 s | 457 s |

**Shared by all five**, and therefore family traits rather than accidents: the interval
progression restarts at short intervals when the final block opens; prescription density
sits in a narrow 31–38 word band with the same watts-but-no-reason character; and the
target climb is essentially never named in a session. Four of the five also put a maximal
test on 09-29.

**What the `-pro` suffix actually buys** is uneven, and it is not insight. sol → sol-pro is
a clean win on testing: the illegal 7-day retest gap widens to a legal 24 days, and the
deload survives. terra → terra-pro is a trade, not an upgrade: three daily-budget overruns
and two strength-floor misses both go to zero and 9.0 h/week comes back to a sane 8.2, but
terra's clean 21-day test spacing degrades to 11 days, and terra-pro *adds* a maximal test
on 09-29 that terra did not have. Neither `-pro` touches the family's real weakness —
event specificity stays at exactly zero in both — and sol-pro thins its climb-specific
block to two quality sessions. You pay ~1.5× the wall time for a plan aimed at the same
nothing-in-particular.

**Picking one** comes down to a genuine trade, since terra-pro, sol and sol-pro all respect
every stated constraint and all finish on 09-30. terra-pro pitches the volume closest to
the 8 h target (8.2) and is the only one of the three that ever says "outdoor", but it
retests after 11 days and never deloads. sol-pro has the cleanest test spacing in the
family (a single 24-day gap) and a real, if shallow, deload, but plans 7.5 h with only two
quality sessions in its specific block and never mentions where a ride happens. Take
terra-pro if you want the load right, sol-pro if you want the testing right.

Avoid the base `gpt-5.6`, whose macrocycle ends four days before the goal, and `terra`,
the only variant that breaks the athlete's own daily budgets and strength floor. And on
any of the five, delete the 09-29 test before using the plan.

### If you only open three files

- `anthropic-claude-opus-5.txt` — the most complete: block order argued rather than
  assumed, anchor tested on day 3, clean 3:1 loading, converges on the event, schedules the
  attempt with fallback days, and is the only plan that engages with the athlete's actual
  failure mode (over-riding easy days, the hill home).
- `google-gemini-3-1-pro-preview.txt` — the best pure progression, ten quality sessions on a
  textbook duration ramp, and it follows the science file's time-trial ordering. Costs you
  watt targets (zones only) and a deload.
- `openai-gpt-5-6.txt` — instructive as a failure case: four FTP tests in four weeks, a
  macrocycle that ends four days before the goal, and a hard test the day before the target
  date.

If you are choosing within the gpt-5.6 family rather than across the whole field, open
`openai-gpt-5-6-terra-pro.txt` — see *The gpt-5.6 family, side by side* above.

These are judgements from measurable criteria — block order against §3, test spacing
against `benchmarks.txt` §1, load rhythm, stated-constraint compliance — not a claim about
which plan would actually make the climb faster.

## Reading sections 7 and 8: what each model could actually see

`workout adapt` only considers constraints between the target date and the end of the
**active mesocycle**, and the models chose very different block lengths. The window each
run actually used is printed in its section 7 ("remainder of the mesocycle (… -> …)"):

| Model | Adapt window | Saw Aug 30 ride | Mentions it | Sessions changed | of those, on Aug 29–30 |
|---|---|---|---|---|---|
| anthropic/claude-opus-5 | 08-16 → 09-05 | **yes** | 3× | 3 | 2 |
| google/gemini-3.1-pro-preview | 08-16 → 09-06 | **yes** | 2× | 3 | 2 |
| openai/gpt-5.6 | 08-16 → 09-05 | **yes** | 2× | 3 | 2 |
| deepseek/deepseek-v4-pro | 08-16 → 08-30 | **yes** | 1× | 0 | — |
| google/gemini-3.7-flash | 08-16 → 09-06 | **yes** | 0 | 0 | — |
| moonshotai/kimi-k3 | 08-16 → 09-05 | **yes** | 0 | 0 | — |
| openai/gpt-5.6-sol | 08-16 → 09-05 | **yes** | 0 | 0 | — |
| openai/gpt-5.6-sol-pro | 08-16 → 09-05 | **yes** | 0 | 0 | — |
| anthropic/claude-opus-4.8 | 08-16 → 08-29 | no | 0 | 0 | — |
| openai/gpt-5.6-terra-pro | 08-16 → 08-29 | no | 0 | 0 | — |
| anthropic/claude-sonnet-5 | 08-16 → 08-22 | no | 0 | 0 | — |
| x-ai/grok-4.5 | 08-16 → 08-29 | no | 0 | 0 | — |
| z-ai/glm-5.2 | 08-16 → 08-29 | no | 0 | 0 | — |
| openai/gpt-5.6-terra | 08-16 → 08-25 | no | 0 | 2 | — |
| qwen/qwen3.8-max | 08-16 → 08-23 | no | 0 | 8 | — |

The **Aug 30 group ride** was visible to 8 of 15, and those eight split three ways:

- **Absorbed it** — opus-5, gemini-3.1-pro-preview and gpt-5.6 each reasoned about the ride
  and reshaped the Aug 29–30 weekend around it, typically moving the long low-intensity ride
  to Saturday and counting Sunday's group ride as the week's hard session.
- **Considered it and declined** — deepseek-v4-pro named the ride, judged it "advisory"
  with a deload week following, and deliberately proposed no change. A defensible call,
  explicitly argued.
- **Never mentioned it** — gemini-3.7-flash, kimi-k3, gpt-5.6-sol and gpt-5.6-sol-pro had
  the constraint in the prompt and in range, and none referenced it or changed anything.
  Both sol variants reasoned at length about HRV, ATL:CTL and alcohol, concluded "all
  metrics are green", and never noticed the ride sitting inside the window they were
  reasoning about.

The other seven could not see it: their active block ends before Aug 30. Two of them
(gpt-5.6-terra, qwen3.8-max) still adapted, on recovery grounds rather than either
constraint — qwen touching 8 sessions off green metrics is worth a look on its own.

The **Sep 11–13 wedding** was outside every model's block, so no model acted on it here. It
is stored in all fifteen databases and will surface on a later `workout adapt` or the next
`plan generate`.

So sections 7–8 compare two things at once: how well a model adapts around a known
disruption, and how far ahead its own block structure lets it look in the first place. When
judging "no change proposed", check the window and the mentions column first — half the
field never saw the ride, and four that did said nothing about it.

## Deliberate differences from the real install

- **Google Calendar is disabled.** The run copy's calendar singleton is replaced with an
  inert stand-in and the `google:` block is dropped from the run's `config.yaml`, so no run
  can write to the real calendar. Daily context is seeded directly instead of synced.
- **`llm.request_timeout_seconds` is raised to 900**, since a full 45-day generation is a
  large call. Nothing else in `config.yaml` is changed — the athlete profile, weekly
  schedule, equipment, preferences and injuries are the real ones.
- **No Garmin pull.** `--no-pull` everywhere; the seeded data through 2026-08-16 is the
  only input.
- **"Today" is pinned to 2026-08-16** in the run copy's `trainmate/util.py`, which is the
  only date source the planning code reads. Needed because the last three models were run
  on 2026-08-17: a Monday rather than a Sunday, which would have shifted every plan onto a
  different week boundary and made the weekly tables incomparable.
- **Sections 6–8 ran against the same copied code as sections 0–5**, which predates any
  later edits in the repo, so all nine sections of a file describe one consistent build.

## Re-running

`setup_run.py <slug>` rebuilds a run directory from scratch; `run_model.py <model>` does
sections 0–5; `redo_step.py <model>` redoes sections 4–8 (remove constraints → regenerate
workouts → add constraints back → adapt) and splices them in, rebuilding the run log.

For a newly added model, `drive_new.sh` runs both steps in order (`run_model.py` then
`redo_step.py`) for every entry in `pending_new.txt`, three at a time — that is how the
three gpt-5.6 variants were produced. All three completed first time, every command exit 0.

Two runs needed a retry, both for transport reasons rather than model output: kimi-k3's
adapt call once exceeded the 1800s harness cap (since raised to 5400s), and its
`workout generate` once died on an OpenRouter connection reset. In the second case the
regeneration never applied, so its database was restored from `snapshots/pre-adapt/` and the
whole of `redo_step.py` re-run — otherwise its adapt would have been measuring an
already-adapted schedule.

Snapshots kept along the way: `snapshots/pre-adapt/` (each database after the original
section 5), `snapshots/pre-redo/` (before this regeneration), plus `out-pre-adapt-backup/`
and `out-pre-redo-backup/` for the corresponding output files. `adapt_step.py` is the
earlier, superseded second-pass script; `redo_step.py` is the one that produced the current
sections 4–8.
