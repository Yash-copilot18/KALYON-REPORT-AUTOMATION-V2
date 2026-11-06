"""
Device-level forecasting and combiner peer analysis.

Two jobs, deliberately different in kind:

**Inverters get a forecast.** Each one is run through the same backtest the
plant uses, on its own history alone, with its own winner chosen from the same
five candidates. The plant's winning model is NOT inherited: a model that suits
the summed series need not suit a single inverter, and assuming otherwise would
be picking a model without comparing it.

**Combiners get an analysis, not a forecast.** 34 of the 170 report essentially
nothing and the rest carry 34 complete days - too few to forecast honestly, and
in any case the operationally useful question about a combiner is not "how much
tomorrow" but "is this one keeping up with its neighbours". So they are ranked
against their own group, and what the comparison shows is stated as evidence
with the cause left open.

Results live in an in-process cache rather than a table, because adding tables
was out of scope. They are lost on restart and recomputed on demand.
"""

from __future__ import annotations

import logging
import os
import statistics
import threading
from concurrent.futures import as_completed
from datetime import datetime
from typing import Any, Dict, Iterator, List, Optional, Tuple

from sqlalchemy.orm import Session

from app.forecast import backtest as BT
from app.forecast import devices as D
from app.forecast import pool as POOL
from app.forecast import features as F
from app.forecast import models as M
from app.forecast import service as S

logger = logging.getLogger(__name__)

_lock = threading.Lock()
# device id -> result dict. Survives requests, not restarts.
_forecast_cache: Dict[str, Any] = {"generated_at": None, "devices": [],
                                   "running": False, "total": 0}


