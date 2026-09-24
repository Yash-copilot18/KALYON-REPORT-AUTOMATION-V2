# app/services/schedule_service.py
"""
Business logic for database-backed scheduled reports.

One place owns: CRUD over `report_schedules`, next-run computation, executing a
schedule (generate the real report + e-mail it + log the run), and the periodic
"run everything that's due" sweep the background scheduler calls.

Scheduled Reports supports ONLY DGR / MGR / YGR, and each is generated through the
SAME service its manual counterpart on the Reports page uses — so scheduled output
is identical to manual output and there is no duplicated query/calculation logic:
  · DGR ("Daily Generation")   → ReportsService.get_report_data over [dbo].[PPC]
                                 + the manual report's counter→generation transform
  · MGR ("Monthly Generation") → mgr_service.get_monthly_generation (INVERTER_DAILY_GEN)
  · YGR ("Yearly Generation")  → ygr_service.get_yearly_report (plant-level)
  · files → report_excel.build_workbook (xlsx) / report_pdf (pdf)
  · e-mail → email_service.send_email_with_attachment
"""

import logging
from datetime import datetime, timedelta, date
from typing import List, Optional, Dict

from sqlalchemy.orm import Session

from app.database.session import SessionLocal
from app.models.schedules import ReportSchedule, ScheduleRun
from app.services import email_service, intervals
from app.services.report_files import PROJECT_NAME
from app.services.report_pdf import build_pdf_bytes, build_pdf_report, PDF_MIME

logger = logging.getLogger(__name__)

XLSX_MIME = ("application", "vnd.openxmlformats-officedocument.spreadsheetml.sheet")

_FMT = "%d/%m/%Y %H:%M:%S"


# ── Helpers ──────────────────────────────────────────────────────────────────
def _normalize_format(fmt: Optional[str]) -> str:
    """
    Scheduled Reports supports ONLY "Excel" and "PDF" (client requirement — CSV was
    removed). Any other/legacy value (e.g. a pre-existing "CSV" schedule) falls back
    to Excel, so old rows stay valid and generate a supported format.
    """
    return "PDF" if (fmt or "").strip().upper() == "PDF" else "Excel"


def _add_months(d: datetime, months: int) -> datetime:
    """Add whole months, clamping the day to the target month's length."""
    m = d.month - 1 + months
    year = d.year + m // 12
    month = m % 12 + 1
    # Last valid day of the target month.
    if month == 12:
        last = 31
    else:
        last = (date(year, month + 1, 1) - timedelta(days=1)).day
    return d.replace(year=year, month=month, day=min(d.day, last))


# The three generation reports. They keep their OWN dedicated builders (DGR/MGR/YGR);
# every OTHER equipment/report type the app exposes is generated through the generic
# Reports export path (build_multi_equipment_workbook / SMB / Tracker builders).
#   "Daily Generation"   → DGR (registry type, INVERTER_DAILY_GEN)
#   "Monthly Generation" → MGR (registry type, INVERTER_MONTHLY_GEN)
#   "Yearly Generation"  → YGR (plant-level, generated via ygr_service)
GENERATION_REPORT_TYPES = ("Daily Generation", "Monthly Generation", "Yearly Generation")
# Retained name for backward compatibility (imported by the router).
SUPPORTED_REPORT_TYPES = GENERATION_REPORT_TYPES

# Default time-of-day for scheduled execution — 8:30 PM (client requirement).
DEFAULT_SCHEDULE_TIME = "20:30"


# Plant-level reports cover the whole plant and therefore carry NO equipment
# identifier. Kept beside the type list so the rule lives in one place.
PLANT_LEVEL_REPORT_TYPES = ("Yearly Generation",)


def is_plant_level(equipment_type: Optional[str]) -> bool:
    """True when this report type has no equipment identifier by design."""
    return (equipment_type or "").strip() in PLANT_LEVEL_REPORT_TYPES


def is_supported_type(equipment_type: Optional[str]) -> bool:
    """
    Scheduling now supports EVERY equipment/report type the application exposes: the
    three generation reports use their dedicated builders, and all other equipment
    types are generated through the same generic Reports export path a manual export
    uses. So any non-empty report type is schedulable.
    """
    return bool((equipment_type or "").strip())


def _parse_time(value: Optional[str]) -> tuple:
    """'HH:MM' → (hour, minute); falls back to 20:30 on anything unparseable."""
    try:
        hh, mm = str(value or DEFAULT_SCHEDULE_TIME).strip().split(":")[:2]
        h, m = int(hh), int(mm)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return 20, 30


def compute_next_run(frequency: str, base: Optional[datetime] = None,
                     schedule_time: Optional[str] = DEFAULT_SCHEDULE_TIME,
                     *, allow_today: bool = False) -> datetime:
    """
    Next fire time one frequency-step after `base`, pinned to `schedule_time`
    (HH:MM, default 20:30). The time-of-day lives in next_run itself, so no schema
    change is needed to store the schedule time.

    `allow_today` — used when a schedule is CONFIGURED (created, re-timed, resumed):
    if today's occurrence of `schedule_time` is still in the future, that is the next
    run, so a schedule created at 14:40 for 20:30 fires TONIGHT rather than tomorrow.
    It stays False when advancing the clock AFTER a run: there the next fire must be
    strictly a full step later, otherwise a run at 12:17:07 for 12:17 would set
    next_run back to 12:17:00 — already past — and re-fire on every tick.
    """
    base = base or datetime.now()
    h, m = _parse_time(schedule_time)
    if allow_today:
        today_at = base.replace(hour=h, minute=m, second=0, microsecond=0)
        if today_at > base:
            return today_at
    f = (frequency or "Daily").strip().lower()
    if f == "weekly":
        nxt = base + timedelta(days=7)
    elif f == "monthly":
        nxt = _add_months(base, 1)
    else:
        nxt = base + timedelta(days=1)     # Daily (default)
    return nxt.replace(hour=h, minute=m, second=0, microsecond=0)


