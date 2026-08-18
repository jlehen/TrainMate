TRAINING ZONES & EFFORT VOCABULARY
==================================

AUTHORITY: VOCABULARY. This file defines what zone and effort terms mean and
how zone data is recorded. It prescribes nothing: which zones a session should
target is a plan document's decision. The rules marked FLOOR are properties of
the measurement and never yield.


1. WHERE ZONE DATA COMES FROM
-----------------------------
All time-in-zone data is bucketed BY GARMIN, at recording time, against the
zone boundaries configured in the athlete's Garmin profile. There is no raw
stream to re-bucket afterwards, so the tables below are true only while that
profile keeps the boundaries stated here (the README's Garmin setup section
pins them).

   - After a benchmark updates an anchor (FTP, LTHR — benchmarks.txt), the new
     value must be entered in Garmin. That is the one sanctioned boundary
     move: until it happens, every new activity is bucketed against the stale
     anchor.
   - FLOOR: never reinterpret or re-band recorded zone seconds. If boundaries
     were wrong when an activity was recorded, its zone data is suspect —
     report that, do not correct for it.

Time can also fall BELOW zone 1 (rest between strength sets, easy walking on
a hike, a chairlift): those seconds belong to no zone. Low zone coverage on
such sports is the sport's nature, not a recording failure.


2. CYCLING POWER ZONES (7 zones, % of FTP)
------------------------------------------
The Coggan model, which is also Garmin's default power banding.

   Zone  Name           % FTP    Per-interval RPE  Primary adaptation
   1     recovery       < 55     1-2               Metabolite clearance; negligible training load.
   2     endurance      56-75    2-3               Mitochondrial density, capillarization, glycogen storage.
   3     tempo          76-90    4-5               Aerobic efficiency, glycogen sparing.
   4     threshold      91-105   6-7               Lactate clearance and buffering capacity.
   5     VO2max         106-120  8-9               Stroke volume, maximal cardiac output, VO2max.
   6     anaerobic      121-150  9-10              Glycolytic capacity, acidosis tolerance.
   7     neuromuscular  > 150    10 (all-out)      ATP-PC power, neuromuscular recruitment.

Zones are measurement vocabulary, not targets. A plan may deliberately aim a
session at a band offset from a zone boundary (e.g. a threshold block hedging
below FTP); the plan's stated target governs, not the zone name nearest to it.


3. HEART RATE ZONES (5 zones, % of LTHR)
----------------------------------------
Garmin's 5-zone model, anchored to lactate threshold heart rate (LTHR) at
Garmin's default bands. Running and any sport without a power meter record in
this model.

   Zone  Name       % LTHR   Meaning
   1     recovery   60-70    Warm-up / active recovery.
   2     aerobic    70-80    Low-intensity aerobic base.
   3     tempo      80-90    Moderate aerobic / tempo.
   4     threshold  90-100   At or near lactate threshold.
   5     VO2max+    > 100    All severe work, undifferentiated (see §4).

   (Below 60% of LTHR: no zone — the unbucketed time of §1.)


4. MAPPING BETWEEN THE TWO MODELS
---------------------------------
When a plan speaks sport-generically, the models correspond by name:

   power 1-2  (recovery, endurance)  <->  HR 1-2  (recovery, aerobic)   low intensity
   power 3    (tempo)                <->  HR 3    (tempo)               moderate
   power 4    (threshold)            <->  HR 4    (threshold)           at/near threshold
   power 5-7  (VO2max and above)     <->  HR 5    (VO2max+)             severe

The last row collapses only one way. FLOOR: HR zone 5 time never identifies
WHICH severe system was trained — VO2max, anaerobic and sprint work all pile
into it, because heart rate both lags and ceilings there. Only power tells
them apart.

Swimming uses neither model: zones are pace offsets from CSS (benchmarks.txt §2).


5. POWER vs HEART RATE
----------------------
   - Power is instantaneous, repeatable, and unaffected by heat, hydration or
     fatigue. It is the currency for pacing and for reading short intervals.
   - Heart rate lags the effort by 1-3 minutes and drifts upward with heat,
     dehydration, caffeine, poor sleep and accumulated duration. FLOOR: never
     judge an interval shorter than ~3 minutes by heart rate.
   - Together they give a diagnostic neither gives alone: rising HR at
     constant power (decoupling) signals aerobic fatigue or dehydration.
   - When both are recorded, training load is computed from power
     (training_load.txt).


6. EFFORT RATINGS: PER-INTERVAL RPE vs SESSION RPE
--------------------------------------------------
Two different quantities share the 1-10 scale. Never validate one against the
other.

   PER-INTERVAL RPE — reserve-anchored ("how much is left in the tank")
   DURING an effort; the scale plan documents use for intensity targets
   (threshold 6-7, HIIT 8-9, SIT 10). At constant output it drifts up with
   accumulated duration — a long zone-2 ride can end at 7-8/10 without ever
   leaving zone 2. FLOOR: never infer a zone or an intensity from a
   late-session or whole-session rating.

   SESSION RPE (sRPE) — ONE post-session rating of the whole session,
   multiplied by duration to estimate training load (~RPE x 10 per hour, on
   the TSS scale). It is the load estimate when power and heart rate are
   absent or too sparse to trust — the normal case for strength work, where
   heart rate reflects little of the mechanical work done and much of the
   clock is rest between sets. An RPE should be entered for every strength
   session. sRPE is athlete-entered only; it is never estimated on the
   athlete's behalf.