# ── per-inverter forecasting ─────────────────────────────────────────────────
def _forecast_one(plant_rows: List[Dict[str, Any]], device: Dict[str, Any]
                  ) -> Dict[str, Any]:
    """
    Backtest one device on its own series.

    Returns a row whether it succeeds or not: a device that cannot be evaluated
    is reported as such, with the reason, rather than dropped from the table.
    """
    base = {
        "device": device["id"],
        "type": device["type"],
        "group": device["group"],
        "complete_days": len(plant_rows),
        "latest_actual_mwh": round(plant_rows[-1]["energy_net_mwh"], 2) if plant_rows else None,
        "latest_actual_date": plant_rows[-1]["d"] if plant_rows else None,
        "mean_actual_mwh": round(
            statistics.fmean(r["energy_net_mwh"] for r in plant_rows), 2
        ) if plant_rows else None,
    }

    if len(plant_rows) < D.MIN_DEVICE_DAYS:
        return {**base, "status": "INSUFFICIENT DATA", "validation": "INSUFFICIENT DATA",
                "reason": (f"{len(plant_rows)} complete days; at least "
                           f"{D.MIN_DEVICE_DAYS} are needed before a backtest can "
                           f"train and still hold days back."),
                "predicted_mwh": None, "forecast_date": None,
                "score_pct": None, "wape_pct": None, "mae_mwh": None,
                "rmse_mwh": None, "bias_mwh": None, "r2": None, "n_test": 0,
                "model": None, "leaderboard": []}

    frame = F.build_frame(plant_rows, None, "energy_net_mwh", weather_forecast=None)
    if len(frame["y"]) < 10:
        return {**base, "status": "INSUFFICIENT DATA", "validation": "INSUFFICIENT DATA",
                "reason": f"only {len(frame['y'])} supervised rows after lag warm-up.",
                "predicted_mwh": None, "forecast_date": None,
                "score_pct": None, "wape_pct": None, "mae_mwh": None,
                "rmse_mwh": None, "bias_mwh": None, "r2": None, "n_test": 0,
                "model": None, "leaderboard": []}

    bt = BT.walk_forward_one_day(frame)
    if not bt.get("ok"):
        return {**base, "status": "INSUFFICIENT DATA", "validation": "INSUFFICIENT DATA",
                "reason": bt.get("reason", "backtest could not run."),
                "predicted_mwh": None, "forecast_date": None,
                "score_pct": None, "wape_pct": None, "mae_mwh": None,
                "rmse_mwh": None, "bias_mwh": None, "r2": None, "n_test": 0,
                "model": None, "leaderboard": []}

    test = bt["test"]
    n_test = int(test.get("n") or 0)

    interval = BT.conformal_interval(bt["test_records"])
    half = (interval or {}).get("half_width_mwh")

    # The forward series comes from the PLANT's own recursion, called directly
    # rather than reimplemented here. One implementation means a device horizon
    # and the plant horizon cannot drift apart, and it inherits the same
    # property that matters: each step feeds its own output back as the next
    # lag, so nothing from the future enters at any horizon.
    forward = S._predict_forward(frame, bt, plant_rows, interval)
    nxt = forward[0]["predicted_mwh"] if forward else 0.0

    # Sample size decides the badge, never the score. A high number on a
    # handful of days is not a measurement.
    enough = n_test >= BT.MIN_CREDIBLE_TEST_DAYS
    validation = "MEASURED" if enough else "PROVISIONAL"

    from datetime import timedelta
    return {
        **base,
        "status": "OK",
        "validation": validation,
        "reason": (f"Scored on {n_test} held-out day{'' if n_test == 1 else 's'}"
                   + ("." if enough else
                      f", below the {BT.MIN_CREDIBLE_TEST_DAYS} needed for the "
                      f"figure to be stable. Provisional.")),
        "predicted_mwh": round(nxt, 2),
        "forecast_date": plant_rows[-1]["d"] + timedelta(days=1),
        "lower_mwh": round(max(0.0, nxt - half), 2) if half else None,
        "upper_mwh": round(nxt + half, 2) if half else None,
        "model": bt["selected_model"],
        "score_pct": round(test["score_pct"], 1) if test.get("score_pct") is not None else None,
        "wape_pct": round(test["wape_pct"], 1) if test.get("wape_pct") is not None else None,
        "mae_mwh": round(test["mae_mwh"], 2) if test.get("mae_mwh") is not None else None,
        "rmse_mwh": round(test["rmse_mwh"], 2) if test.get("rmse_mwh") is not None else None,
        "bias_mwh": round(test["bias_mwh"], 2) if test.get("bias_mwh") is not None else None,
        "r2": round(test["r2"], 2) if test.get("r2") is not None else None,
        "n_test": n_test,
        "test_start": bt["test_start"], "test_end": bt["test_end"],
        "leaderboard": bt["leaderboard"],
        "horizons": _horizons(forward, n_test),
        "daily": [{"date": p["date"], "horizon": p["horizon"],
                   "predicted_mwh": round(p["predicted_mwh"], 2),
                   "lower_mwh": round(p["lower_mwh"], 2) if p["lower_mwh"] else None,
                   "upper_mwh": round(p["upper_mwh"], 2) if p["upper_mwh"] else None}
                  for p in forward],
        "series": _series(bt, forward, half),
    }


def _series(bt: Dict[str, Any], forward: List[Dict[str, Any]],
            half: Optional[float]) -> List[Dict[str, Any]]:
    """
    The device's own Actual vs Predicted series, in the shape the plant chart
    already uses.

    Two halves, exactly as at plant level: the held-out block, where a
    prediction sits beside the day it was scored against, and the forward
    block, where no actual exists or ever will. Keeping the same keys means
    `aggregate.py` rolls a device up into weeks and months with no special
    case, and the device chart becomes the plant chart with different data.

    It is held in memory rather than persisted: 24 devices of roughly 130
    points each is nothing, and it needs no new table.
    """
    rows: List[Dict[str, Any]] = []
    for r in (bt.get("test_records") or []):
        pred = float(r["forecast"])
        rows.append({
            "date": r["d"],
            "predicted_mwh": round(pred, 2),
            "actual_mwh": round(float(r["actual"]), 2),
            "lower_mwh": round(max(0.0, pred - half), 2) if half else None,
            "upper_mwh": round(pred + half, 2) if half else None,
            "is_backtest": True,
        })
    for p in forward:
        rows.append({
            "date": p["date"],
            "predicted_mwh": round(p["predicted_mwh"], 2),
            "actual_mwh": None,
            "lower_mwh": round(p["lower_mwh"], 2) if p["lower_mwh"] else None,
            "upper_mwh": round(p["upper_mwh"], 2) if p["upper_mwh"] else None,
            "is_backtest": False,
        })
    rows.sort(key=lambda r: r["date"])
    return rows


