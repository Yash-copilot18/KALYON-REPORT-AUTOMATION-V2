# app/services/intervals.py
"""
Single source of truth for interval / aggregation behaviour.

SCADA rule: **instant telemetry is never aggregated.** A 1-minute interval is the
plant's native sample rate, so it is served as raw records straight from the table.
Every coarser interval buckets the raw samples and aggregates them (Average by
default).

Import `is_instant()` / `effective_agg()` here instead of re-testing the interval
string — that check used to be duplicated across the repository, the merged-page
service, and each Excel exporter, which is how they drift apart.
"""

from typing import Any, Optional

# Intervals that return raw records. "raw" is every stored sample; "1min" is the
# plant's native cadence — both are instant data and must not be aggregated.
INSTANT_INTERVALS = frozenset({"raw", "1min"})

# Aggregation applied to every non-instant interval unless the caller picks another.
DEFAULT_AGG = "avg"


def norm(interval: Any) -> str:
    """Accept a str or an IntervalEnum and return the plain interval string."""
    return getattr(interval, "value", interval) or ""


def is_instant(interval: Any) -> bool:
    """True when the interval is instant telemetry (raw records, no aggregation)."""
    return norm(interval) in INSTANT_INTERVALS


def effective_agg(interval: Any, agg: Any = None) -> Optional[str]:
    """The aggregation to apply — None for instant intervals, else the chosen one."""
    if is_instant(interval):
        return None
    return getattr(agg, "value", agg) or DEFAULT_AGG


def agg_label(interval: Any, agg: Any = None) -> str:
    """Human-readable aggregation for report metadata (CSV/Excel headers)."""
    resolved = effective_agg(interval, agg)
    return "Not applicable (instant data)" if resolved is None else resolved.upper()


def interval_label(interval: Any) -> str:
    """Human-readable interval for report metadata."""
    value = norm(interval)
    labels = {
        "raw":     "Raw (all records)",
        "1min":    "1 Minute (Instant Data)",
        "5min":    "5 Minutes",
        "15min":   "15 Minutes",
        "30min":   "30 Minutes",
        "hourly":  "Hourly",
        "daily":   "Daily",
        "monthly": "Monthly",
    }
    return labels.get(value, value.title() if value else "")
