"""
Device discovery and per-device daily energy for Kalyon.

Written for the kalyan schema, not ported from CHAMMU. The two plants differ
in every way that matters here:

* Kalyon's inverter tables are zero-padded and there are 24 of them
  (`INVERTER_01` … `INVERTER_24`); CHAMMU's are unpadded and there are 12.
* Kalyon's inverters expose `ACTIVE_POWER` in kW; CHAMMU's expose
  `TOTAL_ACTIVE_POWER`.
* Kalyon's string combiner boxes are **columns**, not tables. Each of the 24
  `INVERTERn_SMB` tables carries roughly 21 boxes as `SCB1_DC_POWER` …
  `SCB21_DC_POWER`. CHAMMU gives each box its own table, 170 of them. One
  grouped query per table therefore covers a whole inverter's worth of boxes
  here — 24 queries rather than 500.

What the plant total means also differs, and it matters for reading the device
table. Kalyon's plant figure is net export measured at the grid
(`PPC.GRID_ACTIVE_POWER_MEASURED`); the inverters measure AC output before the
transformer and the auxiliary load. Measured on 2024-04-15 the 24 inverters
came to 101.4% of grid export, which is what those losses should look like. So
the per-device rows legitimately exceed the plant figure by a percent or so,
and nothing here is scaled to hide that.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.forecast.features import COMPLETE_DAY_MINUTES

logger = logging.getLogger(__name__)

INVERTER_RE = re.compile(r"^INVERTER_(\d{2})$")
SMB_RE = re.compile(r"^INVERTER(\d+)_SMB$")
SCB_POWER_RE = re.compile(r"^SCB(\d+)_DC_POWER$")

# Table and column names are interpolated into SQL because neither can be a
# bind parameter. Everything interpolated is matched against this first, and
# every name originates in INFORMATION_SCHEMA rather than in a request.
SAFE_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")

# A device needs enough history to train AND still hold days back. Below this
# it is reported as INSUFFICIENT DATA rather than given a number.
MIN_DEVICE_DAYS = 21

# How many SMB tables to query at once. These are read-only scans on separate
# tables; the limit keeps a burst of them from monopolising the database.
COMBINER_QUERY_THREADS = int(os.getenv("COMBINER_QUERY_THREADS", "8"))

# Mean daily DC energy below which a combiner box is reporting nothing useful.
SMB_ZERO_KWH = 50.0


def _check(name: str) -> str:
    if not SAFE_IDENT.match(name):
        raise ValueError(f"refusing to build SQL with identifier {name!r}")
    return name


def _tables(db: Session) -> List[str]:
    rows = db.execute(text(
        "SELECT TABLE_NAME FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' ORDER BY TABLE_NAME")).fetchall()
    return [r[0] for r in rows]


def _columns(db: Session, table: str) -> List[str]:
    rows = db.execute(
        text("SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
             "WHERE TABLE_NAME = :t ORDER BY ORDINAL_POSITION"),
        {"t": table}).fetchall()
    return [r[0] for r in rows]


def _all_columns(db: Session) -> Dict[str, List[str]]:
    """
    Every table's column list, in one round trip.

    discover() used to call _columns() per table. On this database that is 93
    separate queries and it cost 4.8 seconds before any forecasting started,
    while the whole catalogue comes back in under a tenth of a second. The
    per-table helper stays for callers that genuinely want one table.
    """
    rows = db.execute(text(
        "SELECT TABLE_NAME, COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
        "ORDER BY TABLE_NAME, ORDINAL_POSITION")).fetchall()
    out: Dict[str, List[str]] = {}
    for table, column in rows:
        out.setdefault(table, []).append(column)
    return out


def discover(db: Session) -> Dict[str, List[Dict[str, Any]]]:
    """
    What this database actually holds, read from INFORMATION_SCHEMA.

    Nothing is assumed from the other plant's schema and nothing is hardcoded
    to a count: if an inverter is added or removed, this follows.
    """
    tables = _tables(db)
    catalogue = _all_columns(db)

    inverters: List[Dict[str, Any]] = []
    for t in tables:
        m = INVERTER_RE.match(t)
        if not m:
            continue
        if "ACTIVE_POWER" not in catalogue.get(t, ()):
            logger.warning("%s matches the inverter pattern but has no "
                           "ACTIVE_POWER column; skipped.", t)
            continue
        inverters.append({"id": t, "table": t, "type": "inverter",
                          "group": "Inverters", "number": int(m.group(1))})
    inverters.sort(key=lambda d: d["number"])

    # Combiner boxes live as columns. One entry per box, carrying the table it
    # is read from and the column that holds its DC power.
    combiners: List[Dict[str, Any]] = []
    for t in tables:
        m = SMB_RE.match(t)
        if not m:
            continue
        inv_no = int(m.group(1))
        for col in catalogue.get(t, ()):
            cm = SCB_POWER_RE.match(col)
            if not cm:
                continue
            combiners.append({
                "id": "INV%02d_SCB%s" % (inv_no, cm.group(1)),
                "table": t, "column": col, "type": "combiner",
                "group": "Inverter %02d" % inv_no,
                "inverter": inv_no, "number": int(cm.group(1)),
            })
    combiners.sort(key=lambda d: (d["inverter"], d["number"]))

    return {"inverters": inverters, "combiners": combiners}


# ── per-inverter daily energy ────────────────────────────────────────────────
#
# Mirrors _DAILY_PLANT_SQL in features.py exactly: duplicate timestamps are
# averaged into one row per minute before anything is summed, energy is the
# integral of power over those minutes, and a day is only returned when it has
# at least COMPLETE_DAY_MINUTES of them. ACTIVE_POWER is kW here against the
# plant's MW, hence the extra /1000.
_DAILY_INVERTER_SQL = """
    WITH minute_slot AS (
        SELECT DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)  AS ts,
               AVG(CAST(ACTIVE_POWER AS float))                  AS kw
        FROM [{table}]
        GROUP BY DATEADD(minute, DATEDIFF(minute, 0, TimeCol), 0)
    )
    SELECT CAST(ts AS DATE)                                      AS d,
           COUNT(*)                                              AS minutes,
           SUM(kw) / 60.0 / 1000.0                               AS energy_net_mwh,
           SUM(CASE WHEN kw > 0 THEN kw ELSE 0 END) / 60.0 / 1000.0
                                                                 AS energy_gross_mwh,
           MAX(kw) / 1000.0                                      AS peak_mw,
           SUM(CASE WHEN kw > 1 THEN 1 ELSE 0 END)               AS producing_minutes
    FROM minute_slot
    GROUP BY CAST(ts AS DATE)
    HAVING COUNT(*) >= {minutes}
    ORDER BY d