def _parse_date(v) -> Optional[date]:
    if v is None or v == "":
        return None
    if isinstance(v, date) and not isinstance(v, datetime):
        return v
    if isinstance(v, datetime):
        return v.date()
    # ISO 'YYYY-MM-DD' (the form's <input type=date> value)
    return datetime.strptime(str(v)[:10], "%Y-%m-%d").date()


def to_row(s: ReportSchedule, *, fallback_recipient: str = "") -> Dict:
    """
    Shape a schedule for the frontend, using the table's EXISTING field names so
    the Scheduled Reports UI needs no structural change.
    """
    return {
        "id":        s.id,
        "eq_type":   s.equipment_type,
        "eq_id":     s.equipment_id or "",
        "from":      s.from_date.isoformat() if s.from_date else "",
        "to":        s.to_date.isoformat() if s.to_date else "",
        "interval":  s.interval,
        "agg":       s.agg_function,
        "format":    _normalize_format(s.report_format),
        "freq":      s.frequency,
        "status":    s.status,
        "recipients": s.recipients or fallback_recipient or "",
        # Scheduled execution time-of-day (HH:MM) — carried in next_run's time part.
        "time":      s.next_run.strftime("%H:%M") if s.next_run else DEFAULT_SCHEDULE_TIME,
        "last_run":  s.last_run.strftime(_FMT) if s.last_run else "",
        "next_run":  s.next_run.strftime(_FMT) if s.next_run else "",
        "created":   s.created_at.strftime("%d/%m/%Y") if s.created_at else "",
    }


# ── CRUD ─────────────────────────────────────────────────────────────────────
def list_schedules(db: Session) -> List[ReportSchedule]:
    """
    Every schedule, newest first. All equipment/report types are supported now, so
    none are hidden by type.
    """
    return (db.query(ReportSchedule)
            .order_by(ReportSchedule.id.desc())
            .all())


def deactivate_unsupported_schedules(db: Session) -> int:
    """
    Historically this paused schedules whose report type was no longer supported.
    Scheduling now supports every equipment/report type, so the only rows paused here
    are ones with a BLANK report type (a data-integrity issue — they can't generate).
    Idempotent; safe to call at startup. Returns how many were changed.
    """
    stale = (db.query(ReportSchedule)
             .filter((ReportSchedule.equipment_type.is_(None)) | (ReportSchedule.equipment_type == ""),
                     ReportSchedule.status != "Paused")
             .all())
    for s in stale:
        s.status = "Paused"
    if stale:
        db.commit()
        logger.info("Paused %d schedule(s) with no report type.", len(stale))
    return len(stale)


def get_schedule(db: Session, schedule_id: int) -> Optional[ReportSchedule]:
    return db.query(ReportSchedule).filter(ReportSchedule.id == schedule_id).first()


def create_schedule(db: Session, data: Dict) -> ReportSchedule:
    freq = data.get("freq") or data.get("frequency") or "Daily"
    sched_time = data.get("time") or DEFAULT_SCHEDULE_TIME     # default 8:30 PM
    s = ReportSchedule(
        equipment_type=data.get("eq_type") or data.get("equipment_type") or "",
        equipment_id=data.get("eq_id") or data.get("equipment_id") or "",
        from_date=_parse_date(data.get("from")),
        to_date=_parse_date(data.get("to")),
        interval=data.get("interval") or "hourly",
        agg_function=data.get("agg") or data.get("agg_function") or "avg",
        report_format=_normalize_format(data.get("format")),
        frequency=freq,
        status=data.get("status") or "Active",
        # Persist the destination ON the schedule row so the saved schedule carries
        # everything the run needs. When the form supplies none, the currently
        # configured recipient is stored, so a later .env change never silently
        # re-targets an existing schedule. Only the address is stored — never any
        # SMTP credential.
        recipients=(normalize_recipients(data.get("recipients"))
                    or email_service.get_recipient_email() or None),
        # allow_today: a schedule created before its time-of-day fires the SAME day.
        next_run=compute_next_run(freq, schedule_time=sched_time, allow_today=True),
    )
    db.add(s)
    db.commit()
    db.refresh(s)
    logger.info("Schedule created -> id=%s | %s/%s | %s", s.id, s.equipment_type,
                s.equipment_id, s.frequency)
    return s


