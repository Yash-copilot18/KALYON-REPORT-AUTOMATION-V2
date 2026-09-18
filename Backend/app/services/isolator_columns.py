# app/services/isolator_columns.py
"""
Shared column model for the `dbo.T1_IS*` / `dbo.T2_IS*` isolator tables.

These tables store N isolators side-by-side as per-ID columns
(`<PARAM>_ID<n>`, e.g. ALARM_ID1 … OPERATION_MODE_ID126). This module is the
SINGLE source of truth for that layout — ID discovery, safe column generation
and header/label formatting — reused by:

  • isolator_service.py     (dynamic ID-scoped JSON data API + live status)
  • tracker_service.py      (per-tracker Excel + preview, dbo.T1_IS2)
  • isolator_excel.py       (generic per-tracker Excel export)

Design goals (production):
  • Never SELECT *. Columns are generated programmatically from the selected IDs
    and validated against the live schema (whitelist) — injection-safe.
  • IDs are DISCOVERED from the schema, never hardcoded, so adding IS127+ needs
    zero code changes.
"""

import re
from typing import Dict, List, Tuple, Iterable

from sqlalchemy.orm import Session

from app.services import schema_cache
from app.repositories.reports_repository import _safe_name

# (db column prefix, display label, unit) — the six parameters per isolator.
PARAMS: List[Tuple[str, str, str]] = [
    ("ALARM",              "Alarm",              ""),
    ("BATTERY_LEVEL",      "Battery Level",      "%"),
    ("ELEVATION_POSITION", "Elevation Position", "deg"),
    ("ELEVATION_SETPOINT", "Elevation Setpoint", "deg"),
    ("MAX_MOTOR_CURRENT",  "Max Motor Current",  "A"),
    ("OPERATION_MODE",     "Operation Mode",     ""),
]
PREFIXES   = [p[0] for p in PARAMS]
PREFIX_SET = set(PREFIXES)
PER_UNIT   = len(PREFIXES)

_LABELS = {p[0]: p[1] for p in PARAMS}
_UNITS  = {p[0]: p[2] for p in PARAMS}
_ID_RE  = re.compile(r"^(?P<prefix>.+)_ID(?P<id>\d+)$")

# Only these isolator table families may be queried (defence-in-depth alongside
# the schema-existence + _safe_name checks). Matches T1_IS4, T2_IS17, …
_TABLE_RE = re.compile(r"^T\d+_IS\d+$", re.IGNORECASE)


def is_isolator_table(table: str) -> bool:
    return bool(table) and _safe_name(table) and bool(_TABLE_RE.match(table))


# The operator-facing report types whose columns carry the per-device _ID<n> suffix.
# Anything NOT in this set (Inverter, WMS, PPC, Alarms, MFM, String Combiner, …) keeps
# its own column order untouched.
ISOLATION_EQUIPMENT_TYPES = frozenset({"Tracker", "T1 Isolation", "T2 Isolation"})

# Same shape as _ID_RE but tolerant of casing, for ordering caller-supplied tag lists.
ID_COL_RE = re.compile(r"^(.*)_ID(\d+)$", re.IGNORECASE)


def order_columns_by_id(cols: Iterable[str]) -> List[str]:
    """
    THE single column order for every isolator/Tracker output — the Excel export and
    the Report Data table both call this, so the UI can never drift from the workbook.

    Columns are grouped BY DEVICE Id first, Ids ascending, and within one Id the tags
    follow the canonical parameter order (ALARM, BATTERY_LEVEL, ELEVATION_POSITION,
    ELEVATION_SETPOINT, MAX_MOTOR_CURRENT, OPERATION_MODE, … — alphabetical by prefix,
    which is the order PARAMS declares):

        ALARM_ID1, BATTERY_LEVEL_ID1, … OPERATION_MODE_ID1,
        ALARM_ID2, BATTERY_LEVEL_ID2, … OPERATION_MODE_ID2, …

    NOT tag-first (ALARM_ID1 … ALARM_ID40, BATTERY_LEVEL_ID1 …).

    The Ids and tags are read from the column NAMES themselves, so the grouping is
    driven entirely by the live schema — no hardcoded tag list, no device-count limit.
    Only the columns passed in are ordered: a tag missing for some Id simply leaves no
    gap. Any column without an _ID<n> suffix keeps its relative order and is placed
    after the Id-grouped ones (the caller adds `timestamp` as the first column).
    """
    def key(c: str):
        m = ID_COL_RE.match(c)
        if m:
            return (0, int(m.group(2)), m.group(1).upper())
        return (1, 0, "")          # non-Id columns last; stable sort keeps their order
    return sorted(cols, key=key)