"""


def daily_inverter(db: Session, table: str) -> List[Dict[str, Any]]:
    """
    One row per COMPLETE day for a single inverter.

    The dict shape matches `features.daily_plant` so the existing frame
    builder and backtest consume it without a special case. The two fields a
    single inverter cannot know - how many inverters were running, and whether
    the plant was curtailed - are filled from its own state rather than
    invented: it is one unit, and it has no view of the set point.
    """
    sql = _DAILY_INVERTER_SQL.format(table=_check(table),
                                     minutes=COMPLETE_DAY_MINUTES)
    rows = db.execute(text(sql)).fetchall()
    return [
        {
            "d": r[0],
            "minutes": int(r[1]),
            "energy_net_mwh": float(r[2] or 0.0),
            "energy_gross_mwh": float(r[3] or 0.0),
            "peak_mw": float(r[4] or 0.0),
            "avg_inverters_running": 1.0,
            "curtailed_minutes": 0,
            "producing_minutes": int(r[5] or 0),
            "mean_setpoint_pct": None,
        }
        for r in rows
    ]


# ── combiner boxes ───────────────────────────────────────────────────────────
def daily_combiners(db: Session, boxes: List[Dict[str, Any]]
                    ) -> Dict[str, Dict[str, Any]]:
    """
    Mean daily DC energy per combiner box.

    One query per SMB table covers every box in it, so this is 24 round trips
    rather than one per box.

    SCBn_DC_POWER is **kW** on this plant, so a day's energy is sum/60 with no
    further division. That was checked against the channel rather than assumed
    from the name: the column peaks at 427 while the same box's voltage and
    current peak at 1322 V and 305.6 A, which multiply to 404 kW. Reading it as
    watts - which is how CHAMMU's equivalent column is scaled - made every one
    of the 504 boxes average 1.4 kWh a day and report ZERO OUTPUT.

    These are NOT forecast. A box carries 61 complete days and no energy
    register, and the operationally useful question about a combiner is not
    "how much tomorrow" but "is this one keeping up with its neighbours".
    """
    by_table: Dict[str, List[Dict[str, Any]]] = {}
    for b in boxes:
        by_table.setdefault(b["table"], []).append(b)

    def _sql_for(table: str, members: List[Dict[str, Any]]) -> str:
        cols = ", ".join(
            "SUM(kw_%(n)s) / 60.0 AS kwh_%(n)s" % {"n": m["number"]}
            for m in members)
        inner = ", ".join(
            "AVG(CAST([%s] AS float)) AS kw_%s" % (_check(m["column"]), m["number"])
            for m in members)
        return """
            WITH minute_slot AS (
                SELECT DATEADD(minute, DATEDIFF(minute,0,TimeCol),0) AS ts, {inner}
                FROM [{table}]
                GROUP BY DATEADD(minute, DATEDIFF(minute,0,TimeCol),0)
            )
            SELECT CAST(ts AS DATE) AS d, COUNT(*) AS minutes, {cols}
            FROM minute_slot
            GROUP BY CAST(ts AS DATE)
            HAVING COUNT(*) >= {minutes}
        """.format(inner=inner, table=_check(table), cols=cols,
                   minutes=COMPLETE_DAY_MINUTES)

    def _collect(table: str, members: List[Dict[str, Any]],
                 rows: List[Any]) -> None:
        for i, m in enumerate(members):
            vals = [r[2 + i] for r in rows if r[2 + i] is not None]
            out[m["id"]] = {
                "device": m["id"],
                "type": "combiner",
                "group": m["group"],
                "inverter": m["inverter"],
                "complete_days": len(vals),
                "mean_kwh": (sum(float(v) for v in vals) / len(vals)) if vals else None,
            }

    out: Dict[str, Dict[str, Any]] = {}

    # Each of these queries scans a whole SMB table and takes about six tenths
    # of a second; run one after another the 24 of them cost 15 seconds, and
    # the Combiners tab sat on a spinner for all of it. They touch different
    # tables, nothing is written, and the time goes on waiting for SQL Server
    # rather than on Python, so they are issued together on threads - the GIL
    # is released for the duration of each round trip. Each thread opens its
    # own session, because a SQLAlchemy Session is not safe to share.
    def _one(item: Any) -> Any:
        table, members = item
        session = SessionLocal()
        try:
            return table, members, session.execute(text(_sql_for(table, members))).fetchall()
        finally:
            session.close()

    items = list(by_table.items())
    workers = min(len(items), COMBINER_QUERY_THREADS)
    if workers > 1:
        try:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                for table, members, rows in ex.map(_one, items):
                    _collect(table, members, rows)
            return out
        except Exception as exc:                       # noqa: BLE001
            # Connection limits, a driver that dislikes concurrency - fall back
            # to the original one-at-a-time pass on the caller's session.
            logger.warning("Parallel combiner read failed (%s); "
                           "falling back to one table at a time.", exc)
            out = {}

    for table, members in items:
        _collect(table, members, db.execute(text(_sql_for(table, members))).fetchall())
    return out


def last_seen(db: Session, tables: List[str]) -> Dict[str, Any]:
    """Newest timestamp per table, so the UI can show data freshness."""
    out: Dict[str, Any] = {}
    for t in tables:
        try:
            out[t] = db.execute(
                text("SELECT MAX(TimeCol) FROM [%s]" % _check(t))).scalar()
        except Exception as exc:                     # noqa: BLE001
            logger.warning("last_seen failed for %s: %s", t, exc)
            out[t] = None
    return out