def update_schedule(db: Session, schedule_id: int, data: Dict) -> Optional[ReportSchedule]:
    s = get_schedule(db, schedule_id)
    if not s:
        return None
    if "eq_type" in data or "equipment_type" in data:
        s.equipment_type = data.get("eq_type") or data.get("equipment_type") or s.equipment_type
    if "eq_id" in data or "equipment_id" in data:
        s.equipment_id = data.get("eq_id") or data.get("equipment_id") or ""
    if "from" in data:     s.from_date = _parse_date(data.get("from"))
    if "to" in data:       s.to_date = _parse_date(data.get("to"))
    if "interval" in data: s.interval = data.get("interval") or s.interval
    if "agg" in data or "agg_function" in data:
        s.agg_function = data.get("agg") or data.get("agg_function") or s.agg_function
    if "format" in data:   s.report_format = _normalize_format(data.get("format"))
    if "recipients" in data:
        # The full edited list, normalised to the column's comma format. Clearing
        # every chip stores NULL, which falls back to the configured address.
        s.recipients = normalize_recipients(data.get("recipients")) or None
    # Current configured time-of-day (from next_run) — preserved unless the user
    # supplies a new one, so editing other fields never resets the schedule time.
    cur_time = s.next_run.strftime("%H:%M") if s.next_run else DEFAULT_SCHEDULE_TIME
    new_time = (data.get("time") or cur_time)
    new_freq = data.get("freq") or data.get("frequency") or s.frequency
    if ("freq" in data or "frequency" in data or "time" in data) and \
       (new_freq != s.frequency or new_time != cur_time):
        # re-base; allow_today so a newly-set time later today fires today
        s.next_run = compute_next_run(new_freq, schedule_time=new_time, allow_today=True)
    s.frequency = new_freq
    db.commit()
    db.refresh(s)
    logger.info("Schedule updated -> id=%s", s.id)
    return s


def delete_schedule(db: Session, schedule_id: int) -> bool:
    s = get_schedule(db, schedule_id)
    if not s:
        return False
    db.query(ScheduleRun).filter(ScheduleRun.schedule_id == schedule_id).delete()
    db.delete(s)
    db.commit()
    logger.info("Schedule deleted -> id=%s", schedule_id)
    return True


def set_status(db: Session, schedule_id: int, status: str) -> Optional[ReportSchedule]:
    s = get_schedule(db, schedule_id)
    if not s:
        return None
    s.status = "Paused" if status.lower() == "paused" else "Active"
    # Resuming re-bases next_run so a long-paused schedule doesn't fire immediately,
    # keeping the configured time-of-day. allow_today so resuming in the morning a
    # schedule set for the evening still runs tonight rather than tomorrow.
    if s.status == "Active" and (s.next_run is None or s.next_run < datetime.now()):
        cur_time = s.next_run.strftime("%H:%M") if s.next_run else DEFAULT_SCHEDULE_TIME
        s.next_run = compute_next_run(s.frequency, schedule_time=cur_time, allow_today=True)
    db.commit()
    db.refresh(s)
    logger.info("Schedule %s -> %s", schedule_id, s.status)
    return s


def list_runs(db: Session, schedule_id: int, limit: int = 50) -> List[ScheduleRun]:
    return (db.query(ScheduleRun)
            .filter(ScheduleRun.schedule_id == schedule_id)
            .order_by(ScheduleRun.execution_time.desc())
            .limit(limit).all())


# ── Execution (real report, reusing the Reports module) ──────────────────────
def _resolve_recipients(s: ReportSchedule) -> List[str]:
    """
    EVERY recipient saved on the schedule, in order. The `recipients` column has
    always held a comma-separated list; this now returns all of them instead of
    only the first, so one schedule e-mails its report to everybody configured on
    it. A schedule with no recipients of its own (older rows, or one saved before
    the address was stored) falls back to the .env address, so single-recipient
    and legacy schedules keep working untouched.
    """
    saved = email_service.parse_recipients(s.recipients)
    if saved:
        return saved
    fallback = email_service.get_recipient_email()
    return [fallback] if fallback else []


# Public alias — the router asks "who would this schedule e-mail?" before starting a
# manual send, and should not reach into a private helper to find out.
def recipients_for(s: ReportSchedule) -> List[str]:
    """Every address this schedule would send to, exactly as execution resolves them."""
    return _resolve_recipients(s)


def normalize_recipients(value) -> str:
    """
    Recipients as stored in the column: an ordered, de-duplicated comma-separated
    string. Accepts either a list (the API's new form) or a string (its original
    form), so the storage format — and every existing row — is unchanged.
    """
    return ", ".join(email_service.parse_recipients(value))


def invalid_recipients(value) -> List[str]:
    """Addresses in `value` that fail validation — for a precise API error."""
    return [a for a in email_service.parse_recipients(value)
            if not email_service.is_valid_email(a)]


# Yearly Generation Report columns (plant-level, from ygr_service) + their labels.
_YGR_COLS   = ["month", "generation", "peak", "pr", "cuf", "days"]
_YGR_LABELS = {
    "month": "Month", "generation": "Generation (MWh)", "peak": "Peak Power (MW)",
    "pr": "PR (%)", "cuf": "CUF (%)", "days": "Days",
}


def _build_ygr_file(db: Session, s: ReportSchedule):
    """
    Yearly Generation Report (YGR) — plant month-by-month table for a year, built
    via ygr_service (it is NOT an equipment/tag report, so it can't go through
    get_report_data). Year = the schedule's To-date year, else the current year.
    """
    from app.services import ygr_service, report_excel

    year = s.to_date.year if s.to_date else datetime.now().year
    ygr  = ygr_service.get_yearly_report(db, int(year))
    rows = ygr.get("months", [])
    label_fn = lambda c: _YGR_LABELS.get(c, c)     # noqa: E731 — tiny local mapper

    fmt   = _normalize_format(s.report_format)
    stamp = datetime.now().strftime("%d-%m-%Y")
    base  = f"YGR_{year}_Report_{stamp}"

    if fmt == "PDF":
        metadata = [
            ("Project Name",           PROJECT_NAME),
            ("Report Name",            "Yearly Generation Report (YGR)"),
            ("Year",                   str(year)),
            ("Generated Date & Time",  datetime.now().strftime(_FMT)),
            ("Total Generation (MWh)", str(ygr.get("total_generation"))),
        ]
        data = build_pdf_bytes(_YGR_COLS, rows,
                               title=f"Yearly Generation Report (YGR) — {year}",
                               metadata=metadata, header_fn=label_fn)
        return data, f"{base}.pdf", PDF_MIME[0], PDF_MIME[1]

    header = {
        "equipment_type": "Yearly Generation", "equipment_id": f"Year {year}",
        "from": f"01/01/{year}", "to": f"31/12/{year}",
        "interval": "Monthly", "agg": "Not applicable",
    }
    data = report_excel.build_workbook([(f"YGR {year}", header, _YGR_COLS, rows, label_fn)])
    return data, f"{base}.xlsx", XLSX_MIME[0], XLSX_MIME[1]


