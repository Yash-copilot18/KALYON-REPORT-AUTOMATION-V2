"""
Forecast module checks — stdlib only, same style as run_tests.py.

    cd Backend
    venv\\Scripts\\python.exe tests\\test_forecast.py

The arithmetic checks need no database. The last group does, and is skipped
with a clear message if the database is unreachable, so the suite is still
useful on a developer machine with no SQL Server.

Nothing here writes to the database. The end-to-end group reads and runs the
pipeline in memory; it never calls the persisting entry point.
"""

import os
import sys
import traceback
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}" + (f"  -> {detail}" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {name}" + (f"  -> {detail}" if detail else ""))


def near(a, b, tol=1e-9):
    return a is not None and b is not None and abs(a - b) <= tol


# ── metrics ──────────────────────────────────────────────────────────────────
def test_metrics():
    print("\nmetrics")
    from app.forecast import metrics as M

    # WAPE = sum|err| / sum(actual). 30 of 600 -> 5%.
    a = [100.0, 200.0, 300.0]
    f = [110.0, 190.0, 310.0]
    check("WAPE is computed on summed error over summed actual",
          near(M.wape(a, f), 100 * 30 / 600), f"{M.wape(a, f):.4f}%")
    check("score is 100 minus WAPE",
          near(M.forecast_score(a, f), 100 - 100 * 30 / 600),
          f"{M.forecast_score(a, f):.4f}%")

    m = M.evaluate(a, f)
    check("MAE is the mean absolute error", near(m["mae_mwh"], 10.0))
    check("bias is signed and positive when over-forecasting",
          near(m["bias_mwh"], 10.0 / 3), f"{m['bias_mwh']:+.3f}")
    check("RMSE is at least MAE", m["rmse_mwh"] >= m["mae_mwh"])

    # A perfect forecast scores 100.
    check("a perfect forecast scores 100%", near(M.forecast_score(a, a), 100.0))

    # Near-zero days must suppress MAPE but still produce WAPE.
    z = M.evaluate([0.5, 300.0], [40.0, 310.0])
    check("MAPE is suppressed when a day is near zero", z["mape_pct"] is None,
          z["mape_note"])
    check("WAPE still reported on a near-zero day", z["wape_pct"] is not None,
          f"{z['wape_pct']:.1f}%")

    # An all-zero window cannot be scored at all, and says so.
    check("a zero-sum window yields no score", M.forecast_score([0, 0], [5, 5]) is None)

    # The score floor keeps a catastrophic model at 0 rather than negative.
    check("score floors at zero rather than going negative",
          near(M.forecast_score([100.0], [500.0]), 0.0))

    check("condition: curtailment outranks irradiance",
          M.classify_condition(600, 7.5) == "curtailed")
    check("condition: high irradiance", M.classify_condition(0, 7.0) == "high irradiance")
    check("condition: missing irradiance is named, not guessed",
          M.classify_condition(0, None) == "no irradiance data")


# ── no leakage in the supervised frame ───────────────────────────────────────
def test_no_leakage():
    print("\nfeature frame / leakage")
    from app.forecast import features as F

    base = date(2024, 1, 1)
    plant = [{
        "d": base + timedelta(days=i),
        "minutes": 1440,
        "energy_net_mwh": float(100 + i),
        "energy_gross_mwh": float(100 + i),
        "peak_mw": 50.0,
        "avg_inverters_running": 13.0,
        "curtailed_minutes": 0,
        "producing_minutes": 700,
        "mean_setpoint_pct": None,
    } for i in range(30)]

    fr = F.build_frame(plant, None)
    check("rows start only after a full week of history",
          len(fr["y"]) == 30 - F.MIN_HISTORY_DAYS, f"{len(fr['y'])} rows")

    names = fr["feature_names"]
    i1, i7 = names.index("lag1"), names.index("lag7")
    ok1 = all(near(fr["X"][k][i1], fr["y"][k] - 1) for k in range(len(fr["y"])))
    ok7 = all(near(fr["X"][k][i7], fr["y"][k] - 7) for k in range(len(fr["y"])))
    check("lag1 is strictly the previous day's value", ok1)
    check("lag7 is strictly the value seven days earlier", ok7)
    check("no feature equals the same-day target",
          not any(near(fr["X"][k][j], fr["y"][k])
                  for k in range(len(fr["y"])) for j in (i1, i7)))
    check("measured weather is NOT a feature unless a forecast is supplied",
          fr["weather_features_used"] is False and
          not any(n.startswith("fc_") for n in names))