# Horizon blocks exposed per device. Day 1 is the product; the rest are
# planning volumes, and the labels below say which is which.
HORIZON_BLOCKS = {"next_day": 1, "next_7_days": 7, "next_30_days": 30}


def _horizons(forward: List[Dict[str, Any]], n_test: int) -> Dict[str, Any]:
    """
    Cumulative totals per horizon, each carrying what the backtest knows.

    Only horizon 1 is measured. Beyond it the model consumes its own output
    with no new information entering, so the series decays toward the device's
    recent average - useful as a planning volume, not a forecast, and labelled
    that way rather than left for the reader to infer.
    """
    out: Dict[str, Any] = {}
    for label, days in HORIZON_BLOCKS.items():
        take = forward[:days]
        have = len(take)
        total = sum(p["predicted_mwh"] for p in take)
        lo = [p["lower_mwh"] for p in take]
        hi = [p["upper_mwh"] for p in take]
        measured = days == 1 and n_test >= BT.MIN_CREDIBLE_TEST_DAYS
        out[label] = {
            "days_requested": days,
            "days_available": have,
            "complete": have >= days,
            "total_mwh": round(total, 2) if have else None,
            "mean_daily_mwh": round(total / have, 2) if have else None,
            # Summing the daily bands assumes the errors move together. They do
            # not, so this band is wider than the truth - erring toward
            # overstating uncertainty rather than understating it.
            "lower_mwh": round(sum(x for x in lo if x is not None), 2)
                         if have and all(x is not None for x in lo) else None,
            "upper_mwh": round(sum(x for x in hi if x is not None), 2)
                         if have and all(x is not None for x in hi) else None,
            "start": take[0]["date"] if have else None,
            "end": take[-1]["date"] if have else None,
            "validated": measured,
            "label": ("MEASURED" if measured
                      else "PROVISIONAL" if days == 1 else "PLANNING ESTIMATE"),
            "basis": (
                f"Measured by the day-ahead backtest on {n_test} held-out "
                f"day{'' if n_test == 1 else 's'}."
                + ("" if measured else " Below the "
                   f"{BT.MIN_CREDIBLE_TEST_DAYS} needed to be stable.")
                if days == 1 else
                f"Not evaluated at this range. Each day after the first is "
                f"predicted from the previous day's prediction with no new "
                f"information entering, so the series settles toward this "
                f"device's recent average. Use as an expected volume."),
        }
    return out