# Monthly Generation Report (MGR) columns/labels — matched to the manual MGR export
# (DGRReports page): a per-inverter monthly total table + a per-day plant total table.
_MGR_INV_COLS   = ["inverter", "generation"]
_MGR_INV_LABELS = {"inverter": "Inverter", "generation": "Monthly Generation (kWh)"}
_MGR_DAY_COLS   = ["date", "generation"]
_MGR_DAY_LABELS = {"date": "Date", "generation": "Generation (kWh)"}


def _mgr_period(s: ReportSchedule) -> tuple:
    """
    (month, year) for an MGR schedule, taken from the schedule's To-date — the
    reporting month chosen in the UI — falling back to the From-date, then the
    current month. NEVER a hardcoded month/year (client requirement).
    """
    ref = s.to_date or s.from_date or date.today()
    return ref.month, ref.year


def _build_mgr_file(db: Session, s: ReportSchedule):
    """
    Monthly Generation Report (MGR) — generated through the SAME service the manual
    MGR uses: mgr_service.get_monthly_generation, which aggregates the real
    per-inverter counters in [dbo].[INVERTER_DAILY_GEN]. So scheduled and manual MGR
    produce identical figures for the same month.

    Root-cause note: the previous implementation ran MGR through the generic
    get_report_data path against the [dbo].[INVERTER_MONTHLY_GEN] table, which is not
    populated — hence the "No data available" PDF. MGR data lives in INVERTER_DAILY_GEN.
    """
    import calendar
    from app.services import mgr_service, report_excel

    month, year = _mgr_period(s)
    period_from = datetime(year, month, 1)
    last_day    = calendar.monthrange(year, month)[1]
    period_to   = datetime(year, month, last_day, 23, 59, 59)
    month_label = period_from.strftime("%B %Y")

    # ── Pre-generation log (client requirement) ──────────────────────────────
    logger.info(
        "[MGR] Generating | ReportType=Monthly Generation (MGR) | Equipment=%s | "
        "FromDate=%s | ToDate=%s | Params(month=%d, year=%d) | "
        "Source=[dbo].[INVERTER_DAILY_GEN] via mgr_service.get_monthly_generation",
        s.equipment_id or "(plant-level)",
        s.from_date.isoformat() if s.from_date else "(none)",
        s.to_date.isoformat() if s.to_date else "(none)",
        month, year,
    )

    payload   = mgr_service.get_monthly_generation(db, month, year)
    inverters = payload.get("inverters", []) or []
    daily     = payload.get("daily", []) or []
    total     = payload.get("total_generation", 0.0) or 0.0

    logger.info(
        "[MGR] Rows returned | inverters=%d | daily_rows=%d | total_generation=%.3f %s",
        len(inverters), len(daily), total, payload.get("unit", "kWh"),
    )

    # ── Zero-row diagnostics (never silently emit an empty report) ────────────
    if not daily or not inverters or total <= 0:
        logger.error(
            "[MGR] NO DATA for %s: [dbo].[INVERTER_DAILY_GEN] returned %d daily row(s) "
            "and %d inverter column(s) with total generation %.3f for the window "
            "[%s .. %s]. Likely cause: no daily-generation telemetry stored for this "
            "month, or the schedule's To-date points at a month with no data. The MGR "
            "month is derived from the schedule's To-date (=%s).",
            month_label, len(daily), len(inverters), total,
            period_from.isoformat(), period_to.isoformat(),
            s.to_date.isoformat() if s.to_date else "(none → current month used)",
        )

    fmt   = _normalize_format(s.report_format)
    stamp = datetime.now().strftime("%d-%m-%Y")
    base  = f"MGR_{year}-{month:02d}_Report_{stamp}"
    title = f"Monthly Generation Report (MGR) — {month_label}"

    metadata = [
        ("Project Name",           PROJECT_NAME),
        ("Report Name",            "Monthly Generation Report (MGR)"),
        ("Month",                  month_label),
        ("Inverters",              str(len(inverters))),
        ("Days",                   str(len(daily))),
        ("Total Generation (kWh)", f"{total:.3f}"),
        ("Generated Date & Time",  datetime.now().strftime(_FMT)),
    ]
    inv_label = lambda c: _MGR_INV_LABELS.get(c, c)   # noqa: E731
    day_label = lambda c: _MGR_DAY_LABELS.get(c, c)   # noqa: E731

    if fmt == "PDF":
        sections = [
            (f"Monthly Generation by Inverter — {month_label}",
             _MGR_INV_COLS, inverters, inv_label),
            (f"Daily Generation Totals — {month_label}",
             _MGR_DAY_COLS, daily, day_label),
        ]
        data = build_pdf_report(title, sections, metadata=metadata)
        return data, f"{base}.pdf", PDF_MIME[0], PDF_MIME[1]

    frm, to = period_from.strftime("%d/%m/%Y"), period_to.strftime("%d/%m/%Y")
    inv_header = {"equipment_type": "Monthly Generation", "equipment_id": "By Inverter",
                  "from": frm, "to": to, "interval": "Monthly",
                  "agg": "Sum of daily peak counters"}
    day_header = {"equipment_type": "Monthly Generation", "equipment_id": "Daily Totals",
                  "from": frm, "to": to, "interval": "Daily", "agg": "Plant total"}
    data = report_excel.build_workbook([
        ("By Inverter",  inv_header, _MGR_INV_COLS, inverters, inv_label),
        ("Daily Totals", day_header, _MGR_DAY_COLS, daily,     day_label),
    ])
    return data, f"{base}.xlsx", XLSX_MIME[0], XLSX_MIME[1]