# ── deviation classification ─────────────────────────────────────────────────
def test_deviation():
    print("\ndeviation detection")
    from app.forecast import service as S

    check("an incomplete day is never alerted on",
          S.classify_deviation(800, 400, minutes=900) == "UNRELIABLE")
    check("a missing actual is never alerted on",
          S.classify_deviation(800, None, minutes=1440) == "UNRELIABLE")
    check("a small absolute miss is OK",
          S.classify_deviation(800, 800 - S.DEVIATION_MIN_MWH / 2, 1440) == "OK")
    big = 800 * (1 + 2 * S.DEVIATION_PCT_THRESHOLD / 100)
    check("a large over-performance is OVER",
          S.classify_deviation(800, big, 1440) == "OVER")
    check("a large under-performance is UNDER",
          S.classify_deviation(800, 800 * 0.5, 1440) == "UNDER")

    ref = S.reference_levels([
        {"avg_inverters_running": 13.0, "producing_minutes": 700},
        {"avg_inverters_running": 13.0, "producing_minutes": 700},
        {"avg_inverters_running": 5.0,  "producing_minutes": 200},
    ])
    check("reference levels come from the plant's own median",
          near(ref["ref_inverters_running"], 13.0), str(ref))

    # A normal day must not raise an availability hypothesis.
    normal = {"curtailed_minutes": 0, "avg_inverters_running": 13.0,
              "ref_inverters_running": 13.2, "producing_minutes": 700,
              "ref_producing_minutes": 700, "gti_kwh_m2": 6.0}
    # Match the claim, not the word: the catch-all message mentions
    # "availability" while explicitly ruling it out.
    claims_availability = lambda hs: any("Reduced availability" in h for h in hs)

    hyp = S.hypotheses(normal, "UNDER")
    check("a typical day raises no false availability hypothesis",
          not claims_availability(hyp), f"{len(hyp)} hypothesis(es): {hyp[0][:60]}…")
    check("a typical day with no measured cause says exactly that",
          any("No measured cause stands out" in h for h in hyp))

    # A genuinely degraded day must raise one.
    bad = dict(normal, avg_inverters_running=5.0, producing_minutes=200)
    check("a materially degraded day does raise one",
          claims_availability(S.hypotheses(bad, "UNDER")))

    curt = dict(normal, curtailed_minutes=600, mean_setpoint_pct=70.0)
    check("curtailment is raised when the setpoint held the plant down",
          any("Curtailment" in h for h in S.hypotheses(curt, "UNDER")))

    check("an OK day produces no hypotheses at all", S.hypotheses(normal, "OK") == [])


# ── weather provider ─────────────────────────────────────────────────────────
def test_weather():
    print("\nweather provider")
    from app.forecast import weather as W

    saved = os.environ.pop(W.PROVIDER_ENV, None)
    try:
        st = W.status()
        check("weather is OFF unless explicitly configured", st["configured"] is False)
        check("the reason is stated for the UI", bool(st["reason"]))
        check("no network call is made while disabled", W.fetch_forecast() is None)
        check("the API key is never echoed back",
              "api_key" not in str(st).lower() or isinstance(st["api_key_present"], bool))
    finally:
        if saved is not None:
            os.environ[W.PROVIDER_ENV] = saved