def run_device_forecasts(db: Session) -> Dict[str, Any]:
    """
    Forecast every discovered inverter.

    The SQL is cheap (about a fifth of a second per device) and the model
    fitting is not: each device fits all five candidates across every fold, so
    even a device that ends up choosing Persistence pays for the forest and the
    booster first. Those fits share nothing - no session, no state, one device's
    history each - so they are run in parallel across processes.

    The arithmetic is untouched. Each device sees exactly the data it saw
    before, the models carry fixed seeds, and the rows are re-ordered back into
    discovery order before they are published, so the output is identical to the
    serial run; only the waiting is shorter.
    """
    with _lock:
        if _forecast_cache.get("running"):
            return {"ok": False, "reason": "A device run is already in progress."}
        _forecast_cache["running"] = True
    try:
        inv = D.discover(db)["inverters"]
        fresh = D.last_seen(db, [d["table"] for d in inv])
        with _lock:
            # Clear previous results at the start. A run takes a while, and
            # leaving the old rows in place while it works makes the progress
            # count read as "11 of 12 done" from the first second.
            _forecast_cache["devices"] = []
            _forecast_cache["total"] = len(inv)

        # Read every device's history on this thread - a worker process has no
        # session, so the database work stays here either way.
        #
        # Fetching is interleaved with submitting rather than done up front.
        # Reading all 24 series takes about 4 seconds, and doing that first
        # left every worker idle throughout; handing each device over the
        # moment its rows arrive means the first fits are already running
        # while the last queries are still coming back.
        work: List[Tuple[Dict[str, Any], List[Dict[str, Any]]]] = []

        def _fetch() -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
            for device in inv:
                rows = D.daily_inverter(db, device["table"])
                work.append((device, rows))
                yield device, rows

        done: Dict[str, Dict[str, Any]] = {}

        def _publish(device: Dict[str, Any], row: Dict[str, Any]) -> None:
            """Record one finished device and republish in discovery order."""
            row["last_seen"] = fresh.get(device["table"])
            done[device["id"]] = row
            ordered = [done[d["id"]] for d, _ in work if d["id"] in done]
            with _lock:
                _forecast_cache["devices"] = ordered
            logger.info("device forecast %s: %s (%s complete days)",
                        device["id"], row["status"], row["complete_days"])

        # The shared pool, already warm: its workers were spawned at startup,
        # so this run pays no interpreter-start cost at all. Submitting rather
        # than mapping keeps the progress count honest - each device is
        # published the moment it finishes, in whatever order they land.
        pool = POOL.get()
        ran_parallel = False
        if pool is not None:
            try:
                futures = {pool.submit(_forecast_one, rows, device): device
                           for device, rows in _fetch()}
                for future in as_completed(futures):
                    device = futures[future]
                    _publish(device, future.result())
                ran_parallel = True
                logger.info("device forecasts fitted on the shared pool "
                            "(%d workers)", POOL.size())
            except Exception as exc:  # noqa: BLE001
                # A machine that cannot spawn processes - a locked-down host, a
                # frozen build - must still produce the forecast, just slower.
                # Anything already published is discarded so the serial pass is
                # the only thing that can reach the cache.
                logger.warning("Parallel device run unavailable (%s). "
                               "Falling back to one device at a time.", exc)
                POOL.reset()
                done.clear()

        if not ran_parallel:
            # Either the pool is off or it failed. `work` is already filled if
            # the parallel attempt got as far as fetching; otherwise fetch now.
            for device, rows in (work or _fetch()):
                _publish(device, _forecast_one(rows, device))

        out = [done[device["id"]] for device, _ in work]
        result = {"ok": True, "generated_at": datetime.now(),
                  "devices": out, "n_devices": len(out)}
        with _lock:
            _forecast_cache.update({"generated_at": result["generated_at"],
                                    "devices": out, "running": False,
                                    "total": len(inv)})
        return result
    except Exception:
        with _lock:
            _forecast_cache["running"] = False
        raise


def cached_forecasts() -> Dict[str, Any]:
    with _lock:
        devices = list(_forecast_cache["devices"])
        return {"generated_at": _forecast_cache["generated_at"],
                "devices": devices,
                "n_devices": len(devices),
                # How many the run will produce in total, so a caller polling
                # mid-run can show "4 of 12" rather than a bare count.
                "total": int(_forecast_cache.get("total") or len(devices)),
                "running": bool(_forecast_cache.get("running"))}




