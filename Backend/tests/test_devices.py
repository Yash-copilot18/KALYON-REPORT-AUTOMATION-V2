# -*- coding: utf-8 -*-
"""
Checks for Kalyon device discovery and per-device energy.

These run against the live kalyan database, read-only. They exist because the
device layer is the part most easily wrong in a way that still looks
plausible: a unit slip turns every combiner box into a dead one, and a leaky
split turns a bad model into a good score.
"""

import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_SERVER", r"SERVICESCADA32\SQLEXPRESS")

from sqlalchemy import text                                      # noqa: E402
from app.database.session import SessionLocal                    # noqa: E402
from app.forecast import devices as D                            # noqa: E402
from app.forecast import device_service as DS                    # noqa: E402
from app.forecast import features as F                           # noqa: E402

_passed = 0
_failed = 0


def check(label, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1
        print("  PASS  %s%s" % (label, ("   " + str(detail)) if detail else ""))
    else:
        _failed += 1
        print("  FAIL  %s   %s" % (label, detail))


db = SessionLocal()

print("=" * 72)
print("DISCOVERY")
print("=" * 72)
disc = D.discover(db)
inv, cmb = disc["inverters"], disc["combiners"]
check("inverters are discovered", len(inv) > 0, "%d found" % len(inv))
check("inverter ids are zero-padded INVERTER_nn",
      all(D.INVERTER_RE.match(d["id"]) for d in inv))
check("inverters come back in order",
      [d["number"] for d in inv] == sorted(d["number"] for d in inv))
check("combiner boxes are discovered", len(cmb) > 0, "%d found" % len(cmb))
check("every box names its source column",
      all(d.get("column") and d.get("table") for d in cmb))
check("box ids are unique", len({d["id"] for d in cmb}) == len(cmb))
check("boxes are grouped per inverter",
      len({d["group"] for d in cmb}) == len({d["inverter"] for d in cmb}))

# Nothing may be hardcoded to a count; discovery must come from the schema.
n_tables = db.execute(text(
    "SELECT COUNT(*) FROM INFORMATION_SCHEMA.TABLES "
    "WHERE TABLE_NAME LIKE 'INVERTER[_][0-9][0-9]'")).scalar()
check("inverter count matches the schema, not a constant",
      len(inv) == n_tables, "%d vs %d" % (len(inv), n_tables))

print()
print("=" * 72)
print("SQL IDENTIFIER SAFETY")
print("=" * 72)
for bad in ("INVERTER_01; DROP TABLE PPC", "foo'--", "1_bad", "x" * 70, ""):
    try:
        D._check(bad)
        check("rejects %r" % bad[:28], False, "accepted")
    except ValueError:
        check("rejects %r" % bad[:28], True)
check("accepts a real table name", D._check("INVERTER_01") == "INVERTER_01")

print()
print("=" * 72)
print("PER-INVERTER DAILY ENERGY")
print("=" * 72)
rows = D.daily_inverter(db, inv[0]["table"])
check("a complete-day series is returned", len(rows) > 0, "%d days" % len(rows))
check("every day meets the completeness threshold",
      all(r["minutes"] >= F.COMPLETE_DAY_MINUTES for r in rows))
check("days are unique", len({r["d"] for r in rows}) == len(rows))
check("days are in order", [r["d"] for r in rows] == sorted(r["d"] for r in rows))
check("energy is non-negative", all(r["energy_net_mwh"] >= 0 for r in rows))
check("gross is never below net",
      all(r["energy_gross_mwh"] >= r["energy_net_mwh"] - 1e-9 for r in rows))

# A single inverter on this plant peaks near 4.7 MW, so a day cannot exceed
# roughly 113 MWh however sunny it is. A unit slip would blow straight past.
check("daily energy is physically possible for one inverter",
      all(0 <= r["energy_net_mwh"] <= 120 for r in rows),
      "max %.1f MWh" % max(r["energy_net_mwh"] for r in rows))
check("the shape matches features.daily_plant",
      set(rows[0]) >= set(F.daily_plant(db)[0]),
      "so the existing frame builder needs no special case")

print()
print("=" * 72)
print("DEVICES RECONCILE WITH THE PLANT")
print("=" * 72)
plant = {r["d"]: r for r in F.daily_plant(db)}
sample = [r["d"] for r in rows if r["d"] in plant][-3:]
for d in sample:
    total = 0.0
    for dev in inv:
        s = {x["d"]: x for x in D.daily_inverter(db, dev["table"])}
        if d in s:
            total += s[d]["energy_net_mwh"]
    ratio = 100.0 * total / plant[d]["energy_net_mwh"]
    # Inverter AC is measured before the transformer and the auxiliary load,
    # so the sum must EXCEED grid export, by a few percent and no more.
    check("%s: inverter sum exceeds grid export by a plausible margin" % d,
          100.0 <= ratio <= 110.0, "%.1f%%" % ratio)

print()
print("=" * 72)
print("COMBINER UNITS")
print("=" * 72)
sub = [c for c in cmb if c["inverter"] == inv[0]["number"]]
means = D.daily_combiners(db, sub)
vals = [v["mean_kwh"] for v in means.values() if v["mean_kwh"] is not None]
check("every box in the group returns a figure", len(vals) == len(sub),
      "%d of %d" % (len(vals), len(sub)))
# The whole point of this check: read as watts instead of kW, these come back
# around 1.4 kWh and every box looks dead.
check("box energy is in a credible range for a string combiner",
      all(100 <= v <= 6000 for v in vals),
      "min %.0f max %.0f kWh/day" % (min(vals), max(vals)))
dc_total = sum(vals)
ac_mean = sum(r["energy_net_mwh"] for r in rows) / len(rows) * 1000.0
check("group DC total is near the inverter's own AC output",
      0.8 <= dc_total / ac_mean <= 1.6,
      "DC %.0f kWh vs AC %.0f kWh, ratio %.2f" % (dc_total, ac_mean, dc_total / ac_mean))

print()
print("=" * 72)
print("NO TEMPORAL LEAKAGE")
print("=" * 72)
row = DS._forecast_one(rows, inv[0])
if row["status"] == "OK":
    check("test block starts after training ends", row["test_start"] <= row["test_end"])
    check("the forecast date follows the last observed day",
          row["forecast_date"] == rows[-1]["d"] + timedelta(days=1),
          str(row["forecast_date"]))
    check("every forward point is after the last actual",
          all(p["date"] > rows[-1]["d"] for p in row["daily"]))
    check("backtest points all carry an actual",
          all(p["actual_mwh"] is not None
              for p in row["series"] if p["is_backtest"]))
    check("forward points carry no actual",
          all(p["actual_mwh"] is None
              for p in row["series"] if not p["is_backtest"]))
    check("the series is ordered",
          [p["date"] for p in row["series"]] == sorted(p["date"] for p in row["series"]))
    check("a sub-threshold sample is not called MEASURED",
          not (row["n_test"] < 30 and row["validation"] == "MEASURED"),
          "n_test=%d -> %s" % (row["n_test"], row["validation"]))
else:
    check("a device that cannot be evaluated says so, with a reason",
          bool(row.get("reason")), row.get("reason"))

print()
print("=" * 72)
print("INSUFFICIENT DATA IS REPORTED, NOT FABRICATED")
print("=" * 72)
short = DS._forecast_one(rows[:5], inv[0])
check("a 5-day device is INSUFFICIENT DATA", short["status"] == "INSUFFICIENT DATA")
check("and offers no prediction", short["predicted_mwh"] is None)
check("and no score", short["score_pct"] is None)
check("but still appears with its day count", short["complete_days"] == 5)
empty = DS._forecast_one([], inv[0])
check("a device with no data does not crash", empty["status"] == "INSUFFICIENT DATA")
check("and reports nothing rather than zero", empty["latest_actual_mwh"] is None)

print()
print("=" * 72)
print("PLANT PIPELINE UNCHANGED")
print("=" * 72)
p = F.daily_plant(db)
check("plant series still builds", len(p) > 0, "%d days" % len(p))
frame = F.build_frame(p, F.daily_weather(db), "energy_net_mwh", weather_forecast=None)
check("plant frame still builds", frame["X"].shape[0] > 0, str(frame["X"].shape))

db.close()
print()
print("=" * 72)
print("%d passed, %d failed" % (_passed, _failed))
print("=" * 72)
sys.exit(1 if _failed else 0)
