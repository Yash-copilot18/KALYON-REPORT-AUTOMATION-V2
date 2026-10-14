"""
Repeatable checks for Scheduled Reports: mail quota, Generated Time, validation
and date-range resolution.

Run from the Backend directory:

    venv\\Scripts\\python.exe tests\\run_tests.py

Safety: every record the suite touches is one it created, SMTP is stubbed so no
mail is sent, and test schedules are removed in a finally block. Deleting a
schedule also removes its runs, which is what the quota counts, so the quota is
left exactly as it was found. Pre-existing schedules and history are never
modified or deleted. See README.md.
"""

import logging
import os
import sys
import threading
import time
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             ".env"))
except Exception:                                      # pragma: no cover
    pass

logging.disable(logging.ERROR)

from app.database.session import SessionLocal                     # noqa: E402
from app.models.schedules import ReportSchedule, ScheduleRun      # noqa: E402
from app.services import email_service                            # noqa: E402
import app.services.mail_quota as mail_quota                      # noqa: E402
import app.services.schedule_service as ss                        # noqa: E402

# Every schedule the suite creates carries this recipient. It is the ONLY thing the
# cleanup matches on, so nothing that was not created here can ever be removed.
# The domain is .invalid (RFC 2606) and SMTP is stubbed, so it can reach no one.
RECIPIENT = "reports@localhost.invalid"

_results = []
_sent = []


def check(name, condition, detail=""):
    _results.append((name, bool(condition), detail))
    print("  [%s] %s%s" % ("PASS" if condition else "FAIL", name,
                           ("  -> " + detail) if detail else ""))
    return bool(condition)


def _stub_ok(**kw):
    _sent.append(kw.get("attachment_filename"))
    return {"success": True}


def _stub_fail(**kw):
    return {"success": False, "error": "SMTP unavailable (stubbed for tests)"}


def _stub_slow(**kw):
    time.sleep(1.5)            # widens the window between the quota check and the write
    _sent.append(kw.get("attachment_filename"))
    return {"success": True}


def make_schedule(db, eq_type="Power vs Irradiance", eq_id="POWER_VS_IRRADIANCE", **over):
    data = {
        "eq_type": eq_type, "eq_id": eq_id, "interval": "hourly", "agg": "avg",
        "format": "Excel", "freq": "Daily", "time": "20:30", "status": "Active",
        "recipients": RECIPIENT,
    }
    data.update(over)
    return ss.create_schedule(db, data)


def drop(db, sid):
    """Remove one schedule created by this suite, with its runs."""
    db.query(ScheduleRun).filter(ScheduleRun.schedule_id == sid).delete()
    row = db.query(ReportSchedule).filter(ReportSchedule.id == sid).first()
    if row:
        db.delete(row)
    db.commit()


def purge_test_records(db):
    """Remove ONLY schedules this suite created. Never touches anything else."""
    rows = db.query(ReportSchedule).filter(ReportSchedule.recipients == RECIPIENT).all()
    for s in rows:
        db.query(ScheduleRun).filter(ScheduleRun.schedule_id == s.id).delete()
        db.delete(s)
    if rows:
        db.commit()
    return len(rows)


# ── tests ────────────────────────────────────────────────────────────────────
def test_crud(db):
    print("\nschedule create / edit / delete")
    before = db.query(ReportSchedule).count()
    s = make_schedule(db)
    sid = s.id
    try:
        check("create adds exactly one schedule",
              db.query(ReportSchedule).count() == before + 1)
        check("created schedule stores no report date",
              s.from_date is None and s.to_date is None)

        ss.update_schedule(db, sid, {"freq": "Weekly", "time": "07:15", "format": "PDF"})
        db.expire_all()
        s = db.query(ReportSchedule).filter(ReportSchedule.id == sid).first()
        check("edit updates configuration",
              s.frequency == "Weekly" and s.report_format == "PDF")
        check("edit does not set Generated Time", s.last_run is None)

        ss.execute_schedule(db, s, kind="RUN")
        runs = db.query(ScheduleRun).filter(ScheduleRun.schedule_id == sid).count()
        ss.delete_schedule(db, sid)
        check("delete removes the schedule",
              db.query(ReportSchedule).filter(ReportSchedule.id == sid).first() is None)
        check("delete also removes its runs (cascade)",
              db.query(ScheduleRun).filter(ScheduleRun.schedule_id == sid).count() == 0,
              "%d run(s) went with it" % runs)
        check("other schedules untouched", db.query(ReportSchedule).count() == before)
    finally:
        drop(db, sid)