# Daily Generation Report (DGR) — matched to the manual DGR (DGRReports page).
# Source: real PPC plant-meter telemetry read through the SAME get_report_data service
# the manual report uses. PLANT_DAILY_PRODUCTION is a cumulative MWh counter that
# resets at midnight, so a bucket's generation is the counter's rise across it (kWh);
# GRID_ACTIVE_POWER_MEASURED (MW) gives the bucket's peak power. The counter→rows
# transform below is identical to the manual report's `toDailyRows`.
_DGR_COLS   = ["time", "generation", "peak", "cumulative"]
_DGR_LABELS = {
    "time": "Time", "generation": "Generation (kWh)",
    "peak": "Peak Power (MW)", "cumulative": "Cumulative Production (kWh)",
}
_DGR_PPC_ENERGY = "PLANT_DAILY_PRODUCTION"
_DGR_PPC_POWER  = "GRID_ACTIVE_POWER_MEASURED"
_DGR_DEFAULT_INTERVAL = "30min"     # matches the manual DGR page's default interval
_MWH_TO_KWH = 1000.0
_VALID_INTERVALS = {"raw", "1min", "5min", "15min", "30min", "hourly", "daily", "monthly"}


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hhmm(ts) -> str:
    """ISO timestamp → 'HH:MM' (matches the manual DGR's local HH:MM), else the raw value."""
    if isinstance(ts, datetime):
        return ts.strftime("%H:%M")
    s = str(ts or "")
    try:
        return datetime.fromisoformat(s.replace("Z", "")).strftime("%H:%M")
    except ValueError:
        return s


def _dgr_transform(raw_rows) -> List[Dict]:
    """
    Counter series → DGR rows, byte-for-byte equivalent to the manual report's
    `toDailyRows`: generation = max(0, cum − prev) × 1000 kWh (delta of the daily
    meter, guarded against the midnight reset), cumulative = cum × 1000 kWh, peak =
    the bucket's MW reading. `prev` advances only on a real reading, exactly as the UI.
    """
    prev = 0.0
    out: List[Dict] = []
    for r in raw_rows:
        cum = _num(r.get(_DGR_PPC_ENERGY))
        generation = None
        if cum is not None:
            generation = max(0.0, cum - prev) * _MWH_TO_KWH
            prev = cum
        out.append({
            "time":       _hhmm(r.get("timestamp")),
            "generation": generation,
            "peak":       _num(r.get(_DGR_PPC_POWER)),
            "cumulative": None if cum is None else cum * _MWH_TO_KWH,
        })
    return out


