# app/services/dedup.py
"""
One place that decides which historian records are the same measurement.

Why this exists
---------------
The SCADA historian occasionally retransmits a poll. The copy is written 10-40 ms
after the original and carries its own clock stamp, so the two rows are NOT equal
on `TimeCol` and nothing in the table marks either as a repeat. Measured on the
Kalyon archive (104 days): 7,034 of 154,564 PPC rows are such copies - 4.55% -
and the same 7,034 timestamps repeat in every one of the 24 inverter tables,
which is what shows it to be a historian-side event rather than a device fault.

The damage is confined to three things: raw row listings show the same instant
twice, `SUM` over an instant channel counts it twice, and `COUNT(*)` row totals
are inflated. On 2024-05-07 the historian repeated the whole day, so
`SUM(GRID_ACTIVE_POWER_MEASURED)/60` reported 2,242 MWh against the plant's own
meter reading of 1,137 MWh.

The business key
----------------
    device/equipment id  +  tag/measurement id  +  timestamp

This historian is wide - one TABLE per device, one COLUMN per tag - so the key is
(table, column, timestamp) and a second row at one timestamp duplicates every tag
of that device. The timestamp is compared **to the second**, not to the
millisecond, because the tags are polled on a strict 60-second cycle: 147,400 of
the 147,512 gaps between consecutive distinct seconds in PPC are exactly 60 s,
and no second anywhere in the archive holds more than two rows. Two readings
10 ms apart cannot both be scheduled samples. Truncating to the second and
truncating to the minute give the identical count (7,034), which is the proof
that this key discards no legitimate measurement.

Which record is kept
--------------------
Not the first, and not the last. Both are wrong, because the archive holds two
different kinds of repeat:

  * analogue drift - the reading moved in the 4th-6th significant digit between
    the two polls (61.419006 vs 61.335468 MW). Either member is valid.

  * a reconnect snapshot - one member is all zeros and false while the other
    carries the live plant (INVERTER_RUNNING 0 vs 24, ACTIVE_POWER_SET_POINT 0.0
    vs 100.1, VOLTAGE_DROOP_ENABLE false vs true). PPC holds 141 of these. In
    PPC the blank one is the earlier row; in TRACKER_MBOX_STATUS it is the later
    one, so neither "first" nor "last" survives contact with the data.

So the ranking is: fewest blank tag values first, then the historian's own
sub-second sequence. On PPC that differs from plain retain-first on 69 of 7,034
groups - it intervenes only where there is a blank to reject - and it moves
2024-05-01 from 20.4% below the plant meter to 2.0% below it, while leaving every
other day within 0.002 MWh. Repeated execution returns the same retained set.

What is NOT deduplicated
------------------------
`Alarms`, `SysMsgs` and `Drivers` are event logs, not measurement series. They
repeat timestamps legitimately - several alarms fire at one instant, and two
different stations report a comms error in the same second:

    13:52:30.453  "Communication error : station B13, ..."
    13:52:30.813  "Communication error : station B2,  ..."

Different devices sharing a timestamp are not duplicates. The device identity
there lives inside the message text, not in a column, so there is no business key
to group on. `Alarms` contains zero full-row duplicates. These tables are
returned untouched.

Nothing here deletes or rewrites a historian record. The duplicate stays in the
database; it is excluded from a calculation, which is a different thing.
"""

from typing import Dict, Iterable, List, Optional, Sequence
import re

# Event logs: repeated timestamps are real, and the device is not a column.
EXCLUDED_TABLES = frozenset({"alarms", "sysmsgs", "drivers"})

# Historian bookkeeping. Never a measurement, never scored for blankness.
META_COLUMNS = frozenset({
    "TimeCol", "MSecCol", "LocalCol", "UserCol", "ReasonCol", "UniID", "TraID",
})

# Numeric SQL types, for deciding how a blank value is spelled.
_NUMERIC_TYPES = frozenset({
    "float", "real", "int", "bigint", "smallint", "tinyint",
    "decimal", "numeric", "money", "smallmoney", "bit",
})

# TimeCol truncated to the second. Anchored at a constant date rather than 0 so
# DATEDIFF cannot overflow its int return for datetimes far from 1900.
SECOND_KEY = "DATEADD(second, DATEDIFF(second, '2000-01-01', TimeCol), '2000-01-01')"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9_]+$")

# Marker column injected into the derived table. Never returned to a caller.
_RN = "_dedup_rn"


def _safe(name: str) -> bool:
    return bool(name) and bool(_SAFE_NAME.match(name))


def applies_to(table: str, columns: Optional[Dict[str, str]] = None) -> bool:
    """True when `table` is a measurement series this module should deduplicate.

    Requires `MSecCol`, which is what makes the retained record deterministic
    rather than whatever order the engine happens to return. Every one of the 85
    historian tables has it; the application's own tables (forecast_runs,
    saved_reports, ...) do not, and are left alone.
    """
    if not _safe(table) or table.lower() in EXCLUDED_TABLES:
        return False
    if columns is None:
        return True
    lower = {c.lower() for c in columns}
    return "timecol" in lower and "mseccol" in lower