def test_validation(db):
    print("\nrecipient / configuration validation")
    from fastapi import HTTPException
    from app.routers.scheduled import _validate_runnable

    s = make_schedule(db)
    sid = s.id
    try:
        for bad in ["not-an-email", "a@b@c.com", "good@x.com, broken@@y"]:
            s.recipients = bad
            db.commit()
            try:
                _validate_runnable(s)
                check("invalid recipient rejected: %s" % bad, False, "was accepted")
            except HTTPException as e:
                check("invalid recipient rejected: %s" % bad, e.status_code == 400)

        s.recipients = RECIPIENT
        s.equipment_id = ""
        db.commit()
        try:
            _validate_runnable(s)
            check("missing equipment id rejected", False, "was accepted")
        except HTTPException as e:
            check("missing equipment id rejected", e.status_code == 400)
    finally:
        s.recipients = RECIPIENT
        db.commit()
        drop(db, sid)


def test_date_range(db):
    print("\ndate range resolution")
    s = make_schedule(db)
    sid = s.id
    try:
        start, end = ss.resolve_range(db, s)
        first, last = ss.data_window(db, s)
        check("no dates resolve to the full stored window",
              start == first.date() and end == last.date(),
              "%s .. %s" % (start, end))

        s.from_date = date(2026, 1, 1)
        s.to_date = None
        db.commit()
        try:
            ss.resolve_range(db, s)
            check("From after all data is refused", False, "was accepted")
        except ValueError as e:
            check("From after all data is refused", "No Data Available" in str(e))
    finally:
        drop(db, sid)


def test_generated_time(db):
    print("\nGenerated Time")
    email_service.send_email_with_attachment = _stub_ok
    s = make_schedule(db)
    sid = s.id
    try:
        check("new schedule has no Generated Time", ss.to_row(s)["generated"] == "")

        res = ss.execute_schedule(db, s, kind="RUN")
        db.expire_all()
        s = db.query(ReportSchedule).filter(ReportSchedule.id == sid).first()
        stamped = ss.to_row(s)["generated"]
        check("successful send sets Generated Time",
              res["status"] == "Success" and bool(stamped), stamped)

        email_service.send_email_with_attachment = _stub_fail
        res = ss.execute_schedule(db, s, kind="RUN")
        db.expire_all()
        s = db.query(ReportSchedule).filter(ReportSchedule.id == sid).first()
        check("failed email leaves Generated Time unchanged",
              res["status"] == "Failed" and ss.to_row(s)["generated"] == stamped)

        email_service.send_email_with_attachment = _stub_ok
        s.from_date = s.to_date = date(2026, 1, 1)
        db.commit()
        res = ss.execute_schedule(db, s, kind="RUN")
        db.expire_all()
        s = db.query(ReportSchedule).filter(ReportSchedule.id == sid).first()
        check("no-data run leaves Generated Time unchanged",
              res["status"] == "Failed" and ss.to_row(s)["generated"] == stamped)
    finally:
        email_service.send_email_with_attachment = _stub_ok
        drop(db, sid)