def _build_dgr_file(db: Session, s: ReportSchedule):
    """
    Daily Generation Report (DGR) — generated through the SAME service the manual DGR
    uses (ReportsService.get_report_data over [dbo].[PPC], PLANT_DAILY_PRODUCTION +
    GRID_ACTIVE_POWER_MEASURED, aggregation MAX), then transformed with the identical
    counter→generation rule. So scheduled and manual DGR are identical for the same
    date + interval.

    The DGR report definition FIXES the equipment (PPC), tags, and aggregation (MAX) —
    exactly as the manual report does — so only the date + interval vary per schedule.
    The old scheduled DGR instead read per-inverter INVERTER_DAILY_GEN columns via the
    generic path, which is a different report; that duplicate path has been removed.
    """
    from app.schemas.reports_schema import ReportDataRequest
    from app.services.reports_service import ReportsService
    from app.services import report_excel

    # DGR is a single-day report. The day is the schedule's To-date (the date chosen in
    # the UI), fallback From-date, then today. NEVER hardcoded.
    day     = s.to_date or s.from_date or date.today()
    from_dt = datetime.combine(day, datetime.min.time())
    to_dt   = datetime.combine(day, datetime.max.time().replace(microsecond=0))
    interval = s.interval if s.interval in _VALID_INTERVALS else _DGR_DEFAULT_INTERVAL

    # ── Pre-generation log (client requirement) ──────────────────────────────
    logger.info(
        "[DGR] Generating | ReportType=Daily Generation (DGR) | Equipment=PPC/PPC | "
        "FromDate=%s | ToDate=%s | Params(interval=%s, agg=max, tags=%s) | "
        "Source=[dbo].[PPC] via ReportsService.get_report_data (same as manual DGR)",
        from_dt.isoformat(), to_dt.isoformat(), interval,
        [_DGR_PPC_ENERGY, _DGR_PPC_POWER],
    )

    req = ReportDataRequest(
        equipment_type="PPC", equipment_id="PPC",
        tags=[_DGR_PPC_ENERGY, _DGR_PPC_POWER],
        from_datetime=from_dt, to_datetime=to_dt,
        interval=interval, agg_function="max",
        page=1, page_size=2000,
    )
    result   = ReportsService.get_report_data(db, req)
    raw_rows = result.get("rows", []) or []
    rows     = _dgr_transform(raw_rows)

    total_gen  = sum((r["generation"] or 0.0) for r in rows)
    peak_vals  = [r["peak"] for r in rows if r["peak"] is not None]
    peak_power = max(peak_vals) if peak_vals else 0.0

    logger.info(
        "[DGR] Rows returned | rows=%d | total_generation=%.3f kWh | peak_power=%.3f MW",
        len(rows), total_gen, peak_power,
    )

    # ── Zero-row diagnostics (never silently emit an empty report) ────────────
    if not rows:
        logger.error(
            "[DGR] NO DATA for %s: [dbo].[PPC] returned 0 rows via get_report_data "
            "(tags=%s, interval=%s, agg=max) for [%s .. %s]. Likely cause: no PPC "
            "telemetry stored for that date, or the schedule's To-date has no data.",
            day.isoformat(), [_DGR_PPC_ENERGY, _DGR_PPC_POWER], interval,
            from_dt.isoformat(), to_dt.isoformat(),
        )

    fmt        = _normalize_format(s.report_format)
    stamp      = datetime.now().strftime("%d-%m-%Y")
    base       = f"DGR_{day.isoformat()}_Report_{stamp}"
    date_label = day.strftime("%d/%m/%Y")
    title      = f"Daily Generation Report (DGR) — {date_label}"

    metadata = [
        ("Project Name",           PROJECT_NAME),
        ("Report Name",            "Daily Generation Report (DGR)"),
        ("Report Date",            date_label),
        ("Time Interval",          intervals.interval_label(interval)),
        ("Intervals",              str(len(rows))),
        ("Total Generation (kWh)", f"{total_gen:.3f}"),
        ("Peak Power (MW)",        f"{peak_power:.3f}"),
        ("Generated Date & Time",  datetime.now().strftime(_FMT)),
    ]
    label_fn = lambda c: _DGR_LABELS.get(c, c)   # noqa: E731

    if fmt == "PDF":
        data = build_pdf_bytes(_DGR_COLS, rows, title=title, metadata=metadata,
                               header_fn=label_fn)
        return data, f"{base}.pdf", PDF_MIME[0], PDF_MIME[1]

    header = {"equipment_type": "Daily Generation", "equipment_id": f"Date {date_label}",
              "from": from_dt.strftime("%d/%m/%Y %H:%M"),
              "to":   to_dt.strftime("%d/%m/%Y %H:%M"),
              "interval": intervals.interval_label(interval), "agg": "Maximum (counter close)"}
    data = report_excel.build_workbook([(f"DGR {date_label}", header, _DGR_COLS, rows, label_fn)])
    return data, f"{base}.xlsx", XLSX_MIME[0], XLSX_MIME[1]


# Intervals accepted for generic equipment reports (mirrors the Reports page).
_EQUIP_VALID_INTERVALS = {"raw", "1min", "5min", "15min", "30min", "hourly", "daily", "monthly"}


def _build_equipment_file(db: Session, s: ReportSchedule):
    """
    Generic equipment/report-type file for a schedule — generated through the SAME
    services a manual Reports export uses, so scheduled output matches manual output:
      · String Combiner             → smb_excel.build_smb_workbook (per-SMB sheets)
      · Tracker / T1 / T2 Isolation → t1_isolation_excel.generate_t1_isolation_export
      · every other equipment type  → report_excel.build_multi_equipment_workbook
      · PDF                         → report_pdf from the same get_report_data rows

    Scheduling uses a SINGLE date; the report covers that whole day. The equipment's
    full column set is auto-discovered (exactly like the Reports page auto-selecting
    every available tag) — nothing about the query/calculation logic changes.
    """
    from app.schemas.reports_schema import ReportDataRequest
    from app.repositories.reports_repository import ReportsRepository

    eq_id = (s.equipment_id or "").strip()
    if not eq_id:
        raise ValueError(f"Schedule id={s.id} ({s.equipment_type}) has no equipment identifier")

    day     = s.to_date or s.from_date or date.today()
    from_dt = datetime.combine(day, datetime.min.time())
    to_dt   = datetime.combine(day, datetime.max.time().replace(microsecond=0))
    interval = s.interval if s.interval in _EQUIP_VALID_INTERVALS else "hourly"
    agg      = s.agg_function or "avg"

    # Full tag set for this equipment. ReportDataRequest requires ≥1 tag; the workbook
    # builders re-discover each equipment's own columns, so this also drives the report.
    tag_cols = [t["column_name"] for t in ReportsRepository.get_tags(db, s.equipment_type, eq_id)]
    if not tag_cols:
        raise ValueError(f"No columns found for {s.equipment_type} / {eq_id}")

    req = ReportDataRequest(
        equipment_type=s.equipment_type, equipment_id=eq_id, tags=tag_cols,
        from_datetime=from_dt, to_datetime=to_dt,
        interval=interval, agg_function=agg, page=1, page_size=100000,
    )

    logger.info(
        "[EQUIP] Generating | type=%s | equipment=%s | date=%s | interval=%s | agg=%s | tags=%d",
        s.equipment_type, eq_id, day.isoformat(), interval, agg, len(tag_cols),
    )

    fmt      = _normalize_format(s.report_format)
    stamp    = datetime.now().strftime("%d-%m-%Y")
    safe_eid = eq_id.replace(" ", "_").replace("/", "_")

    # ── PDF (generic single-equipment table from the same report data) ───────────
    if fmt == "PDF":
        from app.services.reports_service import ReportsService
        result  = ReportsService.get_report_data(db, req)
        columns = result.get("columns") or ["timestamp"]
        rows    = result.get("rows", []) or []
        tag_label = {
            t["column_name"]: (f"{t['tag']} ({t['unit']})" if t.get("unit") else (t.get("tag") or t["column_name"]))
            for t in ReportsRepository.get_tags(db, s.equipment_type, eq_id)
        }
        def label_fn(c):  # noqa: E306
            if c == "timestamp":  return "Timestamp"
            if c == "_equipment": return "Equipment"
            return tag_label.get(c, c)
        metadata = [
            ("Project Name",          PROJECT_NAME),
            ("Report Name",           f"{s.equipment_type} Report"),
            ("Equipment",             eq_id),
            ("Report Date",           day.strftime("%d/%m/%Y")),
            ("Time Interval",         intervals.interval_label(interval)),
            ("Rows",                  str(len(rows))),
            ("Generated Date & Time", datetime.now().strftime(_FMT)),
        ]
        data = build_pdf_bytes(columns, rows, title=f"{s.equipment_type} Report — {eq_id}",
                               metadata=metadata, header_fn=label_fn)
        return data, f"{safe_eid}_Report_{stamp}.pdf", PDF_MIME[0], PDF_MIME[1]

    # ── Excel — dispatch to the type's specific builder (same as manual export) ──
    if s.equipment_type == "String Combiner":
        from app.services.smb_excel import build_smb_workbook
        out, filename, ctype = build_smb_workbook(db, req, [eq_id])
        data = out.getvalue() if hasattr(out, "getvalue") else out
        main, _, sub = ctype.partition("/")
        return data, filename, main or XLSX_MIME[0], sub or XLSX_MIME[1]

    if s.equipment_type in ("Tracker", "T1 Isolation", "T2 Isolation"):
        from app.services.t1_isolation_excel import generate_t1_isolation_export
        data, filename, ctype = generate_t1_isolation_export(req, [eq_id], None)
        main, _, sub = ctype.partition("/")
        return data, filename, main or XLSX_MIME[0], sub or XLSX_MIME[1]

    from app.services import report_excel
    data, filename = report_excel.build_multi_equipment_workbook(req, [eq_id])
    return data, filename, XLSX_MIME[0], XLSX_MIME[1]