def id_label(tid: int) -> str:
    """1 → 'IS01', 126 → 'IS126' (min two digits)."""
    return f"IS{tid:02d}"


def discover_ids(db: Session, table: str) -> Tuple[List[int], set]:
    """
    All isolator IDs present in `table` (an ID needs ALL six params), plus the
    set of the table's column names. Discovered from the schema cache — no
    per-request INFORMATION_SCHEMA round trip, never hardcoded.
    """
    cols = set(schema_cache.get_columns(db, table))
    have: Dict[int, set] = {}
    for c in cols:
        m = _ID_RE.match(c)
        if m and m.group("prefix") in PREFIX_SET:
            have.setdefault(int(m.group("id")), set()).add(m.group("prefix"))
    ids = sorted(tid for tid, present in have.items() if PREFIX_SET.issubset(present))
    return ids, cols


def validate_ids(requested: Iterable, available: Iterable[int]) -> List[int]:
    """Keep only requested IDs that exist, de-duplicated, preserving caller order."""
    avail = set(available)
    seen, out = set(), []
    for tid in requested:
        try:
            tid = int(tid)
        except (TypeError, ValueError):
            continue
        if tid in avail and tid not in seen:
            out.append(tid)
            seen.add(tid)
    return out


def columns_for_ids(ids: Iterable[int], valid_cols: set) -> List[str]:
    """
    Programmatic, whitelisted column list for the selected IDs — six per ID in
    parameter order (ALARM_ID{n}, BATTERY_LEVEL_ID{n}, …). Only columns that
    exist in `valid_cols` and pass the injection guard are emitted. Never *.
    """
    out: List[str] = []
    for tid in ids:
        for pfx in PREFIXES:
            col = f"{pfx}_ID{tid}"
            if col in valid_cols and _safe_name(col):
                out.append(col)
    return out


def parse_column(col: str) -> Tuple[str, int]:
    """'ALARM_ID5' → ('ALARM', 5); returns ('', -1) if it isn't a param column."""
    m = _ID_RE.match(col)
    if m and m.group("prefix") in PREFIX_SET:
        return m.group("prefix"), int(m.group("id"))
    return "", -1


def all_param_columns(valid_cols: Iterable[str]) -> List[str]:
    """
    Every `<PREFIX>_ID<n>` parameter column present in the schema — the FULL tag
    universe (what the UI's tag list shows), independent of whether each ID
    exposes all six params. Ordered by (id, param-order) for stable grouping.
    Used by exporters to assert nothing is dropped.
    """
    parsed = [(c, *parse_column(c)) for c in valid_cols]
    return [c for c, _, tid in
            sorted((p for p in parsed if p[2] >= 0),
                   key=lambda p: (p[2], PREFIXES.index(p[1])))]


def col_label(col: str) -> str:
    """React table label, ID-qualified: 'ALARM_ID5' → 'IS05 · Alarm'."""
    if col == "timestamp" or col == "TimeCol":
        return "Timestamp"
    prefix, tid = parse_column(col)
    if tid >= 0:
        return f"{id_label(tid)} · {_LABELS[prefix]}"
    return col.replace("_", " ").title()


def col_unit(col: str) -> str:
    prefix, tid = parse_column(col)
    return _UNITS.get(prefix, "") if tid >= 0 else ""


def col_header(col: str) -> str:
    """Excel header: 'Label (unit)' (units already carried elsewhere for exports)."""
    if col in ("timestamp", "TimeCol"):
        return "Timestamp (DD/MM/YYYY HH:MM:SS)"
    prefix, tid = parse_column(col)
    if tid >= 0:
        u = _UNITS[prefix]
        return f"{_LABELS[prefix]} ({u})" if u else _LABELS[prefix]
    return col.replace("_", " ").title()


def param_meta() -> List[dict]:
    """Parameter definitions for the UI."""
    return [{"key": k, "label": lbl, "unit": u} for k, lbl, u in PARAMS]