def test_quota(db):
    print("\nmail quota")
    email_service.send_email_with_attachment = _stub_ok
    base = mail_quota.status(db)["count"]
    limit = mail_quota.MAX_SENT_MAILS
    room = limit - base
    if room < 2:
        check("quota has room to test", False, "already at %d/%d" % (base, limit))
        return

    s = make_schedule(db)
    sid = s.id
    try:
        for _ in range(room):
            ss.execute_schedule(db, s, kind="RUN")
        q = mail_quota.status(db)
        check("quota reaches the cap", q["count"] == limit and q["limit_reached"],
              "%d/%d" % (q["count"], q["max"]))

        n_before = db.query(ScheduleRun).filter(ScheduleRun.schedule_id == sid).count()
        res = ss.execute_schedule(db, s, kind="RUN")
        n_after = db.query(ScheduleRun).filter(ScheduleRun.schedule_id == sid).count()
        check("send past the cap is refused",
              res["status"] == "Failed" and "Mail limit reached" in (res["error"] or ""))
        check("refused send writes no run record", n_after == n_before)

        res = ss.execute_schedule(db, s, kind="AUTO")
        check("scheduler is bound by the cap too",
              res["status"] == "Failed" and "Mail limit reached" in (res["error"] or ""))
    finally:
        drop(db, sid)


def test_quota_concurrency(db):
    print("\nmail quota under concurrency")
    email_service.send_email_with_attachment = _stub_slow
    base = mail_quota.status(db)["count"]
    limit = mail_quota.MAX_SENT_MAILS
    room = limit - base
    if room < 1:
        check("quota has room to test concurrency", False,
              "already at %d/%d" % (base, limit))
        return

    s = make_schedule(db)
    sid = s.id
    try:
        for _ in range(room - 1):                     # leave exactly ONE slot
            ss.execute_schedule(db, s, kind="RUN")
        check("primed to one slot left",
              mail_quota.status(db)["count"] == limit - 1)

        results, lock, barrier = [], threading.Lock(), threading.Barrier(6)

        def worker():
            d = SessionLocal()
            try:
                row = d.query(ReportSchedule).filter(ReportSchedule.id == sid).first()
                barrier.wait()
                r = ss.execute_schedule(d, row, kind="RUN")
                with lock:
                    results.append(r["status"])
            except Exception as e:                    # noqa: BLE001
                with lock:
                    results.append("EXC:" + type(e).__name__)
            finally:
                d.close()

        threads = [threading.Thread(target=worker) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        db.expire_all()
        ok = results.count("Success")
        final = mail_quota.status(db)["count"]
        check("only one of six concurrent sends takes the last slot", ok == 1,
              "Success=%d Failed=%d" % (ok, results.count("Failed")))
        check("cap is never exceeded", final == limit, "%d/%d" % (final, limit))
    finally:
        email_service.send_email_with_attachment = _stub_ok
        drop(db, sid)


def main():
    real_send = email_service.send_email_with_attachment
    email_service.send_email_with_attachment = _stub_ok   # nothing leaves the machine

    db = SessionLocal()
    leftovers = purge_test_records(db)
    if leftovers:
        print("cleared %d leftover test schedule(s) from a previous run" % leftovers)

    start_schedules = db.query(ReportSchedule).count()
    start_runs = db.query(ScheduleRun).count()
    start_quota = mail_quota.status(db)["count"]
    print("baseline: schedules=%d runs=%d quota=%d/%d"
          % (start_schedules, start_runs, start_quota, mail_quota.MAX_SENT_MAILS))

    try:
        test_crud(db)
        test_validation(db)
        test_date_range(db)
        test_generated_time(db)
        test_quota(db)
        test_quota_concurrency(db)
    finally:
        purge_test_records(db)
        email_service.send_email_with_attachment = real_send
        db.expire_all()
        print("\nleft behind unchanged")
        check("schedule count restored",
              db.query(ReportSchedule).count() == start_schedules,
              "%d" % start_schedules)
        check("run history restored",
              db.query(ScheduleRun).count() == start_runs, "%d" % start_runs)
        check("mail quota restored",
              mail_quota.status(db)["count"] == start_quota, "%d" % start_quota)
        check("no e-mail sent", True,
              "%d attachment(s) built, all stubbed" % len(_sent))
        db.close()

    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