def _build_report_file(db: Session, s: ReportSchedule):
    """
    Build the real report file for a schedule. Returns
    (data, filename, mime_main, mime_sub). Raises on failure (caller logs it).

    Each type is generated through the SAME service its manual counterpart uses, so
    scheduled output == manual output — no duplicated query/calculation logic:
      · DGR ("Daily Generation")   → get_report_data over [dbo].[PPC]  (+ counter rule)
      · MGR ("Monthly Generation") → mgr_service   (INVERTER_DAILY_GEN aggregation)
      · YGR ("Yearly Generation")  → ygr_service   (plant-level month-by-month)
      · any other equipment type   → _build_equipment_file (generic Reports export)
    """
    if s.equipment_type == "Daily Generation":
        return _build_dgr_file(db, s)
    if s.equipment_type == "Monthly Generation":
        return _build_mgr_file(db, s)
    if s.equipment_type == "Yearly Generation":
        return _build_ygr_file(db, s)
    # Every other supported equipment/report type (Inverter, String Combiner, WMS, PPC,
    # Tracker, Alarms, …) → the SAME generic Reports export path used by manual exports.
    return _build_equipment_file(db, s)


def _generate_and_email(db: Session, s: ReportSchedule, t0: datetime,
                        recipients: List[str], *, kind: str, label: str) -> Dict:
    """
    THE generation + e-mail step, shared by every path that sends a report.

    It builds the file through _build_report_file and hands it to email_service —
    the same queries, the same Excel/PDF builders, the same SMTP configuration, the
    same recipient list and the same attachment handling — so a scheduled send, a
    manual send of a saved schedule and an ad-hoc send can never drift apart.

    It writes NOTHING to the database. Recording the attempt (a ScheduleRun) and
    advancing the clock belong to the caller, because an ad-hoc send has no schedule
    row to record against.

    Returns {"ok": bool, "filename": str|None, "size": int|None, "error": str|None}.
    """
    fmt = _normalize_format(s.report_format)
    logger.info("[%s] Generating %s -> %s/%s | fmt=%s | to=%d recipient(s) %s",
                kind, label, s.equipment_type, s.equipment_id, fmt,
                len(recipients), recipients)

    # Fail fast if SMTP isn't configured. Only the missing NAMES are reported.
    missing = email_service.get_missing_config()
    if missing:
        err = ("SMTP is not configured. Missing: " + ", ".join(missing)
               + ". Set them in Backend/.env (see .env.example).")
        logger.error("[%s] %s aborted -> %s", kind, label, err)
        return {"ok": False, "filename": None, "size": None, "error": err}

    try:
        data, filename, mime_main, mime_sub = _build_report_file(db, s)
    except Exception as e:  # noqa: BLE001
        err = f"Report generation failed: {type(e).__name__}: {e}"
        logger.error("[%s] %s -> %s", kind, label, err, exc_info=True)
        return {"ok": False, "filename": None, "size": None, "error": err}

    subject = f"Kalyon Scheduled Report - {s.equipment_type}/{s.equipment_id} - {t0.strftime(_FMT)}"
    body = (
        "Kalyon Solar Monitoring — Automated Report\n\n"
        f"Equipment : {s.equipment_type} / {s.equipment_id}\n"
        f"Format    : {fmt}\n"
        f"Frequency : {s.frequency}\n"
        f"Generated : {t0.strftime(_FMT)}\n\n"
        "The requested report is attached.\n"
    )
    result = email_service.send_email_with_attachment(
        to_email=recipients, subject=subject, body=body,
        attachment_bytes=data, attachment_filename=filename,
        mime_main=mime_main, mime_sub=mime_sub,
    )
    if result.get("success"):
        logger.info("[%s] %s -> SUCCESS | %s | delivered to %d recipient(s)",
                    kind, label, filename, len(recipients))
        return {"ok": True, "filename": filename, "size": len(data), "error": None}

    err = result.get("error")
    logger.error("[%s] %s -> email FAILED | %s", kind, label, err)
    return {"ok": False, "filename": filename, "size": len(data), "error": err}


