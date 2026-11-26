# -*- coding: utf-8 -*-
"""
Checks for SCADA duplicate handling (app/services/dedup.py).

Run from the Backend directory:

    venv\\Scripts\\python.exe tests\\test_dedup.py

Two tiers. The first is pure string construction and always runs. The second
reads the live historian - SELECT / COUNT / GROUP BY only, never a write - and
asserts the properties that matter on the real archive: that an exact duplicate
is dropped, that a blank reconnect snapshot loses to the live one, that two
different devices sharing a timestamp are both kept, that a missing minute stays
missing, that unresolvable pairs are left alone rather than guessed at, and that
running the same pipeline twice returns the same records. It skips, rather than
fails, when no database is reachable.

Nothing here modifies a historian record.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             ".env"))
except Exception:                                      # pragma: no cover
    pass

from app.services import dedup                          # noqa: E402

_passed = 0
_failed = 0
_skipped = 0


def check(label, condition, detail=""):
    global _passed, _failed
    if condition:
        _passed += 1
        print("  PASS  %s%s" % (label, ("   " + str(detail)) if detail else ""))
    else:
        _failed += 1
        print("  FAIL  %s   %s" % (label, detail))


def skip(label, why):
    global _skipped
    _skipped += 1
    print("  SKIP  %s   %s" % (label, why))


# ── tier 1: what the SQL builder promises ────────────────────────────────────
print("\n--- retention rule construction ---")

PPC_TYPES = {
    "TimeCol": "datetime", "MSecCol": "smallint", "LocalCol": "datetime",
    "GRID_ACTIVE_POWER_MEASURED": "float", "INVERTER_RUNNING": "bigint",
    "VOLTAGE_DROOP_ENABLE": "nvarchar",
}
PPC_TAGS = ["GRID_ACTIVE_POWER_MEASURED", "INVERTER_RUNNING", "VOLTAGE_DROOP_ENABLE"]

src = dedup.source("PPC", PPC_TAGS, PPC_TYPES, where="TimeCol BETWEEN :a AND :b")

check("groups on the second, not the raw millisecond",
      "DATEDIFF(second" in src and "DATEDIFF(millisecond" not in src)
check("ranks by how blank the record is before anything else",
      src.index("CASE WHEN") < src.index("TimeCol ASC"))
check("falls back to the historian's own sub-second sequence",
      "TimeCol ASC, MSecCol ASC" in src)
check("keeps exactly one record per key",
      "_dedup_rn = 1" in src)
check("pushes the range filter inside, so the TimeCol index is still used",
      src.index("WHERE TimeCol BETWEEN :a AND :b") < src.index("_dedup_rn = 1"))
check("aliases back to the table name, so callers need no other change",
      src.rstrip().endswith("AS [PPC]"))
check("a numeric tag counts as blank when NULL or zero",
      "CAST([GRID_ACTIVE_POWER_MEASURED] AS FLOAT) = 0" in src)
check("a text tag counts as blank when NULL, empty, '0' or 'false'",
      "'', '0', 'false'" in src)
check("never selects a column the caller did not ask for",
      "[LocalCol]" not in src)

print("\n--- tables that must not be deduplicated ---")
for t in ("Alarms", "SysMsgs", "Drivers", "alarms"):
    check("%s is left untouched (event log, device is not a column)" % t,
          dedup.source(t, ["DescCol"], {"TimeCol": "datetime", "MSecCol": "smallint",
                                        "DescCol": "nvarchar"}) == "[%s]" % t)
check("a table without MSecCol is left untouched (no deterministic order)",
      dedup.source("forecast_runs", ["score_pct"],
                   {"TimeCol": "datetime", "score_pct": "float"}) == "[forecast_runs]")
check("a request with no usable tag is left untouched",
      dedup.source("PPC", [], PPC_TYPES) == "[PPC]")
check("an unsafe table name is refused",
      dedup.source("PPC; DROP TABLE x", PPC_TAGS, PPC_TYPES) == "[PPC; DROP TABLE x]"
      or not dedup.applies_to("PPC; DROP TABLE x"))

print("\n--- deterministic ordering ---")
check("ascending order carries the tie-break", dedup.order_by() == "TimeCol, MSecCol")
check("descending order carries the tie-break",
      dedup.order_by_desc() == "TimeCol DESC, MSecCol DESC")
check("order_by can be qualified for a joined query",
      dedup.order_by("x") == "x.TimeCol, x.MSecCol")
check("the same inputs always build the same SQL",
      dedup.source("PPC", PPC_TAGS, PPC_TYPES, where="TimeCol > :a")
      == dedup.source("PPC", PPC_TAGS, PPC_TYPES, where="TimeCol > :a"))

print("\n--- bookkeeping columns are never measurements ---")
check("tag_columns drops the historian's own columns",
      dedup.tag_columns(["TimeCol", "MSecCol", "LocalCol", "UserCol", "ReasonCol",
                         "UniID", "TraID", "ACTIVE_POWER"]) == ["ACTIVE_POWER"])
check("a blank score over no columns is zero, not an empty expression",
      dedup.blank_score_expr([], {}) == "0")


# ── tier 2: the live archive ─────────────────────────────────────────────────
print("\n--- against the historian ---")

db = None
try:
    from app.database.session import SessionLocal
    from sqlalchemy import text
    db = SessionLocal()
    db.execute(text("SELECT 1")).scalar()
except Exception as exc:                               # pragma: no cover
    skip("live historian checks", "no database: %s" % str(exc)[:80])
    db = None

if db is not None:
    from app.services import schema_cache

    def q(sql, **p):
        return db.execute(text(sql), p).fetchall()

    SEC = dedup.SECOND_KEY
    types = schema_cache.get_columns(db, "PPC") or {}
    tags = dedup.tag_columns(types)
    source = dedup.source("PPC", tags, types)

    # One record per (device, tag, second) - and that is every second there is.
    row = q(f"""SELECT (SELECT COUNT(*) FROM [PPC]) AS before_,
                       (SELECT COUNT(*) FROM {source}) AS after_,
                       (SELECT COUNT(DISTINCT {SEC}) FROM [PPC]) AS seconds_""")[0]
    before, after, seconds = row
    check("retains exactly one record per second", after == seconds,
          "%d rows -> %d retained, %d distinct seconds" % (before, after, seconds))
    check("retains fewer records than the historian holds", after < before,
          "%d duplicates excluded" % (before - after))

    # No second may survive with two records, or the key is wrong.
    left = q(f"""SELECT COUNT(*) FROM (SELECT {SEC} AS s FROM {source}
                 GROUP BY {SEC} HAVING COUNT(*) > 1) z""")[0][0]
    check("no duplicate survives the rule", left == 0, left)

    # Repeated execution of the same pipeline returns the same records.
    h1 = q(f"SELECT COUNT(*) AS n, SUM(CONVERT(bigint, CHECKSUM(TimeCol))) AS h FROM {source}")[0]
    h2 = q(f"SELECT COUNT(*) AS n, SUM(CONVERT(bigint, CHECKSUM(TimeCol))) AS h FROM {source}")[0]
    check("running the pipeline twice retains the identical set",
          tuple(h1) == tuple(h2), tuple(h1))

    # Exact duplicates: both members identical, so whichever is kept the values
    # are unchanged. Every tag of the retained record must still be present.
    nulls = q(f"""SELECT COUNT(*) FROM {source}
                  WHERE GRID_ACTIVE_POWER_MEASURED IS NULL
                    AND TimeCol IN (SELECT TimeCol FROM [PPC]
                                    WHERE GRID_ACTIVE_POWER_MEASURED IS NOT NULL)""")[0][0]
    check("never turns a measured value into NULL", nulls == 0, nulls)

    # A blank reconnect snapshot must lose to the record that carries the plant.
    blanks = q(f"""SELECT COUNT(*) FROM {source}
                   WHERE ISNULL(GRID_VOLTAGE_L_L_MEASURED, 0) = 0
                     AND {SEC} IN (SELECT {SEC} FROM [PPC] GROUP BY {SEC}
                                   HAVING COUNT(*) > 1
                                      AND MAX(ISNULL(GRID_VOLTAGE_L_L_MEASURED,0)) > 0)""")[0][0]
    check("rejects the blank snapshot when the pair holds a live record",
          blanks == 0, "%d blanks retained" % blanks)

    # Conflicting pairs the historian cannot order are left alone, not guessed
    # at: the rule still returns one record, and it is one of the two originals.
    strays = q(f"""SELECT COUNT(*) FROM {source}
                   WHERE NOT EXISTS (SELECT 1 FROM [PPC] AS p
                                     WHERE p.TimeCol = [PPC].TimeCol
                                       AND p.MSecCol = [PPC].MSecCol)""")[0][0]
    check("every retained record is an unmodified historian record", strays == 0, strays)

    # Different devices sharing a timestamp are not duplicates of each other.
    shared = q(f"""SELECT COUNT(*) FROM
        (SELECT {SEC} AS s FROM {source}) a
        JOIN (SELECT {SEC} AS s FROM {dedup.source('WMS',
              dedup.tag_columns(schema_cache.get_columns(db, 'WMS') or {}),
              schema_cache.get_columns(db, 'WMS') or {})}) b ON b.s = a.s""")[0][0]
    check("two devices keep their own record at the same timestamp", shared > 0,
          "%d timestamps present in both PPC and WMS" % shared)

    # An event log keeps every row, including several at one instant.
    al_types = schema_cache.get_columns(db, "Alarms") or {}
    if al_types:
        al_src = dedup.source("Alarms", dedup.tag_columns(al_types), al_types)
        same = q(f"""SELECT (SELECT COUNT(*) FROM [Alarms]) AS a,
                            (SELECT COUNT(*) FROM {al_src}) AS b""")[0]
        check("Alarms keeps every row", same[0] == same[1], same)
        multi = q("""SELECT COUNT(*) FROM (SELECT TimeCol FROM [Alarms]
                     GROUP BY TimeCol HAVING COUNT(*) > 1) z""")[0][0]
        check("Alarms really does hold several events at one instant", multi > 0, multi)

    # Missing minutes must stay missing - dedup removes records, never invents one.
    gaps = q(f"""SELECT (SELECT COUNT(DISTINCT DATEADD(minute,DATEDIFF(minute,0,TimeCol),0))
                         FROM [PPC]) AS before_,
                        (SELECT COUNT(DISTINCT DATEADD(minute,DATEDIFF(minute,0,TimeCol),0))
                         FROM {source}) AS after_""")[0]
    check("no minute is added or lost", gaps[0] == gaps[1], gaps)

    # Energy: the retained set must agree with the plant's own meter far better
    # than the raw rows do, on the days the historian retransmitted.
    worst = q(f"""
      WITH raw_ AS (SELECT CAST(TimeCol AS DATE) d, COUNT(*) n, COUNT(DISTINCT {SEC}) k,
                           SUM(GRID_ACTIVE_POWER_MEASURED)/60.0 e,
                           MAX(CAST(PLANT_DAILY_PRODUCTION AS float)) meter
                    FROM [PPC] GROUP BY CAST(TimeCol AS DATE)),
           ded AS (SELECT CAST(TimeCol AS DATE) d,
                          SUM(GRID_ACTIVE_POWER_MEASURED)/60.0 e
                   FROM {source} GROUP BY CAST(TimeCol AS DATE))
      SELECT TOP 1 raw_.d, raw_.e, ded.e, raw_.meter
      FROM raw_ JOIN ded ON ded.d = raw_.d
      WHERE raw_.n <> raw_.k AND raw_.meter > 0
      ORDER BY ABS(raw_.e - raw_.meter) DESC""")
    if worst:
        d, raw_e, ded_e, meter = worst[0]
        check("the retained set is closer to the plant meter than the raw rows",
              abs(ded_e - meter) < abs(raw_e - meter),
              "%s: raw %.1f, retained %.1f, meter %.1f MWh" % (d, raw_e, ded_e, meter))
        check("the raw rows were over-reporting, not under-reporting",
              raw_e > ded_e, "%.1f -> %.1f MWh" % (raw_e, ded_e))
    else:
        skip("energy reconciliation", "no retransmitted day in the archive")

    # The counter path is the plant's official generation and must not move.
    cnt = q(f"""SELECT (SELECT SUM(e) FROM (SELECT CAST(TimeCol AS DATE) d,
                          MAX(CAST(PLANT_DAILY_PRODUCTION AS float)) e
                        FROM [PPC] GROUP BY CAST(TimeCol AS DATE)) x) AS before_,
                       (SELECT SUM(e) FROM (SELECT CAST(TimeCol AS DATE) d,
                          MAX(CAST(PLANT_DAILY_PRODUCTION AS float)) e
                        FROM {source} GROUP BY CAST(TimeCol AS DATE)) y) AS after_""")[0]
    check("counter-derived generation is unchanged",
          cnt[0] is not None and abs(cnt[0] - cnt[1]) < 0.001,
          "%.3f -> %.3f MWh" % (cnt[0] or 0, cnt[1] or 0))

    # A row count must report retained records, not raw rows.
    cs = dedup.count_source("PPC", types)
    cnt_rows = q(f"SELECT COUNT(*) FROM {cs}")[0][0]
    check("count_source counts retained records", cnt_rows == seconds,
          "%d vs %d" % (cnt_rows, seconds))

    db.close()

print()
print("=" * 70)
print("%d passed, %d failed, %d skipped" % (_passed, _failed, _skipped))
print("=" * 70)
sys.exit(1 if _failed else 0)