# ── combiner peer analysis ───────────────────────────────────────────────────
def combiner_analysis(db: Session) -> Dict[str, Any]:
    """
    Rank every string combiner box against the others behind its own inverter.

    Kalyon carries 504 boxes across 24 `INVERTERn_SMB` tables, 21 or so per
    table held as columns. Comparison is within an inverter rather than across
    the whole plant, because the 24 inverters sit on different parts of the
    site and a plant-wide ranking would read orientation and layout as if they
    were faults.

    These are ranked, not forecast. A box has 61 complete days and no energy
    register, which is not enough to forecast honestly, and the operationally
    useful question about a combiner is not "how much tomorrow" but "is this
    one keeping up with its neighbours".

    Every flag is evidence, not a diagnosis. A box reading nothing may be
    disconnected, may never have been instrumented, or may have a dead sensor
    on strings that are working fine. This says which boxes differ from their
    peers and by how much, and leaves the cause to someone who can walk the
    site.
    """
    boxes = D.discover(db)["combiners"]
    if not boxes:
        return {"devices": [], "groups": {}, "n_devices": 0,
                "note": "No INVERTERn_SMB tables found in this database."}

    measured = D.daily_combiners(db, boxes)
    fresh = D.last_seen(db, sorted({b["table"] for b in boxes}))

    by_group: Dict[str, List[Dict[str, Any]]] = {}
    for b in boxes:
        by_group.setdefault(b["group"], []).append(b)

    rows: List[Dict[str, Any]] = []
    group_stats: Dict[str, Any] = {}

    for group, members in sorted(by_group.items()):
        means = {m["id"]: (measured.get(m["id"]) or {}).get("mean_kwh")
                 for m in members}
        producing = [v for v in means.values()
                     if v is not None and v >= D.SMB_ZERO_KWH]
        median = statistics.median(producing) if producing else None

        group_stats[group] = {
            "devices": len(members),
            "producing": len(producing),
            "median_kwh": round(median, 1) if median else None,
            "group_total_mwh": round(sum(producing) / 1000.0, 2) if producing else None,
        }

        # Only producing boxes are ranked; a box reading nothing has no
        # meaningful position in an ordering of output.
        ranked = sorted([m["id"] for m in members
                         if means[m["id"]] is not None
                         and means[m["id"]] >= D.SMB_ZERO_KWH],
                        key=lambda i: means[i], reverse=True)
        rank_of = {i: n + 1 for n, i in enumerate(ranked)}

        for m in members:
            info = measured.get(m["id"]) or {}
            mean = means[m["id"]]
            days = int(info.get("complete_days") or 0)
            status, note = _combiner_status(mean, days, median)
            rows.append({
                "device": m["id"],
                "type": "combiner",
                "group": group,
                "inverter": m["inverter"],
                "source": "%s.%s" % (m["table"], m["column"]),
                "complete_days": days,
                "mean_kwh": round(mean, 1) if mean is not None else None,
                "group_median_kwh": round(median, 1) if median else None,
                "deviation_pct": (round(100.0 * (mean - median) / median, 1)
                                  if (mean is not None and median) else None),
                "rank_in_group": rank_of.get(m["id"]),
                "group_size": len(members),
                "status": status,
                "evidence": note,
                "last_seen": fresh.get(m["table"]),
            })

    flagged = [r for r in rows if r["status"] in ("ZERO OUTPUT", "BELOW PEERS")]
    return {
        "devices": rows,
        "groups": group_stats,
        "n_devices": len(rows),
        "n_flagged": len(flagged),
        "note": (
            "Combiner boxes are ranked against the other boxes behind the same "
            "inverter, not forecast: they carry no energy register and too few "
            "complete days to forecast honestly. Every status is a measurement "
            "with the cause left open."),
    }



def _combiner_status(mean: Optional[float], days: int,
                     median: Optional[float]) -> tuple:
    """Status and the evidence behind it. Never asserts a hardware failure."""
    if days == 0:
        return "NO DATA", "No complete day of readings."
    if days < 20:
        return "INSUFFICIENT DATA", f"Only {days} complete days of history."
    if mean is None or mean < D.SMB_ZERO_KWH:
        measured = (f"Averages {mean:.1f} kWh on a complete day against a group "
                    f"median of {median:.0f}."
                    if (mean is not None and median) else
                    "Reports effectively nothing on a complete day.")
        return "ZERO OUTPUT", (
            measured + " The reading is the measurement; the cause is not. It "
            "could be disconnected, never instrumented, or a failed sensor on "
            "strings that are working. Worth a site check.")
    if median and mean < 0.75 * median:
        return "BELOW PEERS", (
            f"Averages {mean:.0f} kWh against a group median of {median:.0f}, "
            f"{100.0 * (median - mean) / median:.0f}% lower. Worth inspecting.")
    return "OK", (f"Within range of its group median of {median:.0f} kWh."
                  if median else "Producing.")