def execute_schedule(db: Session, s: ReportSchedule, *, kind: str = "RUN") -> Dict:
    """
    Generate the real report, e-mail it, record a ScheduleRun, and advance
    last_run / next_run. Returns a status dict (also the Run-Now HTTP response).
    """
    t0 = datetime.now()
    recipients = _resolve_recipients(s)                 # ALL saved addresses
    recipient = ", ".join(recipients)                   # display/report form
    fmt = _normalize_format(s.report_format)

    logger.info("[%s] Executing schedule id=%s -> %s/%s | fmt=%s | to=%d recipient(s) %s",
                kind, s.id, s.equipment_type, s.equipment_id, fmt, len(recipients), recipients)

    run = ScheduleRun(schedule_id=s.id, execution_time=t0, status="Failed",
                      report=None, data_source="database", error=None)

    def _finalize(status_str: str):
        run.status = status_str
        run.duration_ms = int((datetime.now() - t0).total_seconds() * 1000)
        db.add(run)
        # Advance the clock regardless of success so a failing schedule doesn't
        # hammer every tick; Run-Now also refreshes last_run for the UI. The
        # configured time-of-day (from the current next_run) is carried forward.
        cur_time = s.next_run.strftime("%H:%M") if s.next_run else DEFAULT_SCHEDULE_TIME
        s.last_run = t0
        s.next_run = compute_next_run(s.frequency, base=t0, schedule_time=cur_time)
        db.commit()
        return {
            "status":         "Success" if status_str == "Success" else "Failed",
            "execution_time": t0.strftime(_FMT),
            "recipient":      recipient,
            "report":         run.report,
            "error":          run.error,
        }

    outcome = _generate_and_email(db, s, t0, recipients,
                                  kind=kind, label=f"schedule id={s.id}")
    run.report    = outcome["filename"]
    run.file_size = outcome["size"]
    run.error     = outcome["error"]
    return _finalize("Success" if outcome["ok"] else "Failed")


def generate_and_send_adhoc(db: Session, data: Dict) -> Dict:
    """
    ONE-OFF "Generate & Send": build the report described by the form and e-mail it
    immediately. NOTHING is persisted - no ReportSchedule, no ScheduleRun, no clock
    change - so this never appears in the Report Schedules list.

    The configuration is carried on a TRANSIENT ReportSchedule that is deliberately
    never added to the session. That is what lets the send go through the very same
    _generate_and_email the scheduler uses, instead of a second, manual-only report
    implementation that could drift out of step.
    """
    t0 = datetime.now()
    s = ReportSchedule(
        equipment_type=data.get("eq_type") or data.get("equipment_type") or "",
        equipment_id=data.get("eq_id") or data.get("equipment_id") or "",
        from_date=_parse_date(data.get("from")),
        to_date=_parse_date(data.get("to")),
        interval=data.get("interval") or "hourly",
        agg_function=data.get("agg") or data.get("agg_function") or "avg",
        report_format=_normalize_format(data.get("format")),
        frequency=data.get("freq") or data.get("frequency") or "Daily",
        status="Adhoc",
        recipients=normalize_recipients(data.get("recipients")) or None,
    )
    recipients = _resolve_recipients(s)

    outcome = _generate_and_email(db, s, t0, recipients,
                                  kind="ADHOC", label="ad-hoc send")
    return {
        "status":         "Success" if outcome["ok"] else "Failed",
        "execution_time": t0.strftime(_FMT),
        "recipient":      ", ".join(recipients),
        "report":         outcome["filename"],
        "error":          outcome["error"],
    }


def run_schedule_now(db: Session, schedule_id: int) -> Optional[Dict]:
    s = get_schedule(db, schedule_id)
    if not s:
        return None
    return execute_schedule(db, s, kind="RUN")


def run_due_schedules() -> int:
    """
    Executed by the background scheduler each tick: run every Active schedule whose
    next_run has passed. Own DB session; one bad schedule never aborts the batch.
    Returns the number executed.
    """
    db = SessionLocal()
    executed = 0
    try:
        now = datetime.now()
        due = (db.query(ReportSchedule)
               .filter(ReportSchedule.status == "Active",
                       ReportSchedule.next_run.isnot(None),
                       ReportSchedule.next_run <= now)
               .all())
        if due:
            logger.info("Scheduler tick -> %d schedule(s) due", len(due))
        for s in due:
            try:
                execute_schedule(db, s, kind="AUTO")
                executed += 1
            except Exception as e:  # noqa: BLE001
                logger.error("AUTO run failed for schedule id=%s: %s", s.id, e, exc_info=True)
                db.rollback()
    finally:
        db.close()
    return executed