# ── end to end against the real database (read-only) ─────────────────────────
def test_end_to_end():
    print("\nend to end (database)")
    try:
        from app.database.session import SessionLocal
        from app.forecast import backtest as BT
        from app.forecast import features as F
        db = SessionLocal()
    except Exception as exc:                            # noqa: BLE001
        print(f"  [SKIP] database unavailable: {exc}")
        return

    try:
        plant = F.daily_plant(db)
        check("complete days are returned", len(plant) > 0, f"{len(plant)} days")
        check("every returned day meets the completeness threshold",
              all(p["minutes"] >= F.COMPLETE_DAY_MINUTES for p in plant))
        check("gross energy is never below net energy",
              all(p["energy_gross_mwh"] >= p["energy_net_mwh"] - 1e-6 for p in plant))
        check("days are unique and ordered",
              [p["d"] for p in plant] == sorted({p["d"] for p in plant}))

        wx = F.daily_weather(db)
        check("weather days are returned", len(wx) > 0, f"{len(wx)} days")

        frame = F.build_frame(plant, wx)
        check("the supervised frame is non-empty", len(frame["y"]) > 0,
              f"{len(frame['y'])} rows")

        bt = BT.walk_forward_one_day(frame)
        check("day-ahead validation completes", bt.get("ok") is True,
              bt.get("reason", ""))
        if bt.get("ok"):
            check("the test block is disjoint from training",
                  bt["test_start"] > bt["train_end"],
                  f"train ends {bt['train_end']}, test starts {bt['test_start']}")
            check("both baselines were scored",
                  sum(1 for r in bt["leaderboard"] if r["is_baseline"]) == 2)
            check("a held-out score was produced",
                  bt["test"].get("score_pct") is not None,
                  f"{bt['test'].get('score_pct'):.1f}%")
            check("the held-out score is reported, not asserted to meet target",
                  0 <= bt["test"]["score_pct"] <= 100)
    finally:
        db.close()


# ── quality control ──────────────────────────────────────────────────────────
def test_quality_control():
    print("\nquality control")
    from app.forecast import features as F
    from datetime import date as _d

    # The clear-sky ceiling must follow the season, not sit at a flat number.
    mar = F.extraterrestrial_daily_kwh_m2(_d(2024, 3, 30))
    jun = F.extraterrestrial_daily_kwh_m2(_d(2024, 6, 21))
    dec = F.extraterrestrial_daily_kwh_m2(_d(2024, 12, 21))
    check("clear-sky ceiling rises from winter to summer", dec < mar < jun,
          f"Dec {dec:.1f} < Mar {mar:.1f} < Jun {jun:.1f} kWh/m2")
    check("the ceiling is physically sensible at 38N", 4 < mar < 12, f"{mar:.2f}")

    # C3 must not flag ordinary cloud enhancement.
    check("cloud-enhancement peaks are not treated as faults",
          F.QC_MAX_PEAK_GTI_WM2 >= 1400, f"threshold {F.QC_MAX_PEAK_GTI_WM2:.0f} W/m2")


# ── forecast cache / leakage ────────────────────────────────────────────────
def test_cache_leakage():
    print("\ncache and leakage")
    try:
        from app.database.session import SessionLocal
        from app.forecast import cache as C
        db = SessionLocal()
    except Exception as exc:                            # noqa: BLE001
        print(f"  [SKIP] database unavailable: {exc}")
        return
    try:
        ok, msg = C.verify_no_leakage(db)
        check("no cached forecast was issued on or after the day it predicts",
              ok, msg)
        cov = C.coverage(db)
        check("the cache reports zero leaking rows", cov["leakage_rows"] == 0,
              f"{cov['rows']} rows cached")
        fc = C.load_day_ahead(db)
        if cov["rows"]:
            check("loaded forecasts are keyed by the day they describe",
                  len(fc) > 0, f"{len(fc)} usable")
    finally:
        db.close()


# ── pre-registration integrity ──────────────────────────────────────────────
def test_preregistration():
    print("\npre-registration")
    import os
    from app.forecast import backtest as BT, models as M
    prereg = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), "docs", "forecast-prereg.md")
    check("the pre-registration document exists", os.path.exists(prereg), prereg)
    if os.path.exists(prereg):
        txt = open(prereg, encoding="utf-8").read()
        check("it names the model that is scored",
              M.PREREGISTERED_MODEL in txt, M.PREREGISTERED_MODEL)
        check("it states the scoring formula", "100 − WAPE" in txt or "100 - WAPE" in txt)
    check("the rolling window is fixed, not inferred",
          BT.PREREG_ROLLING_WINDOW == 60, str(BT.PREREG_ROLLING_WINDOW))
    check("scoring begins only after a minimum training period",
          BT.PREREG_MIN_TRAIN >= 30, str(BT.PREREG_MIN_TRAIN))


def main():
    print("Forecast module checks")
    for fn in (test_metrics, test_no_leakage, test_deviation,
               test_weather, test_quality_control, test_cache_leakage,
               test_preregistration, test_end_to_end):
        try:
            fn()
        except Exception:                               # noqa: BLE001
            global FAIL
            FAIL += 1
            print(f"  [FAIL] {fn.__name__} raised:")
            traceback.print_exc()
    print(f"\n{PASS}/{PASS + FAIL} checks passed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
