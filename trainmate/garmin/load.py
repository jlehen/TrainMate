"""Training-load math: measured/HR/RPE TSS, per-activity load, RPE divergence."""
import math
import time
from typing import Any, Dict, Optional, Tuple

from trainmate.config import config

# --- Power-zone TSS weights (TSS per second in each zone) -------------------
# Derived from Dr. Andrew Coggan's power-zone model (Allen & Coggan, "Training
# and Racing with a Power Meter"). TSS over a steady effort is IF^2 * 100 per
# hour, so each zone's representative Intensity Factor (IF = fraction of FTP)
# gives a per-second rate of (IF^2 * 100) / 3600. Garmin records power in this
# native Coggan 7-zone model:
#     Z1 Active Recovery   <55% FTP   IF~0.50 -> 0.0069
#     Z2 Endurance       56-75%       IF~0.65 -> 0.0117
#     Z3 Tempo           76-90%       IF~0.80 -> 0.0178
#     Z4 Lactate Thresh  91-105%      IF~0.95 -> 0.0250
#     Z5 VO2max          106-120%     IF~1.10 -> 0.0333
#     Z6 Anaerobic       121-150%     IF~1.35 -> 0.0506
#     Z7 Neuromuscular   >150%        IF~1.60 -> 0.0711  (open-ended; IF capped
#                                                         at 1.60 as a sane max)
POWER_ZONE_TSS_PER_SEC = (0.0069, 0.0117, 0.0178, 0.0250, 0.0333, 0.0506, 0.0711)
# --- Heart-rate-zone TSS weights (TSS per second in each zone) --------------
# Based on Joe Friel's hrTSS methodology ("The Cyclist's Training Bible"):
# time spent in each HR zone, scaled to Lactate Threshold Heart Rate (LTHR),
# is assigned a baseline TSS/hour rate per zone. Expressed here per second
# across Garmin's 5-zone HR model (Z1 recovery -> Z5 above threshold).
HR_ZONE_TSS_PER_SEC = (0.0055, 0.0111, 0.0166, 0.0222, 0.0277)
def _zone_tss(
    zone_sec: Dict[str, Any], prefix: str, weights: Tuple[float, ...]
) -> Optional[float]:
    """Weighted sum of seconds-in-zone. None when no zone carries positive
    time (i.e. the data is absent), so callers can fall through the hierarchy."""
    total = 0.0
    have_data = False
    for i, weight in enumerate(weights, start=1):
        secs = zone_sec.get(f"{prefix}{i}_sec")
        if secs:
            total += float(secs) * weight
            have_data = True
    return round(total, 1) if have_data else None
def _hr_zone_coverage(hr_zone_sec: Dict[str, Any], duration_sec: float) -> float:
    """Fraction of the activity recorded inside any HR zone (0.0 when unknown)."""
    if not duration_sec:
        return 0.0
    total = sum(float(hr_zone_sec.get(f"zone{i}_sec") or 0) for i in range(1, 6))
    return total / duration_sec
def _rpe_tss(rpe: float, duration_sec: float) -> float:
    """Session-RPE (sRPE) TSS estimate. Foster (2001), "A new approach to
    monitoring exercise training", validated for resistance work by Sweet et
    al. (2004). Maps the Borg CR-10 1-10 scale to duration: (RPE*10) per hour."""
    return round((rpe * 10.0) * (duration_sec / 3600.0), 1)
def measured_tss(
    power_zone_sec: Dict[str, Any], hr_zone_sec: Dict[str, Any]
) -> Optional[float]:
    """The objective Training Stress Score actually recorded: power TSS when a
    power meter was present, else hrTSS. None when neither was recorded. This is
    a pure measurement (no coverage gate, no RPE) and is what we store in the
    `tss` column; the training-load fallback is computed separately on the fly."""
    power = _zone_tss(power_zone_sec, "power_zone", POWER_ZONE_TSS_PER_SEC)
    if power is not None:
        return power
    return _zone_tss(hr_zone_sec, "zone", HR_ZONE_TSS_PER_SEC)
def compute_load(
    power_zone_sec: Dict[str, Any],
    hr_zone_sec: Dict[str, Any],
    rpe: Optional[float],
    duration_sec: float,
) -> Tuple[float, str, Optional[str]]:
    """Training load via a best-available fallback (no estimated RPE, no max-ing):

        1. Power TSS                       (Coggan 7-zone)   -> method "power"
        2. hrTSS, if HR coverage >= MIN    (Friel 5-zone)    -> method "hr"
        3. Session RPE * duration          (Foster sRPE)     -> method "rpe"
           (used when power is absent and HR is missing or too sparse to trust)

    When method 3 should apply but the user entered no RPE, we keep the weak
    hrTSS (or 0) and return a warning string so callers can surface it.

    Returns (load, method, warning). `method` identifies the source; `warning`
    is a human reason when we fell back to an unreliable estimate for lack of a
    user RPE, else None. RPE is user-entered only and never synthesised.
    """
    power = _zone_tss(power_zone_sec, "power_zone", POWER_ZONE_TSS_PER_SEC)
    if power is not None:
        return power, "power", None

    hr = _zone_tss(hr_zone_sec, "zone", HR_ZONE_TSS_PER_SEC)
    if hr is not None:
        # Trust hrTSS only when the HR zones cover enough of the session; sparse
        # coverage means the effort sat below zone 1 (low-intensity work) and
        # hrTSS undercounts, so prefer the user's RPE when available.
        if _hr_zone_coverage(hr_zone_sec, duration_sec) >= config.hr_zone_coverage_min:
            return hr, "hr", None
        if rpe:
            return _rpe_tss(float(rpe), duration_sec), "rpe", None
        return hr, "hr_sparse", "low HR-zone coverage and no RPE entered"

    if rpe:
        return _rpe_tss(float(rpe), duration_sec), "rpe", None
    return 0.0, "none", "no power, HR, or RPE data"