def tag_columns(columns: Iterable[str]) -> List[str]:
    """The measurement columns of a historian table, bookkeeping removed."""
    return [c for c in columns if c not in META_COLUMNS]


def blank_score_expr(columns: Sequence[str], col_types: Dict[str, str]) -> str:
    """How many of `columns` read as empty: NULL, zero, or a false-ish string.

    A reconnect snapshot is blank across every tag, so scoring the columns the
    caller actually selected detects it just as well as scoring all 768 of them,
    and costs a fraction as much to read. Where a tag is legitimately zero - the
    whole plant at night - both members of the pair score the same and the
    sub-second sequence decides, which is the same record either way.
    """
    parts = []
    for c in columns:
        if not _safe(c) or c in META_COLUMNS:
            continue
        if (col_types.get(c) or "float").lower() in _NUMERIC_TYPES:
            parts.append(
                f"CASE WHEN [{c}] IS NULL OR CAST([{c}] AS FLOAT) = 0 THEN 1 ELSE 0 END"
            )
        else:
            parts.append(
                f"CASE WHEN [{c}] IS NULL "
                f"OR LOWER(CONVERT(NVARCHAR(32), [{c}])) IN ('', '0', 'false') "
                f"THEN 1 ELSE 0 END"
            )
    return "(" + " + ".join(parts) + ")" if parts else "0"


def order_by(prefix: str = "") -> str:
    """Deterministic row order for a historian table.

    `ORDER BY TimeCol` alone is not an order: 133 PPC rows share both TimeCol and
    MSecCol, and with OFFSET/FETCH a non-unique sort key lets the engine show one
    row on two consecutive pages and skip another. `TOP 1 ... ORDER BY TimeCol
    DESC` has the same problem at the newest timestamp, where it can return a
    different row on each call.
    """
    p = f"{prefix}." if prefix else ""
    return f"{p}TimeCol, {p}MSecCol"


def order_by_desc(prefix: str = "") -> str:
    """As `order_by`, newest first - for the `TOP 1` live-value reads."""
    p = f"{prefix}." if prefix else ""
    return f"{p}TimeCol DESC, {p}MSecCol DESC"


def source(
    table: str,
    columns: Sequence[str],
    col_types: Dict[str, str],
    where: Optional[str] = None,
) -> str:
    """A `FROM` target that yields one record per (device, tag, second).

    Drops in where the table name was: the result is aliased back to `[table]`,
    so the surrounding WHERE, GROUP BY and column references need no change.

        FROM [PPC]  ->  FROM <source("PPC", tags, types, where="TimeCol BETWEEN ...")>

    `where` is pushed *inside* the derived table on purpose. Without it the
    window function would rank the whole archive before the range filter applied,
    which would undo the TimeCol index seek the paged report queries depend on -
    the very thing that caused the 30-second timeouts these queries were tuned to
    avoid. Pass the same predicate the outer query uses; leaving it in the outer
    query as well is harmless.

    A table this module does not deduplicate comes back as plain `[table]`, so
    call sites need no special case for Alarms.
    """
    if not applies_to(table, col_types):
        return f"[{table}]"

    wanted = [c for c in tag_columns(columns) if _safe(c) and c in col_types]
    if not wanted:
        return f"[{table}]"

    inner_cols = ", ".join(f"[{c}]" for c in ["TimeCol", "MSecCol"] + wanted)
    blank = blank_score_expr(wanted, col_types)
    pred = f"WHERE {where}" if where else ""
    return (
        f"(SELECT {inner_cols} FROM ("
        f"SELECT {inner_cols}, ROW_NUMBER() OVER ("
        f"PARTITION BY {SECOND_KEY} "
        f"ORDER BY {blank} ASC, TimeCol ASC, MSecCol ASC) AS {_RN} "
        f"FROM [{table}] {pred}) AS _d WHERE {_RN} = 1) AS [{table}]"
    )


def count_source(
    table: str,
    col_types: Dict[str, str],
    where: Optional[str] = None,
) -> str:
    """A `FROM` target for counting retained records, cheaply.

    A row count does not need any tag value, only how many distinct seconds the
    range holds, so this groups on the key instead of ranking rows. Used where
    the old code counted raw rows and so reported 2,880 records for a 1,440-minute
    day.
    """
    if not applies_to(table, col_types):
        return f"[{table}]"
    pred = f"WHERE {where}" if where else ""
    return (
        f"(SELECT {SECOND_KEY} AS TimeCol FROM [{table}] {pred} "
        f"GROUP BY {SECOND_KEY}) AS [{table}]"
    )
