"""Is today's recovery metric normal for this athlete?

The thresholds are coaching judgment, not presentation: HRV counts as suppressed one
standard deviation below the rolling mean, resting HR as elevated above the mean plus
at least 3 bpm (a floor, so a very steady athlete's tiny standard deviation doesn't
flag ordinary noise), and a sleep score below 60 as poor regardless of baseline.

They lived hand-coded inside two render loops — `data show-metrics` and `status` — and
each display then decided its own colour and glyph from them. Callers now ask for the
verdict and keep only the formatting.
"""
from typing import Any, Dict, Optional

# A resting-HR rise smaller than this is noise even for an athlete whose day-to-day
# spread is narrower than it.
RHR_MIN_ELEVATED_MARGIN_BPM = 3.0

# Absolute, not baseline-relative: a bad night is a bad night.
POOR_SLEEP_SCORE = 60

# Used when a baseline exists but its spread is missing or zero.
_DEFAULT_STD = 1.0

NORMAL = "normal"
SUPPRESSED = "suppressed"   # below the expected range (HRV, sleep)
ELEVATED = "elevated"       # above the expected range (RHR)
UNKNOWN = "unknown"         # no reading, or nothing to compare it against


def classify_metric(
    kind: str, value: Optional[float], baseline: Optional[Dict[str, Any]] = None
) -> str:
    """Grades one reading against the athlete's rolling baseline.

    `kind` is 'hrv', 'rhr' or 'sleep'. Returns NORMAL, SUPPRESSED, ELEVATED or
    UNKNOWN — never a colour or a glyph, which are the caller's business.
    """
    if value is None:
        return UNKNOWN

    if kind == "sleep":
        # Deliberately baseline-free, so a poor night still reads as poor for an
        # athlete who habitually sleeps badly.
        return SUPPRESSED if value < POOR_SLEEP_SCORE else NORMAL

    if kind == "hrv":
        mean = (baseline or {}).get("hrv_baseline_mean")
        if mean is None:
            return UNKNOWN
        std = (baseline or {}).get("hrv_baseline_std") or _DEFAULT_STD
        return SUPPRESSED if value < (mean - std) else NORMAL

    if kind == "rhr":
        mean = (baseline or {}).get("rhr_baseline_mean")
        if mean is None:
            return UNKNOWN
        std = (baseline or {}).get("rhr_baseline_std") or _DEFAULT_STD
        margin = max(RHR_MIN_ELEVATED_MARGIN_BPM, std)
        return ELEVATED if value > (mean + margin) else NORMAL

    raise ValueError(f"unknown metric kind: {kind!r}")


def is_anomalous(verdict: str) -> bool:
    """True when the verdict is something the athlete should notice."""
    return verdict in (SUPPRESSED, ELEVATED)
