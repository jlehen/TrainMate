# TrainMate model comparison — 2026-08-18

> Run on 2026-08-18 against the author's real install. This is a snapshot of the
> experiment's report, kept in the repo for reference: the run directories it mentions
> (`out/<model>.txt`, `runs/`) and the driver scripts live in the private experiment
> tree, not here. Location and identity details are fictionalized for publication
> (matching `config.sample.yaml`); every measured number is as run.

One isolated TrainMate install per model, each with its own database, code copy and
pinned clock, so the fifteen runs are directly comparable. The test is a **two-goal
season** with a constraint known before planning and two constraints dropped on the
schedule afterwards.

The scenario, on a simulated "today" of 2026-08-18:

- **Goal 1 (B, id 1)** — *Hillcrest hill-climb battle*, 2026-09-27, priority 2, `event`.
  A friendly showdown ~6 weeks out.
- **Goal 2 (A, id 2)** — *Eastbridge Autumn Gravel Fondo* (~90 km / ~1800 m), 2026-11-22,
  priority 1, `event`. 8 weeks after goal 1, 14 weeks out.
- Only **goal 2** is planned (`plan generate -g 2`). Because goal 1 has no plan of its
  own, the app builds one macrocycle spanning the full 14 weeks — so every model must
  periodize **through** the B-event on the way to the A-event. Whether it plans a
  mini-taper, a full (wrong) taper, or ignores the event entirely is the first thing
  this scenario measures.
- **Work-travel week Oct 12–18**, added **before** planning: no bike, no kettlebells,
  hotel gym with dumbbells/machines/stationary bike, 45–60 min most mornings, and the
  athlete cannot run. Tests whether generation honors a constraint deep in the plan.
- **Group ride Aug 30** and a **3-day wedding with alcohol Sep 11–13**, added **after**
  the 14-week schedule was built, followed by `workout adapt`. Tests whether adaptation
  reworks an existing schedule around late-breaking life.

## What to read

`out/<model>.txt` — one file per model, ten labelled sections:

| Section | Content |
|---|---|
| 0 | Goals in the database (`goal list`) |
| 1 | `data bootstrap` — history reconstruction, mesocycle inference, coach learnings |
| 2 | The travel constraint added before any planning |
| 3 | `plan generate -g 2` as printed while generating (the coach's reasoning) |
| 4 | The resulting plan (`plan show -g 2 -w`) |
| 5 | `workout generate -g 2` as printed while generating |
| 6 | The resulting 14-week schedule (`workout list -g 2 -v`) — the pre-adapt baseline |
| 7 | The two late constraints, and the full constraint list |
| 8 | `workout adapt` as printed while adapting (the decision summary) |
| 9 | The workouts after adaptation — diff against section 6 to see what moved |
| — | Run log: per-command exit code and wall time |

`runs/<model>/logs/llm_exchanges/*.md` holds the full prompt and raw response for each
LLM call. Every number below is measured straight from the run databases and run logs.
Nothing is hand-counted.

## How each run was built

Each run gets a private copy of the code, a fresh database migrated by the app itself
and seeded with the athlete's metrics/activities/context through 2026-08-18,
Garmin/Calendar sync watermarks set so nothing ever pulls, Google Calendar replaced by
an inert stub, and `today_date()` pinned to **2026-08-18**, so every model plans from
the same Tuesday. The LLM HTTP timeout was raised to 1800 s: the 14-week generation is
a single long call, and two models (kimi-k3 1193 s, qwen3.8-max 1312 s) would have died
at the default 900 s.

## The measured table

The headline row per model; each column is unpacked into its own table further down.

| model | workouts | mesos | B-boundary | travel viol. | bike on gym days | over-dur | wedding TSS | adapted | gen s |
|---|---|---|---|---|---|---|---|---|---|
| claude-opus-4.8 | 96 | 6 | no | 0 | 25 | 4 | 185 | 0 | 222 |
| claude-opus-5 | 99 | 6 | yes | 0 | 2 | 2 | 195 | 0 | 461 |
| claude-sonnet-5 | 97 | 9 | yes | 0 | 0 | 1 | 140 | 0 | 573 |
| deepseek-v4-pro | 96 | 7 | yes | 0 | 18 | 1 | 185 | 2 | 526 |
| gemini-3.1-pro-preview | 96 | 4 | yes | 0 | 2 | 1 | 0 | 4 | 139 |
| gemini-3.7-flash | 96 | 4 | yes | 1 | 1 | 0 | 230 | 0 | 107 |
| kimi-k3 | 97 | 5 | no | 0 | 27 | 15 | 105 | 2 | 1193 |
| gpt-5.6 | 109 | 8 | yes | 0 | 14 | 0 | 139 | 2 | 209 |
| gpt-5.6-sol | 107 | 8 | yes | 0 | 24 | 0 | 152 | 1 | 201 |
| gpt-5.6-sol-pro | 97 | 6 | yes | 0 | 1 | 0 | 212 | 7 | 246 |
| gpt-5.6-terra | 107 | 8 | yes | 0 | 26 | 4 | 240 | 0 | 126 |
| gpt-5.6-terra-pro | 96 | 9 | yes | 0 | 2 | 0 | 215 | 0 | 151 |
| qwen3.8-max | 96 | 7 | yes | 0 | 17 | 7 | 255 | 1 | 1312 |
| grok-4.5 | 96 | 7 | no | 0 | 14 | 6 | 138 | 5 | 273 |
| glm-5.2 | 0 — gen failed ×2 | 5 | — | — | — | — | — | — | 750 |

Column notes. *B-boundary*: a mesocycle boundary within ±2 days of the B-event —
"no" means the block structure rolls past the race. *travel viol.*: outdoor-bike
sessions scheduled inside Oct 12–18. *bike on gym days*: cycling sessions on the four
weekdays the athlete's `weekly_schedule` marks gym-only (no bike available). *over-dur*:
sessions longer than the day's `total_available_hours`. *wedding TSS*: load left on
Sep 11–13 after adapt (0 = cleared). *adapted*: rows `workout adapt` actually changed.

glm-5.2 never produced a schedule (see *glm-5.2: the structural-compliance failure* at
the end), so it is absent from the per-dimension tables below.

## The plans compared (sections 3–6)

### 1. The 14-week shape

All fourteen completed models spanned the full 14 weeks and treated the B-event as a
**train-through tune-up** rather than a second peak — the phrasing is strikingly
convergent ("B-event ... not a full taper" appears in nearly every strategy). Every
model also names the travel week explicitly in its block structure — most as its own
mesocycle, five folded into a longer block (·travel below). The block structures
themselves:

| model | blocks | in order (weeks) | B-event boundary |
|---|---|---|---|
| claude-opus-4.8 | 6 | Base 2.9w → HIIT·B 3.1w → Threshold·travel 3.4w → Threshold 1.9w → Peak/taper 0.9w → Race 0.9w | **no** |
| claude-opus-5 | 6 | Base 2.7w → HIIT·B 2.9w → Threshold·travel 3.9w → HIIT 1.9w → Peak/taper 0.9w → Race 0.9w | yes |
| claude-sonnet-5 | 9 | SIT 1.9w → HIIT 1.9w → Peak/taper·B 1.7w → Threshold 1.9w → Travel 0.9w → Recovery 0.9w → Specific 1.9w → Peak/taper 0.9w → Race 0.9w | yes |
| deepseek-v4-pro | 7 | Base 1.9w → HIIT 3.9w → HIIT 1.7w → Travel 0.9w → Threshold 2.9w → Peak/taper 0.9w → Race 0.9w | yes |
| gemini-3.1-pro-preview | 4 | HIIT·B 6.0w → Threshold·travel 2.6w → Threshold 2.9w → Peak/taper 1.9w | yes |
| gemini-3.7-flash | 4 | Threshold 5.7w → Travel 2.9w → HIIT 2.9w → Peak/taper 1.9w | yes |
| kimi-k3 | 5 | HIIT 3.7w → Threshold·B 3.9w → Travel 0.9w → Threshold 2.9w → Peak/taper 1.9w | **no** |
| gpt-5.6 | 8 | SIT 1.7w → HIIT·B 2.9w → B-tune 0.9w → Threshold 1.9w → Threshold·travel 0.9w → Specific 2.9w → Peak/taper 0.9w → Race 0.9w | yes |
| gpt-5.6-sol | 8 | Base 1.7w → HIIT·B 2.9w → B-tune 0.9w → Base 1.9w → Travel 0.9w → Specific 2.9w → Peak/taper 0.9w → Race 0.9w | yes |
| gpt-5.6-sol-pro | 6 | Base 2.7w → HIIT·B 2.9w → Threshold·travel 2.9w → Specific 2.9w → Peak/taper 0.9w → Race 0.9w | yes |
| gpt-5.6-terra | 8 | SIT 2.9w → Specific·B 1.7w → Peak/taper·B 0.9w → Base 1.9w → Travel 0.9w → Specific 2.9w → Peak/taper 0.9w → Race 0.9w | yes |
| gpt-5.6-terra-pro | 9 | SIT 1.9w → Specific·B 2.7w → B-tune 0.9w → Recovery·B 0.9w → Threshold 0.9w → Travel 0.9w → Specific 2.9w → Peak/taper 0.9w → Race 0.9w | yes |
| qwen3.8-max | 7 | SIT 1.7w → HIIT·B 3.9w → Consolidation 1.9w → Travel 0.9w → Threshold 2.9w → Peak/taper 1.7w → Race 0.0w | yes |
| grok-4.5 | 7 | Base 1.9w → SIT 1.9w → HIIT·B 2.7w → Travel 1.9w → Threshold 2.9w → Peak/taper 0.9w → Race 0.9w | **no** |

Each block name is compressed to a type label (keyword-matched; "Specific" =
event-specific build, "B-tune" = a dedicated B-race tune-up block). **·B** marks a
block whose name references the Hillcrest climb, **·travel** a block that absorbs the
travel week rather than isolating it. The full block names are in each report's
section 4.

Three models let their block structure roll straight past the B-race: opus-4.8's HIIT
block ends 09-30 (three days after the event), kimi-k3's threshold block runs to
10-11, grok-4.5's to 10-04 — the race sits mid-block with no structural
acknowledgement. Details worth calling out beyond the table:

- **qwen3.8-max says "no full taper" and then plans one**: Sep 15–27 is two weeks of
  reduced volume, rest days and openers — a full taper for a B-event, contradicting its
  own strategy text. It also opens the 14-week autumn plan with a sprint (SIT) block on
  the argument that anaerobic residuals "decay quickly" — which is precisely the reason
  to put SIT *late*, not first.
- **gemini-3.1-pro-preview starts HIIT on day 1** with no base block, against its own
  bootstrap data showing CTL falling — and its history reconstruction invents a "summer
  overload maintaining a heightened CTL base" that never happened.
- **claude-opus-5** was the only model to derive its weekly shape from the athlete's
  actual per-day equipment calendar (bikes exist on only three days of its week) and the only one
  to place an FTP re-test with a reasoned trigger and a no-retest window before the race.
- **kimi-k3** wrote the most operationally specific plan (bookended travel deload, cold/
  wet-kit fondo simulations, a Nov 7 dress rehearsal, correct race-day sessions for both
  events) — and then broke its own volume promise with a 10.5 h week containing seven
  consecutive training days, and racked up the worst availability score (see table 6).

### 2. Load: weekly TSS across the 14 weeks

*Italics* mark the travel week; **bold** marks a ≥25% drop against the preceding week
(a deload-sized dip, planned or not). The h/wk column averages the 12 full weeks
08-24 → 11-15, travel week included, against the athlete's 8 h target.

| model | 08-17 | 08-24 | 08-31 | 09-07 | 09-14 | 09-21 | 09-28 | 10-05 | 10-12 | 10-19 | 10-26 | 11-02 | 11-09 | 11-16 | h/wk |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| claude-opus-4.8 | 265 | 285 | 348 | 323 | 301 | 233 | 268 | 376 | *238* | 403 | 329 | 483 | **213** | 314 | 7.0 |
| claude-opus-5 | 247 | 337 | 306 | 405 | 413 | **260** | 340 | 455 | *228* | 423 | 398 | 418 | **271** | 385 | 7.5 |
| claude-sonnet-5 | 245 | 280 | 297 | 282 | 240 | 181 | 285 | 360 | *163* | 259 | 345 | 347 | **182** | 294 | 6.7 |
| deepseek-v4-pro | 245 | 302 | 345 | 360 | 360 | **185** | 365 | 320 | *120* | 370 | 375 | **210** | **140** | **100** | 6.0 |
| gemini-3.1-pro-preview | 295 | 305 | 320 | **160** | 280 | **205** | 420 | 445 | *125* | 340 | 400 | 415 | **230** | 320 | 6.8 |
| gemini-3.7-flash | 386 | 410 | 420 | 390 | 325 | **225** | 337 | 380 | *180* | 395 | 415 | 400 | **220** | **92** | 7.1 |
| kimi-k3 | 305 | 437 | 482 | **215** | 392 | 479 | **353** | 505 | *163* | 375 | 519 | 537 | **225** | 305 | 9.1 |
| gpt-5.6 | 251 | 306 | 336 | 343 | 334 | **246** | 292 | 342 | *144* | 338 | 336 | 331 | **196** | **96** | 6.7 |
| gpt-5.6-sol | 240 | 353 | 342 | 352 | 349 | 278 | 272 | 343 | *158* | 324 | 382 | 388 | **247** | **110** | 7.5 |
| gpt-5.6-sol-pro | 167 | 275 | 357 | 370 | 380 | **252** | 272 | 354 | *182* | 316 | 380 | 380 | **228** | **88** | 6.8 |
| gpt-5.6-terra | 326 | 440 | 460 | 485 | 475 | **253** | 367 | 440 | *140* | 485 | 520 | 505 | **318** | **102** | 8.9 |
| gpt-5.6-terra-pro | 271 | 330 | 400 | 410 | 390 | **257** | **173** | 420 | *170* | 438 | 433 | 413 | **267** | **89** | 7.3 |
| qwen3.8-max | 300 | 378 | 413 | 420 | **306** | **195** | 381 | 383 | *165* | 423 | 428 | 423 | **216** | 317 | 7.6 |
| grok-4.5 | 240 | 310 | 261 | 298 | 295 | 307 | 300 | 355 | *158* | 325 | 326 | 302 | **168** | **77** | 7.5 |

The load rhythm is strikingly uniform across the field, because the scenario forces it:
nearly everyone dips into the B-race week (09-21), bottoms out in the travel week, and
drops again for the peak week (11-09). Within that shared shape, the spread is at the
edges. **kimi-k3 (9.1 h) and gpt-5.6-terra (8.9 h) plan well over the 8 h budget** —
terra has six weeks at 460 TSS or more, and kimi stacks its two heaviest weeks of the
whole plan (519, 537) into the final build block. At the other end **deepseek-v4-pro
plans just 6.0 h** and starts shedding load from 11-02, a three-week glide into the
race. grok-4.5 is the only model whose load *rises* into the B-race week (295 → 307
TSS) — consistent with its block rolling past the event.

The tiny race weeks (77–110 TSS) in the 11-16 column belong to the eight models whose
schedule ends on Nov 21 — the app's end-exclusive horizon bug (see *App findings*), not
a modelling choice; the models with a race-day session show 294–385.

### 3. Race days

What sits on the two event dates, and how each model arrives at the B-race:

| model | B-event 2026-09-27 | day before B | A-event 2026-11-22 |
|---|---|---|---|
| claude-opus-4.8 | Hillcrest Hill-Climb (B-Race) (rpe 9, 90m) | Rest Day | Eastbridge Autumn Gravel Fondo (A-RACE) (rpe 9, 210m) |
| claude-opus-5 | B-RACE: Hillcrest Hill-Climb (rpe 10, 90m) | Openers (rpe 6, 50m) | A-RACE: Eastbridge Autumn Gravel Fondo (rpe 9, 240m) |
| claude-sonnet-5 | Hillcrest Hill-Climb (B-Race) (rpe 8, 80m) | Pre-Race Openers (rpe 4, 40m) | Eastbridge Autumn Gravel Fondo (A-Race) (rpe 8, 300m) |
| deepseek-v4-pro | **Recovery Spin (rpe 1, 45m)** | **Hillcrest Hill-Climb (Race) (rpe 10, 90m)** | — |
| gemini-3.1-pro-preview | Hillcrest Hill-Climb (B-Race) (rpe 9, 90m) | Road Easy Spin (rpe 3, 60m) | Eastbridge Autumn Gravel Fondo (A-Race) (rpe 9, 240m) |
| gemini-3.7-flash | Hillcrest Hill-Climb Battle (B-Race) (rpe 9, 80m) | Pre-Race Opener Ride (rpe 4, 40m) | — |
| kimi-k3 | Hillcrest Hill-Climb (B-Race) (rpe 10, 90m) | Easy Spin + Openers (rpe 3, 45m) | Eastbridge Autumn Gravel Fondo (A-Race) (rpe 9, 300m) |
| gpt-5.6 | Hillcrest Hill-Climb Battle (rpe 10, 60m) | Hill-Climb Openers (rpe 4, 35m) | — |
| gpt-5.6-sol | Hillcrest Hill-Climb Battle (rpe 10, 75m) | Pre-Race Openers (rpe 5, 30m) | — |
| gpt-5.6-sol-pro | Hillcrest Hill-Climb Battle (rpe 9, 90m) | Hill-Climb Openers (rpe 4, 40m) | — |
| gpt-5.6-terra | Hillcrest Hill-Climb Battle (rpe 9, 75m) | Pre-Race Rest | — |
| gpt-5.6-terra-pro | Hillcrest Hill-Climb Battle (rpe 9, 90m) | Pre-Race Easy Spin (rpe 2, 45m) | — |
| qwen3.8-max | Hillcrest Hill-Climb Battle (rpe 9, 90m) | Pre-Race Activation (rpe 3, 30m) | Eastbridge Autumn Gravel Fondo — Race Day (rpe 9, 180m) |
| grok-4.5 | Hillcrest Hill-Climb Battle (B-Race) (rpe 9, 90m) | Easy Spin (Pre-B-Race) (rpe 2, 60m) | — |

Two findings sit in this table. **deepseek-v4-pro scheduled the B-race on the wrong
day** — "Hillcrest Hill-Climb (Race)" on Saturday Sep 26, with a recovery spin on the
actual event date. The only hard date error in the field. And the empty A-event column
for eight models is the **race-day off-by-one**: the app literally asks for workouts
through Nov 21 (see *App findings* #1), so those eight followed the requested span
faithfully. The six models with a Nov 22 session exceeded the requested horizon,
usefully. The B-race run-ins are uniformly sensible — openers or rest everywhere.

### 4. The travel week: near-universal compliance

This was the constraint known at planning time, and it worked:

| model | sessions | h total | longest (min) | outdoor-bike violations |
|---|---|---|---|---|
| claude-opus-4.8 | 4 cycling, 2 strength, 1 rest | 5.1 | 55 | 0 |
| claude-opus-5 | 4 cycling, 2 strength, 1 rest | 5.1 | 60 | 0 |
| claude-sonnet-5 | 4 cycling, 2 strength, 1 rest | 4.7 | 55 | 0 |
| deepseek-v4-pro | 3 rest, 2 strength, 2 cycling | 2.8 | 45 | 0 |
| gemini-3.1-pro-preview | 3 cycling, 2 strength, 2 rest | 3.8 | 45 | 0 |
| gemini-3.7-flash | 3 cycling, 2 strength, 2 rest | 4.5 | 75 | **Oct 18 Post-Travel Easy Gravel Spin** |
| kimi-k3 | 3 cycling, 2 strength, 1 yoga, 1 rest | 4.4 | 50 | 0 |
| gpt-5.6 | 3 cycling, 2 strength, 2 rest | 3.9 | 50 | 0 |
| gpt-5.6-sol | 3 cycling, 2 strength, 2 rest | 4.2 | 55 | 0 |
| gpt-5.6-sol-pro | 3 cycling, 2 strength, 2 rest | 4.2 | 60 | 0 |
| gpt-5.6-terra | 3 cycling, 2 strength, 2 rest | 3.8 | 50 | 0 |
| gpt-5.6-terra-pro | 3 cycling, 2 strength, 2 rest | 4.0 | 55 | 0 |
| qwen3.8-max | 5 strength, 2 rest | 4.5 | 55 | 0 |
| grok-4.5 | 4 cycling, 2 strength, 1 rest | 4.8 | 55 | 0 |

Every "cycling" session above except flash's Oct 18 ride is hotel-stationary-bike or
equivalent indoor work. Thirteen of fourteen models produced a fully compliant week:
hotel strength with dumbbells/machines, stationary-bike aerobic work, no kettlebells,
and **no running anywhere** — many restate "no running" verbatim in session
descriptions, and all fit the 45–60 min morning windows (longest session 45–60 min
everywhere but flash's violation). Several elevated the week to its own named
mesocycle with an overload week before and a transition week after (the science
guidelines' trip-bookending protocol; kimi-k3, sonnet-5, opus-4.8, opus-5, grok-4.5
and gemini-3.1-pro all did this explicitly). qwen3.8-max explicitly rejected
bookending and was the outlier in style — five strength sessions and no aerobic work
at all.

The one violation: **gemini-3.7-flash scheduled a 75-min outdoor gravel ride on
Oct 18** ("Post-Travel Easy Gravel Spin ... on home gravel trails") — the last day of
the declared no-bike window.

### 5. Benchmark testing

FTP test sessions (test-titled cycling, confirmed against the `benchmark_type` flag)
across the 14 weeks — the athlete's 250 W FTP anchor enters the run unverified:

| model | FTP test | day of plan |
|---|---|---|
| kimi-k3 | 08-21 | 4 |
| gpt-5.6-terra-pro | 08-22 | 5 |
| gpt-5.6-sol | 08-26 | 9 |
| deepseek-v4-pro | 08-27 | 10 |
| grok-4.5 | 08-27 | 10 |
| gpt-5.6 | 08-29 | 12 |
| gpt-5.6-sol-pro | 09-02 | 16 |
| claude-opus-5 | 09-05 | 19 |
| claude-sonnet-5 | 09-30 | 44 |
| gemini-3.1-pro-preview | 10-24 | 68 |
| claude-opus-4.8 | never | — |
| gemini-3.7-flash | never | — |
| gpt-5.6-terra | never | — |
| qwen3.8-max | never | — |

Testing discipline is uniform: **no model schedules more than one test in 14 weeks**,
so illegally short retest gaps never arise. The field splits by *when* — six models anchor
the number inside the first two weeks, before the blocks that scale off it; opus-5
tests at the end of its base block with a stated trigger and an explicit no-retest
window before the race; sonnet-5 re-anchors three days after the B-race for its
threshold block; gemini-3.1-pro waits until week 10. And **four models — opus-4.8,
gemini-3.7-flash, gpt-5.6-terra and qwen3.8-max — ride the whole 14 weeks without
ever verifying the anchor**, while still prescribing watt-target sessions off it.

The `benchmark_type` column also shows app bug #2 in the raw data: after adaptation,
"Social Recovery Group Ride" (gpt-5.6), "Friends Group Ride" (sol-pro) and "Group
Ride Endurance" (grok) all still carry `ftp_20min` — a social ride flagged to record
as an FTP result.

### 6. Availability & prescription

The athlete's `weekly_schedule` puts a bike within reach on only three days a week;
the other four weekdays are short gym-only slots (~1.3 h). Sorted best to worst
(travel week excluded — its constraint explicitly grants a stationary bike):

| model | bike on gym-only days | over daily budget | rest days | words/session |
|---|---|---|---|---|
| claude-sonnet-5 | 0 | 1 | 16 | 22 |
| gemini-3.7-flash | 1 | 0 | 27 | 27 |
| gpt-5.6-sol-pro | 1 | 0 | 29 | 32 |
| gpt-5.6-terra-pro | 2 | 0 | 17 | 24 |
| gemini-3.1-pro-preview | 2 | 1 | 20 | 15 |
| claude-opus-5 | 2 | 2 | 15 | 59 |
| gpt-5.6 | 14 | 0 | 29 | 33 |
| grok-4.5 | 14 | 6 | 18 | 18 |
| qwen3.8-max | 17 | 7 | 16 | 47 |
| deepseek-v4-pro | 18 | 1 | 25 | 17 |
| gpt-5.6-sol | 24 | 0 | 26 | 36 |
| claude-opus-4.8 | 25 | 4 | 13 | 30 |
| gpt-5.6-terra | 26 | 4 | 17 | 26 |
| kimi-k3 | 27 | 15 | 11 | 35 |

The field splits cleanly in two: six models schedule 0–2 bike sessions on gym-only
weekdays, the other eight schedule 14–27. Most offenders are weekday Zwift spins —
arguably negotiable in real life, but the stated availability says no, and six models
prove it was satisfiable. **kimi-k3 compounds the worst weekday-bike count with 15
over-duration sessions and only 11 rest days in 14 weeks.** Prescription density
spans a 4× range: opus-5 writes 59-word protocols with contingencies,
gemini-3.1-pro-preview 15-word zone prescriptions; the gpt-5.6 family clusters
in a narrow band (24–36) with a shared watts-but-no-context character.

### 7. Cost and wall time

Per-command wall time from each run log (LLM total = the four LLM commands summed):

| model | bootstrap | plan generate | workout generate | workout adapt | LLM total |
|---|---|---|---|---|---|
| gemini-3.7-flash | 17 s | 17 s | 107 s | 8 s | 149 s |
| gpt-5.6-terra | 33 s | 30 s | 126 s | 15 s | 205 s |
| gemini-3.1-pro-preview | 52 s | 23 s | 139 s | 35 s | 250 s |
| gpt-5.6-terra-pro | 54 s | 52 s | 151 s | 41 s | 298 s |
| claude-opus-4.8 | 46 s | 46 s | 222 s | 6 s | 320 s |
| gpt-5.6 | 63 s | 38 s | 209 s | 30 s | 339 s |
| gpt-5.6-sol | 61 s | 44 s | 201 s | 38 s | 344 s |
| gpt-5.6-sol-pro | 104 s | 74 s | 246 s | 124 s | 548 s |
| grok-4.5 | 75 s | 100 s | 273 s | 156 s | 604 s |
| claude-opus-5 | 100 s | 155 s | 461 s | 24 s | 739 s |
| deepseek-v4-pro | 194 s | 31 s | 526 s | 124 s | 875 s |
| claude-sonnet-5 | 121 s | 115 s | 573 s | 79 s | 888 s |
| kimi-k3 | 402 s | 268 s | 1193 s | 391 s | 2254 s |
| qwen3.8-max | 345 s | 356 s | 1312 s | 298 s | 2311 s |

The 14-week single-call generation costs an order of magnitude more on some models
than others for the same task (flash 107 s vs qwen 1312 s), and kimi-k3 and
qwen3.8-max again close the field at ~38 minutes end to end — both were saved only by
the raised 1800 s HTTP timeout. Note the adapt column rewards doing nothing:
opus-4.8's 6 s adapt returned "no changes", while sol-pro's 124 s bought the best
gpt-family adaptation in the set.

## Reading sections 8 and 9: the adaptation

The two late constraints — group ride Aug 30, wedding Sep 11–13 — separated the field
more than anything else. `workout adapt` only reaches to the end of
the **active mesocycle**, so each model's window is set by its own first-block length;
"mentions" counts how often the decision summary (section 8) names each constraint:

| model | adapt window | saw ride / mentions | saw wedding / mentions | changed | Aug 30 after adapt | wedding TSS |
|---|---|---|---|---|---|---|
| gemini-3.1-pro-preview | → 09-29 | yes, 2× | **yes, 3×** | 4 | **Group Ride Showdown (rpe 8, 150m)** | **0** |
| gpt-5.6-sol-pro | → 09-06 | yes, 3× | no | 7 | Friends Group Ride (rpe 7, 120m) | 212 |
| grok-4.5 | → 08-31 | yes, 2× | no | 5 | Group Ride Endurance (Z2 Intent) (rpe 4, 140m) | 138 |
| deepseek-v4-pro | → 08-31 | yes, 2× | no | 2 | Group Ride (Long Outdoor) (rpe 4, 180m) | 185 |
| gpt-5.6 | → 08-30 | yes, 1× | no | 2 | Social Recovery Group Ride (rpe 3, 75m) | 139 |
| gpt-5.6-sol | → 08-30 | yes, 2× | no | 1 | Controlled Endurance Group Ride (rpe 6, 160m) | 152 |
| kimi-k3 | → 09-13 | yes, 0 | **yes, 0** | 2 | Zone 2 Gravel or Recovery Spin (rpe 3, 90m) | 105 |
| qwen3.8-max | → 08-30 | yes, 0 | no | 1 | Long Z2 Ride — SIT Block Closer (rpe 4, 150m) | 255 |
| claude-opus-4.8 | → 09-07 | yes, 0 | no | 0 | Easy Recovery Spin (rpe 3, 45m) | 185 |
| claude-opus-5 | → 09-06 | yes, 0 | no | 0 | Easy Spin (rpe 3, 60m) | 195 |
| claude-sonnet-5 | → 08-31 | yes, 0 | no | 0 | Recovery Hike (rpe 4, 120m) | 140 |
| gemini-3.7-flash | → 09-27 | yes, 0 | **yes, 0** | 0 | Long Zone 2 Rolling Hills Ride (rpe 4, 165m) | 230 |
| gpt-5.6-terra | → 09-07 | yes, 0 | no | 0 | Easy Road Endurance (rpe 3, 125m) | 240 |
| gpt-5.6-terra-pro | → 08-31 | yes, 0 | no | 0 | Easy Endurance Spin (rpe 3, 90m) | 215 |

(All windows start at 2026-08-18. The "Aug 30 after adapt" column shows the day's
session after adaptation — an unchanged easy spin means the model left a friends ride
"whose intensity is hard to control" labelled as recovery.)

- **gemini-3.1-pro-preview** was the only model to handle **both** constraints: it
  turned Aug 30 into a "Group Ride Showdown" (RPE 8, honestly relabelling the day as
  the hard session it will be, with openers the day before) and **cleared both wedding
  days to rest**. Best adapt in the benchmark.
- **gpt-5.6-sol-pro** produced the most sophisticated single-constraint response: seven
  changed sessions re-sequencing its FTP test off the group-ride day with pre/post
  recovery.
  **grok-4.5** (5 changes) did the same manoeuvre with the most realistic athlete
  guidance ("Expect surges you won't fully control—don't chase every attack").
  **gpt-5.6** (2), **deepseek-v4-pro** (2, moved its long ride onto the group ride) and
  **gpt-5.6-sol** (1, raised the day's expected intensity) all responded sensibly.
- **Six models changed nothing**: all three Anthropic models, gemini-3.7-flash,
  gpt-5.6-terra and terra-pro returned "all metrics are green, no changes" — and, per
  the mentions column, none of them even *named* the new constraints in their decision
  summaries, despite the group ride being inside every window and despite the CLI
  printing "this 3-day constraint displaces ~52% of a typical week's planned load" at
  them. terra-pro spent ~108k prompt tokens to conclude nothing. **kimi-k3** and
  **qwen3.8-max** adapted *something* while ignoring the constraints outright — kimi
  relocated its FTP test off the morning after an unplanned hard ride, qwen trimmed
  one ride to Z2 on recovery grounds; neither mentions the group ride or the wedding.
- The wedding exposed an emergent property of the app's design: whether Sep 11–13 was
  even visible depended on block-length choices each model made weeks earlier. Only
  **gemini-3.7-flash** (window to Sep 27) and **kimi-k3** (to Sep 13) had the wedding
  in reach besides gemini-3.1-pro — and both ignored it. For everyone else the correct
  app-level answer would have been a plan-level replan, which the CLI offered ("Replan
  around it? [y/N]") and the harness declined by design. A follow-up scenario could
  answer "y" and compare the replans.

## App findings (bugs the benchmark surfaced in TrainMate itself)

All four have since been fixed in the app; the descriptions below record the behavior
as measured on 2026-08-18.

1. **Race-day off-by-one**: `workout generate -g 2` computes its horizon as
   `(end_date - start).days`, which is end-exclusive — the app literally asks for
   workouts through Nov 21, the day *before* the A-race. Eight models followed the
   requested span and have **no race-day session** (all five gpt-5.6 variants,
   deepseek, flash, grok); six exceeded it, usefully, and scheduled the Nov 22 race
   (all three Anthropic models, gemini-3.1-pro, kimi, qwen). Fix: make the `-g`
   horizon inclusive.
2. **`[BENCHMARK]` tag survives adaptation**: when adapt converts an FTP-test day into
   a social ride (gpt-5.6, sol-pro, grok all did this), the replacement ride keeps the
   benchmark flag — "Friends Group Ride [BENCHMARK]" — which risks recording a group
   ride as an FTP result.
3. **`plan show` prints "Constraints considered: None"** even when an advisory
   constraint (the travel week) shaped the plan — only `replan=1` constraints are
   snapshotted into the plan fingerprint, so advisory ones vanish from the rendering
   even though the LLM saw them. Cosmetic, but it misleads exactly when auditing runs
   like these.
4. **kimi-k3's bootstrap rendered empty blocks** ("Macrocycle Focus ( to ): N/A",
   empty coach observations) and the CLI printed "No coach learnings yet. Run 'data
   bootstrap'..." immediately *after* bootstrap ran — worth a look at what a
   partially-parseable bootstrap response leaves behind (gpt-5.6-terra hit the same
   empty-learnings state).

## glm-5.2: the structural-compliance failure

glm-5.2's first attempt failed at `workout generate`: after 388 s and 58k completion
tokens it returned *complete, well-formed JSON of the wrong shape* — a top-level list
of week-objects each holding a `workouts` array, instead of the required single
`{"workouts": [...]}` object — and the app died on `'list' object has no attribute
'get'`. Its plan (4 blocks to Nov 22) had been fine. The full failed report is
preserved as `out/z-ai-glm-5-2.txt.attempt1`.

The retry (now the main `out/z-ai-glm-5-2.txt`) failed the same step a **different
way**: 750 s and 50k completion tokens ending in a missing-comma JSON error at
character 58,495, with the raw response tail degenerating into a long run of
whitespace — a classic long-output breakdown. Everything before and after the
generation step worked both times (its 5-block plan is reasonable, with a bookended
travel mesocycle). Two attempts, two distinct failure modes: glm-5.2 cannot reliably
sustain a ~100-session single-call generation — the clearest capability failure in
the benchmark.

## A ranking, for what it's worth

The one section that is judgement rather than measurement — but every grade below is
anchored in a numbered table above (the column headers say which), and the two halves
of the scenario weigh equally: the 14-week plan (tables 1–6) and the response to the
late constraints (the adaptation table). Within the plan, whether the athlete can
actually *execute* it — right race days, legal travel week, sessions on days the
equipment exists — counts more than how the prose reads. Adaptation weighs heavily
because it is the axis this scenario was designed to separate on, and it separated
more than everything else combined.

### The scorecard

| rank | model | structure §1 | race days §3 | travel §4 | avail. §6 | load §2 | testing §5 | adapt §8–9 |
|---|---|---|---|---|---|---|---|---|
| 1 | gemini-3.1-pro-preview | C+ | A | A | A- | B | B | **A+** |
| 2 | gpt-5.6-sol-pro | B+ | B- | A | A | B+ | A- | A- |
| 3 | claude-opus-5 | **A** | A | A | A- | A- | **A** | F |
| 4 | claude-sonnet-5 | A- | A | A | **A** | B | B+ | F |
| 5 | grok-4.5 | C | B- | A | C- | B- | A- | A- |
| 6 | gpt-5.6 | A- | B- | A | C | B | A- | B |
| 7 | gpt-5.6-terra-pro | A- | B- | A | A- | B+ | A- | F |
| 8 | gpt-5.6-sol | B+ | B- | A | D+ | A- | A- | B- |
| 9 | deepseek-v4-pro | B | **F** | A- | C- | C | A- | B |
| 10 | kimi-k3 | B- | A | A | **F** | D | A- | D+ |
| 11 | gemini-3.7-flash | B- | B- | D | A | B+ | F | F |
| 12 | claude-opus-4.8 | C | A | A | D | B+ | F | F |
| 13 | qwen3.8-max | D+ | A | B | D+ | B+ | F | D |
| 14 | gpt-5.6-terra | B+ | B- | A | D | D+ | F | F |
| 15 | glm-5.2 | *two failed generations — no schedule to grade* | | | | | | |

The B- cluster in the race-days column is the eight models with no A-race session;
that miss is app-assisted (the off-by-one horizon, App findings #1), so it costs a
grade, not a tier. F is reserved for outright failures: a race on the wrong date, a
constraint violated, watt targets with the anchor never tested, an adapt that changed
nothing — or, for kimi's availability, 27 weekday bikes *plus* 15 over-duration
sessions *plus* 11 rest days in 14 weeks.

### The order, defended

1. **gemini-3.1-pro-preview** — wins on the scenario's actual test. It was the only
   model to handle *both* late constraints: Aug 30 honestly relabelled as the hard
   ride it will be (RPE 8 with openers the day before), both wedding days cleared to
   rest — the only zero in the wedding-TSS column. Around that: both race days, a
   near-clean availability sheet (2 weekday bikes), and the third-fastest run at
   250 s. Its weaknesses are all plan-side and real — HIIT from day 1 with no base
   against its own falling-CTL bootstrap, an invented "summer overload" in its
   history reconstruction, and the field's latest FTP test (day 68, softened by its
   zones-only prescriptions). A C+ plan structure under an A+ adaptation still wins,
   because the plan flaws cost fitness at the margin while the adaptation flaws
   everyone else made cost the athlete the two things they actually asked for.

2. **gpt-5.6-sol-pro** — the no-weak-dimension model. Second-best adaptation
   (7 changed sessions, re-sequencing its FTP test
   off the group-ride day with recovery placed around it), near-perfect availability
   (1 weekday bike, 0 over-duration), sane load (6.8 h), clean 6-block structure,
   sensibly early test. Its only misses are the app-assisted A-race gap and a wedding
   that its own short first block put out of reach. It out-ranks the two Claude
   planners because it did both halves of the job well rather than one half
   perfectly.

3. **claude-opus-5** — the best 14-week plan in the field, full stop: the only model
   to derive its weekly shape from the athlete's per-day equipment calendar, both
   race days scheduled, the only reasoned FTP-test placement (stated trigger, explicit
   no-retest window before the race), availability 2/2, and the closest full-week
   volume to the 8 h target (7.5 h) with a textbook deload rhythm. Then `workout
   adapt` returned "no changes" in 24 s without naming the group ride sitting inside
   its window, after the CLI told it the wedding displaces ~52 % of a week's load. If
   the benchmark were planning only, it wins; it isn't.

4. **claude-sonnet-5** — the cleanest execution sheet in the field: zero bikes on
   gym-only days, one over-duration session, both race days, a dedicated B-race
   mini-taper block, and a defensible post-B-race re-anchor test (day 44). It sits
   below opus-5 on plan richness (6.7 h undershoots the budget, 59-word vs 22-word
   protocols cuts both ways) and shares the same F: an inert adapt that never
   mentioned either constraint.

5. **grok-4.5** — the third real adaptation (5 changes, and the most realistic
   athlete guidance in the set: "Expect surges you won't fully control—don't chase
   every attack"). The plan underneath is mid-pack at best: one of three models whose
   blocks roll straight past the B-race, the only model whose load *rises* into the
   B-race week, 14 weekday bikes and 6 over-duration sessions. Adaptation quality
   pulls it above better planners; plan flaws keep it out of the top three.

6. **gpt-5.6** — solid everywhere, spectacular nowhere: 8 blocks with a dedicated
   B-tune week, day-12 test, compliant travel week, and a modest but sensible
   2-change adaptation (it converted the day into a social recovery ride — thereby
   exposing app bug #2, which is the app's fault, not the model's). The 14 weekday
   bikes and missing A-day are the gap to the top five.

7. **gpt-5.6-terra-pro** — the best pure plan of the gpt family: 9 blocks including
   a B-tune week *and* a post-B recovery week, availability 2/0, day-5 test, 7.3 h.
   All of it wasted at the second hurdle: ~108k prompt tokens to conclude "no changes"
   with the group ride in its window. The plan alone would rank top-four; the F adapt
   costs it three places.

8. **gpt-5.6-sol** — a competent plan (8 blocks, B-tune, day-9 test, 7.5 h) and a
   minimal-but-correct adaptation (1 change: raised the group-ride day's expected
   intensity instead of pretending it's recovery). The 24 weekday bikes — third worst
   in the field — are what hold it here.

9. **deepseek-v4-pro** — the only hard date error in the benchmark: the B-race
   scheduled on Saturday Sep 26 with a recovery spin on the actual race day. Its
   adaptation was fine (2 changes, long ride moved onto the group ride) and its
   travel week compliant, but a coach that sends the athlete to the start line a day
   early cannot rank above one that doesn't, whatever else it does. The lightest load
   in the field (6.0 h) with a three-week fade into the A-race compounds it.

10. **kimi-k3** — the benchmark's sharpest quality-vs-discipline split. The plan
    content is the richest anywhere: both race days, day-4 test, a Nov 7 dress
    rehearsal, cold/wet-kit fondo simulations, an exemplary bookended travel week.
    The execution numbers are the worst anywhere: 27 weekday bikes, 15 over-duration
    sessions, 11 rest days in 14 weeks, 9.1 h against its own stated volume promise —
    and a constraint-blind adapt despite being one of only three models with the
    wedding inside its window. Second-slowest end to end (~38 min). It ranks above
    the bottom four because what it gets right (race days, testing, travel) they
    mostly get wrong; it ranks no higher because an athlete cannot ride the plan as
    written.

11. **gemini-3.7-flash** — near-perfect availability (1/0), coherent load, and 20×
    faster than the slowest model, but three outright failures: the field's only
    travel violation (a 75-min outdoor gravel ride on the last no-bike day), watt
    targets in 31 of 42 cycling sessions with no FTP test all season, and an inert
    adapt as one of only three models that could even see the wedding. Speed is the
    product argument for flash; this scenario shows what it costs.

12. **claude-opus-4.8** — both race days and tidy load management (7.0 h, sane
    rhythm), but the plan is un-executable twice a week — 25 bike sessions on
    gym-only days, second worst in the field — its block structure rolls past the
    B-race with no acknowledgement, it prescribes watts for 14 weeks without ever
    testing the anchor, and its 6-second adapt changed nothing. Flash edges it on the
    grounds that flash's plan can at least be ridden as written on 13 of 14 weeks'
    days; opus-4.8's can't on any full week.

13. **qwen3.8-max** — the self-contradiction case: writes "no full taper" for the
    B-event and then plans a textbook two-week one, opens a 14-week autumn plan with
    a SIT block on reasoning ("anaerobic residuals decay quickly") that argues for
    the opposite placement, and ends with a zero-width race block. Add 17 weekday
    bikes with 7 over-duration sessions, no FTP test, the worst post-adapt wedding
    load (255 TSS), a near-blind 1-change adapt, and the slowest run (~39 min). Both
    race days present and a compliant (if aerobic-free) travel week keep it off the
    bottom.

14. **gpt-5.6-terra** — last of the finishers not for one disqualifying error but
    for missing on every dimension that matters at once: 26 weekday bikes, 8.9 h
    against the 8 h budget with six weeks at 460+ TSS, no FTP test behind its watt
    targets, no coach learnings saved at bootstrap, an inert adapt, and the
    heaviest-but-one wedding load (240 TSS). Its B-taper block and speed (205 s) are
    the only entries on the credit side.

15. **glm-5.2** — two attempts, two different structural failures at the same
    14-week generation step, no schedule ever produced. See the section above.

