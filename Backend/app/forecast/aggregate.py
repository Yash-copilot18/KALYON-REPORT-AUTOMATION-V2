"""
Roll the daily forecast series up into calendar weeks and months.

No model is trained per period. A weekly total is the sum of that week's daily
figures and nothing else, because the forecast is produced daily and inventing
a second model for the same quantity would mean two numbers that disagree.

The reason this is not a one-line groupby is that a calendar period is rarely
full. The plant's record has gaps, incomplete days are excluded upstream, and a
week that straddles the last observed day holds measured days on one side and
predicted days on the other. Summing those without saying so produces a bar
that looks like a week's generation and is in fact three days of it, sitting
next to a prediction for all seven. Every period here therefore carries its own
day counts, and a comparison is offered only when both sides cover the whole
period.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import Any, Dict, Iterable, List, Optional

BUCKETS = ("day", "week", "month")


def _as_date(value: Any) -> Optional[date]:
    """Accept what the API layer holds: date, datetime, or ISO string."""
    if value is None:
        return None
    if isinstance(value, date) and not hasattr(value, "hour"):
        return value
    if hasattr(value, "date") and callable(value.date):
        return value.date()
    text = str(value)
    if "T" in text:
        text = text.split("T", 1)[0]
    text = text.strip()[:10]
    try:
        y, m, d = (int(part) for part in text.split("-"))
        return date(y, m, d)
    except (ValueError, TypeError):
        return None


def period_bounds(d: date, bucket: str) -> tuple:
    """First and last calendar day of the period `d` falls in."""
    if bucket == "day":
        return d, d
    if bucket == "week":
        start = d - timedelta(days=d.weekday())        # Monday
        return start, start + timedelta(days=6)        # Sunday
    if bucket == "month":
        last = calendar.monthrange(d.year, d.month)[1]
        return date(d.year, d.month, 1), date(d.year, d.month, last)
    raise ValueError(f"unknown bucket {bucket!r}")


def period_label(start: date, end: date, bucket: str) -> str:
    if bucket == "day":
        return start.isoformat()
    if bucket == "week":
        return f"{start.strftime('%d %b')} - {end.strftime('%d %b')}"
    return start.strftime("%b %Y")


def _sum_or_none(values: List[float]) -> Optional[float]:
    """Sum of what is present. An empty period is unknown, never zero."""
    return float(sum(values)) if values else None


def aggregate_points(points: Iterable[Dict[str, Any]], bucket: str
                     ) -> List[Dict[str, Any]]:
    """
    Group daily forecast points into `bucket` periods.

    Each returned period states how much of itself it actually covers, so the
    caller never has to guess whether a total is whole. Energy is summed, never
    averaged: a week is seven days of generation, not a daily rate.
    """
    if bucket not in BUCKETS:
        raise ValueError(f"unknown bucket {bucket!r}")

    groups: Dict[date, List[Dict[str, Any]]] = {}
    for p in points:
        d = _as_date(p.get("date"))
        if d is None:
            continue                       # undated row cannot be placed in a period
        start, _ = period_bounds(d, bucket)
        groups.setdefault(start, []).append({**p, "_d": d})

    out: List[Dict[str, Any]] = []
    for start in sorted(groups):
        rows = sorted(groups[start], key=lambda r: r["_d"])
        _, end = period_bounds(start, bucket)
        days_in_period = (end - start).days + 1

        actuals = [float(r["actual_mwh"]) for r in rows
                   if r.get("actual_mwh") is not None]
        preds = [float(r["predicted_mwh"]) for r in rows
                 if r.get("predicted_mwh") is not None]

        actual = _sum_or_none(actuals)
        predicted = _sum_or_none(preds)

        actual_complete = len(actuals) == days_in_period
        predicted_complete = len(preds) == days_in_period

        # The band is summed only when every single day of the period carries
        # both bounds. A partial sum of bounds is not a bound on anything.
        lowers = [r.get("lower_mwh") for r in rows]
        uppers = [r.get("upper_mwh") for r in rows]
        bounded = (predicted_complete
                   and len(lowers) == days_in_period
                   and all(v is not None for v in lowers)
                   and all(v is not None for v in uppers))
        lower = float(sum(float(v) for v in lowers)) if bounded else None
        upper = float(sum(float(v) for v in uppers)) if bounded else None

        # A deviation is only meaningful between two whole periods. Comparing
        # three measured days against seven predicted ones would read as a
        # 57% shortfall that never happened.
        deviation = deviation_pct = None
        if actual_complete and predicted_complete and actual is not None:
            deviation = actual - predicted
            deviation_pct = (100.0 * deviation / predicted) if predicted else None

        statuses = [r.get("status") for r in rows if r.get("status")]
        out.append({
            "bucket": bucket,
            "date": start.isoformat(),          # chart x-key, as the day view uses
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "label": period_label(start, end, bucket),
            "predicted_mwh": round(predicted, 2) if predicted is not None else None,
            "lower_mwh": round(lower, 2) if lower is not None else None,
            "upper_mwh": round(upper, 2) if upper is not None else None,
            "actual_mwh": round(actual, 2) if actual is not None else None,
            "deviation_mwh": round(deviation, 2) if deviation is not None else None,
            "deviation_pct": round(deviation_pct, 2) if deviation_pct is not None else None,
            "days_in_period": days_in_period,
            "days_present": len(rows),
            "days_with_actual": len(actuals),
            "days_with_predicted": len(preds),
            "actual_complete": actual_complete,
            "predicted_complete": predicted_complete,
            "comparable": bool(actual_complete and predicted_complete),
            "is_backtest": bool(rows) and all(r.get("is_backtest") for r in rows),
            "has_forecast_days": any(not r.get("is_backtest") for r in rows),
            "status": statuses[-1] if statuses else None,
            "interval_method": "sum of daily bounds" if bounded else None,
        })
    return out


#: Said once, in the payload, rather than implied by a shaded area.
INTERVAL_CAVEAT = (
    "Period bounds are the sum of the daily bounds. That is the widest honest "
    "reading - it assumes every day misses in the same direction at once. The "
    "daily errors are in fact correlated, because each forecast day is produced "
    "from the previous day's prediction, but by how much has never been "
    "measured, so the conservative sum is used rather than a narrower figure "
    "that would have to be assumed."
)

AGGREGATION_NOTE = {
    "day": ("One point per day. Points marked is_backtest were predicted by a "
            "model that had not seen that day."),
    "week": ("Calendar weeks, Monday to Sunday. Energy is summed, not averaged. "
             "A period is only compared against its prediction when both cover "
             "all seven days; partial periods are shown with their day count "
             "and no deviation."),
    "month": ("Calendar months. Energy is summed, not averaged, and month "
              "lengths differ. A period is only compared against its prediction "
              "when both cover the whole month; partial periods are shown with "
              "their day count and no deviation."),
}