def _has_power_zones(act: Dict[str, Any]) -> bool:
    return any(act.get(f"power_zone{i}_sec") for i in range(1, 8))
def _measurement_is_load(act: Dict[str, Any], duration_sec: float) -> bool:
    """True when the stored `tss` measurement is the load (came from power, or
    from HR with adequate coverage), rather than being overridden by RPE."""
    if _has_power_zones(act):
        return True
    has_hr = any(act.get(f"zone{i}_sec") for i in range(1, 6))
    if not has_hr:
        return True  # no zone data to second-guess the stored measurement
    return _hr_zone_coverage(act, duration_sec) >= config.hr_zone_coverage_min
def _divergence_ratio(act: Dict[str, Any], duration_sec: float) -> Optional[float]:
    """Raw sRPE / measured-TSS ratio when the stored `tss` is a trustworthy
    measurement (power, or adequately-covered HR) and the user entered an RPE;
    else None. Shared basis for both inflating the load and flagging divergence,
    so the two always agree on when the meters under-counted real strain."""
    rpe = act.get("rpe")
    tss = act.get("tss")
    if not rpe or not tss or float(tss) <= 0:
        return None
    if not _measurement_is_load(act, duration_sec):
        return None  # measurement isn't the load; RPE already wins, no divergence
    return _rpe_tss(float(rpe), duration_sec) / float(tss)
def _divergence_threshold() -> float:
    return config.rpe_divergence_ratio
def activity_load(act: Dict[str, Any]) -> float:
    """Training load for a stored activity row, derived on the fly. Reads the
    objective measurement from the `tss` column (power TSS or hrTSS) and applies
    the fallback: trust it when it came from power or adequately-covered HR;
    otherwise prefer the user's RPE (sRPE), keeping the weak measurement only
    when no RPE was entered. RPE-only when there is no measurement at all.

    When the measurement is trustworthy but the user's RPE implies a materially
    higher load (>= `rpe_divergence_ratio`), the load is taken from RPE instead:
    the meters under-counted real strain the body paid for (strength/resistance
    work, HIIT, heat, sleep debt). `rpe_divergence` flags the same activities so
    the bump can be explained to the user."""
    tss = act.get("tss")
    rpe = act.get("rpe")
    duration_sec = act.get("duration_sec") or 0.0
    if tss is not None:
        if not _measurement_is_load(act, duration_sec):
            return _rpe_tss(float(rpe), duration_sec) if rpe else float(tss)
        ratio = _divergence_ratio(act, duration_sec)
        if ratio is not None and ratio >= _divergence_threshold():
            return _rpe_tss(float(rpe), duration_sec)
        return float(tss)
    if rpe:
        return _rpe_tss(float(rpe), duration_sec)
    return 0.0
def load_method(act: Dict[str, Any]) -> str:
    """Where `activity_load` took this row's number from — the provenance that is
    currently invisible everywhere (DESIGN_intensity_distribution.md §9.7):

        power           | Coggan power zones
        hr              | hrTSS, HR coverage adequate
        rpe             | coverage poor, load rescued from the athlete's RPE
        hr_sparse       | coverage poor AND no RPE — the load number is undercounted too
        rpe_divergence  | measurement trustworthy, but RPE implied materially more
                          strain, so the load came from RPE anyway
        measured        | a stored TSS with no zone columns to attribute it to
        none            | no power, HR or RPE

    Kept beside `activity_load` and branching identically, so the tag a surface prints
    can never name a provenance that is not the provenance of the number beside it.
    `rpe_divergence` is a path `compute_load` has no word for: without it those rows
    would read `hr` above a figure hrTSS never produced.
    """
    tss = act.get("tss")
    rpe = act.get("rpe")
    duration_sec = act.get("duration_sec") or 0.0
    if tss is None:
        return "rpe" if rpe else "none"
    if not _measurement_is_load(act, duration_sec):
        return "rpe" if rpe else "hr_sparse"
    ratio = _divergence_ratio(act, duration_sec)
    if ratio is not None and ratio >= _divergence_threshold():
        return "rpe_divergence"
    if _has_power_zones(act):
        return "power"
    if any(act.get(f"zone{i}_sec") for i in range(1, 6)):
        return "hr"
    return "measured"


def rpe_divergence(act: Dict[str, Any]) -> Optional[float]:
    """If the load came from an objective measurement (power/HR) but the user's
    RPE implied a materially higher load, returns the ratio sRPE_load / measured;
    else None. Flags strain the meters miss (resistance work, heat, sleep debt,
    muscular damage), e.g. kettlebell HIIT. When this fires, `activity_load` has
    taken the load from RPE; the ratio explains by how much the meters fell short.
    The threshold is config `rpe_divergence_ratio`."""
    duration_sec = act.get("duration_sec") or 0.0
    ratio = _divergence_ratio(act, duration_sec)
    if ratio is None:
        return None
    return round(ratio, 2) if ratio >= _divergence_threshold() else None
def _safe_round(value: Any, ndigits: int = 1) -> float:
    try:
        return round(float(value), ndigits)
    except (ValueError, TypeError):
        return 0.0
