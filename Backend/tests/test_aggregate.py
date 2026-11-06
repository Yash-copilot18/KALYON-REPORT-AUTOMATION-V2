# -*- coding: utf-8 -*-
"""
Checks for the week/month rollup.

The arithmetic is trivial; what is worth testing is everything around it -
periods that are only half there, months of different lengths, years turning
over, days that are simply absent, and the refusal to compare a partial actual
against a whole prediction. Those are the cases that quietly produce a chart
that looks right and says something false.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.forecast import aggregate as AGG   # noqa: E402

_passed = 0
_failed = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %s%s" % (label, ("   " + str(detail)) if detail else ""))
    else:
        _failed += 1
        print("  FAIL  %s   %s" % (label, detail))


def pt(d, predicted=None, actual=None, lower=None, upper=None, backtest=False):
    return {"date": d if isinstance(d, str) else d.isoformat(),
            "predicted_mwh": predicted, "actual_mwh": actual,
            "lower_mwh": lower, "upper_mwh": upper,
            "deviation_mwh": None, "deviation_pct": None,
            "status": None, "is_backtest": backtest}


def full_week(monday, predicted=10.0, actual=None, lower=None, upper=None):
    return [pt(monday + timedelta(days=i), predicted, actual, lower, upper)
            for i in range(7)]


print("=" * 70)
print("PERIOD BOUNDARIES")
print("=" * 70)

# A Wednesday must land in the Monday-Sunday week that contains it.
s, e = AGG.period_bounds(date(2025, 6, 4), "week")
check("week runs Monday to Sunday", (s, e) == (date(2025, 6, 2), date(2025, 6, 8)),
      "%s..%s" % (s, e))

s, e = AGG.period_bounds(date(2025, 6, 2), "week")
check("a Monday is its own week start", s == date(2025, 6, 2))

s, e = AGG.period_bounds(date(2025, 6, 8), "week")
check("a Sunday closes the week it ends", (s, e) == (date(2025, 6, 2), date(2025, 6, 8)))

s, e = AGG.period_bounds(date(2025, 2, 14), "month")
check("February 2025 has 28 days", (e - s).days + 1 == 28)

s, e = AGG.period_bounds(date(2024, 2, 14), "month")
check("February 2024 has 29 days", (e - s).days + 1 == 29, "leap year")

s, e = AGG.period_bounds(date(2025, 7, 15), "month")
check("July has 31 days", (e - s).days + 1 == 31)

# A week straddling New Year must not be split into two.
s, e = AGG.period_bounds(date(2025, 1, 1), "week")
check("a week may cross a year boundary", (s, e) == (date(2024, 12, 30), date(2025, 1, 5)),
      "%s..%s" % (s, e))

print()
print("=" * 70)
print("SUMMING, NOT AVERAGING")
print("=" * 70)

rows = AGG.aggregate_points(full_week(date(2025, 6, 2), predicted=10.0, actual=12.0), "week")
check("one full week produces one period", len(rows) == 1)
check("weekly predicted is the sum of its days", rows[0]["predicted_mwh"] == 70.0,
      rows[0]["predicted_mwh"])
check("weekly actual is the sum of its days", rows[0]["actual_mwh"] == 84.0,
      rows[0]["actual_mwh"])
check("a full week is marked complete on both sides",
      rows[0]["actual_complete"] and rows[0]["predicted_complete"])
check("a full week is comparable", rows[0]["comparable"])
check("deviation is actual minus predicted", rows[0]["deviation_mwh"] == 14.0,
      rows[0]["deviation_mwh"])
check("deviation percent is against predicted", rows[0]["deviation_pct"] == 20.0,
      rows[0]["deviation_pct"])

# 30 days of June at 10 MWh is 300, not 10 and not 70.
june = [pt(date(2025, 6, 1) + timedelta(days=i), 10.0, 10.0) for i in range(30)]
rows = AGG.aggregate_points(june, "month")
check("a 30-day month sums to 30 days", rows[0]["predicted_mwh"] == 300.0,
      rows[0]["predicted_mwh"])
check("month length is taken from the calendar", rows[0]["days_in_period"] == 30)
check("a full month is comparable", rows[0]["comparable"])

# Month lengths must not be assumed equal.
july = [pt(date(2025, 7, 1) + timedelta(days=i), 10.0, 10.0) for i in range(31)]
rows = AGG.aggregate_points(june + july, "month")
check("two months of unequal length both complete",
      [r["days_in_period"] for r in rows] == [30, 31],
      [r["days_in_period"] for r in rows])
check("each month keeps its own total",
      [r["actual_mwh"] for r in rows] == [300.0, 310.0],
      [r["actual_mwh"] for r in rows])

print()
print("=" * 70)
print("PARTIAL PERIODS ARE NOT COMPARED")
print("=" * 70)

# Three measured days against seven predicted ones: the trap this exists for.
mixed = full_week(date(2025, 6, 2), predicted=10.0)
for i in range(3):
    mixed[i]["actual_mwh"] = 12.0
rows = AGG.aggregate_points(mixed, "week")
r = rows[0]
check("partial actual is still totalled", r["actual_mwh"] == 36.0, r["actual_mwh"])
check("partial actual is flagged incomplete", r["actual_complete"] is False)
check("the day count is reported", (r["days_with_actual"], r["days_in_period"]) == (3, 7),
      "%d of %d" % (r["days_with_actual"], r["days_in_period"]))
check("NO deviation is offered for a partial period", r["deviation_mwh"] is None)
check("NO deviation percent either", r["deviation_pct"] is None)
check("the period is not comparable", r["comparable"] is False)

# A month with one day in it must not read as a month.
rows = AGG.aggregate_points([pt(date(2025, 5, 31), 10.0, 11.0)], "month")
check("a one-day month is incomplete", rows[0]["actual_complete"] is False)
check("a one-day month offers no deviation", rows[0]["deviation_mwh"] is None)
check("a one-day month still reports its total", rows[0]["actual_mwh"] == 11.0)

print()
print("=" * 70)
print("MISSING DAYS ARE MISSING, NOT ZERO")
print("=" * 70)

# Drop Wednesday and Thursday entirely, as an excluded incomplete day would be.
gapped = [p for p in full_week(date(2025, 6, 2), 10.0, 10.0)
          if AGG._as_date(p["date"]).weekday() not in (2, 3)]
rows = AGG.aggregate_points(gapped, "week")
r = rows[0]
check("absent days are not counted as present", r["days_present"] == 5, r["days_present"])
check("absent days do not contribute zero", r["actual_mwh"] == 50.0, r["actual_mwh"])
check("a gapped week is incomplete", r["actual_complete"] is False)
check("the period still spans seven calendar days", r["days_in_period"] == 7)

# A period with no actual at all must report None, never 0.0.
forecast_only = full_week(date(2025, 6, 9), predicted=10.0, actual=None)
rows = AGG.aggregate_points(forecast_only, "week")
check("a period with no actual reports None", rows[0]["actual_mwh"] is None)
check("a forecast-only period still has a prediction", rows[0]["predicted_mwh"] == 70.0)
check("a forecast-only period is not comparable", rows[0]["comparable"] is False)

# And the reverse: measured days with no prediction.
measured_only = [pt(date(2025, 6, 2) + timedelta(days=i), None, 10.0) for i in range(7)]
rows = AGG.aggregate_points(measured_only, "week")
check("a period with no prediction reports None", rows[0]["predicted_mwh"] is None)
check("a measured-only period offers no deviation", rows[0]["deviation_mwh"] is None)

print()
print("=" * 70)
print("PREDICTION INTERVALS")
print("=" * 70)

bounded = full_week(date(2025, 6, 2), predicted=10.0, lower=8.0, upper=13.0)
rows = AGG.aggregate_points(bounded, "week")
check("bounds sum across the period",
      (rows[0]["lower_mwh"], rows[0]["upper_mwh"]) == (56.0, 91.0),
      "%s..%s" % (rows[0]["lower_mwh"], rows[0]["upper_mwh"]))
check("the summing method is declared",
      rows[0]["interval_method"] == "sum of daily bounds")
check("the band still contains the prediction",
      rows[0]["lower_mwh"] <= rows[0]["predicted_mwh"] <= rows[0]["upper_mwh"])

# One day without bounds invalidates the period's band - a partial sum of
# bounds is not a bound.
holey = full_week(date(2025, 6, 2), predicted=10.0, lower=8.0, upper=13.0)
holey[4]["lower_mwh"] = None
rows = AGG.aggregate_points(holey, "week")
check("one unbounded day removes the period band", rows[0]["lower_mwh"] is None)
check("and the method is not claimed", rows[0]["interval_method"] is None)

# A period missing a day cannot have a band either, even if every present day
# carries one: the absent day's uncertainty is unknown, not zero.
short = [p for p in bounded if AGG._as_date(p["date"]).weekday() != 5]
rows = AGG.aggregate_points(short, "week")
check("a short period has no band", rows[0]["lower_mwh"] is None)

print()
print("=" * 70)
print("GROUPING AND LABELLING")
print("=" * 70)

# Two weeks must not merge, and must come back in order.
two = full_week(date(2025, 6, 2), 10.0, 10.0) + full_week(date(2025, 6, 9), 20.0, 20.0)
rows = AGG.aggregate_points(two, "week")
check("adjacent weeks stay separate", len(rows) == 2)
check("periods are returned oldest first",
      rows[0]["period_start"] < rows[1]["period_start"])
check("each week keeps its own total",
      [r["predicted_mwh"] for r in rows] == [70.0, 140.0])
check("a week is labelled with its range", "-" in rows[0]["label"], rows[0]["label"])
check("a month is labelled with month and year",
      AGG.aggregate_points(june, "month")[0]["label"] == "Jun 2025",
      AGG.aggregate_points(june, "month")[0]["label"])
check("the chart x-key is the period start",
      rows[0]["date"] == rows[0]["period_start"])

# Backtest flags: a period is only a backtest period if every day in it is.
part_bt = full_week(date(2025, 6, 2), 10.0, 10.0)
for i in range(3):
    part_bt[i]["is_backtest"] = True
rows = AGG.aggregate_points(part_bt, "week")
check("a part-backtest week is not marked as backtest", rows[0]["is_backtest"] is False)
check("and it is flagged as containing forecast days", rows[0]["has_forecast_days"] is True)

all_bt = [dict(p, is_backtest=True) for p in full_week(date(2025, 6, 2), 10.0, 10.0)]
rows = AGG.aggregate_points(all_bt, "week")
check("an all-backtest week is marked as backtest", rows[0]["is_backtest"] is True)
check("and carries no forecast days", rows[0]["has_forecast_days"] is False)

print()
print("=" * 70)
print("INPUT HANDLING")
print("=" * 70)

check("day bucket passes each day through",
      len(AGG.aggregate_points(full_week(date(2025, 6, 2), 10.0, 10.0), "day")) == 7)
check("a day period is one day long",
      AGG.aggregate_points([pt(date(2025, 6, 2), 10.0, 10.0)], "day")[0]["days_in_period"] == 1)
check("empty input gives an empty series", AGG.aggregate_points([], "week") == [])

# The router holds datetimes; the DB-free path holds dates; JSON holds strings.
iso = AGG.aggregate_points([pt("2025-06-04T00:00:00", 10.0, 10.0)], "week")
check("an ISO datetime string is parsed", iso[0]["period_start"] == "2025-06-02")

from datetime import datetime                                        # noqa: E402
dt_rows = AGG.aggregate_points(
    [{"date": datetime(2025, 6, 4, 0, 0), "predicted_mwh": 10.0, "actual_mwh": 10.0,
      "lower_mwh": None, "upper_mwh": None, "is_backtest": False}], "week")
check("a datetime object is parsed", dt_rows[0]["period_start"] == "2025-06-02")

check("an undated row is dropped, not crashed on",
      AGG.aggregate_points([{"date": None, "predicted_mwh": 1.0}], "week") == [])

try:
    AGG.aggregate_points([], "fortnight")
    check("an unknown bucket is rejected", False, "no error raised")
except ValueError:
    check("an unknown bucket is rejected", True)

print()
print("=" * 70)
print("CONSISTENCY WITH THE DAY VIEW")
print("=" * 70)

# The property that matters operationally: whatever the bucket, the totals of
# the periods must equal the totals of the days they were built from.
series = []
d = date(2025, 5, 27)
for i in range(39):
    if i % 7 == 3:          # scatter some absent days through the series
        d += timedelta(days=1)
        continue
    series.append(pt(d, predicted=250.0 + i, actual=(240.0 + i) if i < 20 else None))
    d += timedelta(days=1)

day_pred = sum(p["predicted_mwh"] for p in series)
day_act = sum(p["actual_mwh"] for p in series if p["actual_mwh"] is not None)

for bucket in ("day", "week", "month"):
    rows = AGG.aggregate_points(series, bucket)
    tp = sum(r["predicted_mwh"] for r in rows if r["predicted_mwh"] is not None)
    ta = sum(r["actual_mwh"] for r in rows if r["actual_mwh"] is not None)
    check("%-5s totals match the daily series" % bucket,
          abs(tp - day_pred) < 1e-9 and abs(ta - day_act) < 1e-9,
          "pred %.1f vs %.1f, actual %.1f vs %.1f" % (tp, day_pred, ta, day_act))

# No day may be counted twice or dropped.
for bucket in ("week", "month"):
    rows = AGG.aggregate_points(series, bucket)
    check("%-5s accounts for every day exactly once" % bucket,
          sum(r["days_present"] for r in rows) == len(series),
          "%d of %d" % (sum(r["days_present"] for r in rows), len(series)))

print()
print("=" * 70)
print("%d passed, %d failed" % (_passed, _failed))
print("=" * 70)
sys.exit(1 if _failed else 0)
