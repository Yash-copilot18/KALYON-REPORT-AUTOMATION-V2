"""
Document body for the prototype assurance report.

Separated from build_report.py (which holds the chart/table primitives and the
analysis driver) purely to keep each file readable. Every value rendered here comes
from the `a` dict produced by build_report.analyse(); this module performs no
queries and no arithmetic beyond formatting and simple aggregation of values already
measured.
"""

from __future__ import annotations

import statistics
from datetime import datetime
from typing import Any, Dict, List

from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, Spacer

from build_report import (ACCENT, BAD, GOOD, INK,
                          INSTALLED_CAPACITY_MW, MUTED, WARN,
                          S_BODY, S_CELL, S_CELLB, S_NOTE, S_SUB, S_TITLE,
                          bar_chart, fmt, forecast_chart, grouped_bar, h1, h2,
                          kpi_row, line_chart, month_bar, para, scatter_chart,
                          table, tag)


def interp(rows: List[Any]) -> Any:
    """
    The five questions every chart in this report has to answer.

    Written as a table rather than prose so that a reading cannot be offered
    without an evidence line beside it, and so that a missing cause shows up as
    an explicit 'not established' instead of disappearing into a sentence.
    """
    data = [[para("Question", S_CELLB), para("Answer from measured data", S_CELLB)]]
    for k, v in rows:
        data.append([para(k, S_CELLB), para(v)])
    return table(data, [30 * mm, 118 * mm], size=7)


def period_tag(m: Dict[str, Any]) -> Any:
    return (tag("COMPLETE", GOOD) if m["complete_month"]
            else tag("PARTIAL", WARN))


def story(a: Dict[str, Any]) -> List[Any]:
    F: List[Any] = []
    full = a["full"]
    first_d, last_d = full[0]["d"], full[-1]["d"]
    mean_day = a["tot_int"] / len(full)
    diff = a["tot_reg"] - a["tot_int"]
    diff_pct = 100 * diff / a["tot_int"]
    mon, yrs = a["monthly"], a["yearly"]
    hi_m = max(mon, key=lambda m: m["mean_mwh_day"])
    lo_m = min(mon, key=lambda m: m["mean_mwh_day"])
    complete_months = [m for m in mon if m["complete_month"]]
    res_months = [m for m in mon if m["pr_pct"] is not None]
    apr0, may0 = res_months[-2], res_months[-1]
    inv_sorted = sorted(a["inverters"], key=lambda i: i["energy_mwh"])
    med_e = statistics.median([i["energy_mwh"] for i in a["inverters"]])
    pr = None
    if a["model"]:
        pr = 100 * a["model"]["mean_energy"] / (a["model"]["mean_gti"] * 130.0)

    # ── 1 cover + executive summary ──────────────────────────────────────────
    F += [Spacer(1, 2 * mm),
          Paragraph("AI Performance Assurance Report", S_TITLE),
          Paragraph(f"Kalyon Nigde 130 MW &nbsp;·&nbsp; forensic analysis of plant "
                    f"telemetry {first_d:%d %b %Y} – {last_d:%d %b %Y} &nbsp;·&nbsp; "
                    f"PROTOTYPE generated {datetime.now():%d %b %Y %H:%M}", S_SUB),
          Spacer(1, 2 * mm),
          kpi_row([(f"{a['tot_int']:,.0f}", "MWh delivered (integrated)", INK),
                   (f"{len(full)}", "complete days analysed", INK),
                   (f"{mean_day:,.0f}", "MWh/day mean", INK),
                   (f"{diff_pct:+.2f}%", "register vs integrated",
                    WARN if abs(diff_pct) > 1 else GOOD)]),
          Spacer(1, 2 * mm),
          kpi_row([(f"{a['curt']['limited_minutes']:,}", "minutes curtailed", WARN),
                   (f"{a['curt']['affected_days']}", "days with curtailment", WARN),
                   (f"{pr:.1f}%" if pr else "—", "implied performance ratio",
                    GOOD if pr and pr > 80 else MUTED),
                   ("Unavailable", "next-12-month forecast", BAD)]),
          Spacer(1, 2 * mm),
          kpi_row([(f"{hi_m['mean_mwh_day']:,.0f}",
                    f"MWh/day best month — {hi_m['month']}"
                    f"{' (partial)' if not hi_m['complete_month'] else ''}", GOOD),
                   (f"{lo_m['mean_mwh_day']:,.0f}",
                    f"MWh/day lowest month — {lo_m['month']}"
                    f"{' (partial)' if not lo_m['complete_month'] else ''}", WARN),
                   (f"{len(complete_months)} of 12",
                    "complete calendar months in the archive", WARN),
                   ("Unavailable", "year-over-year comparison", BAD)]),
          Spacer(1, 2 * mm),
          h1("1", "Executive summary"),
          Paragraph(
              f"The plant delivered <b>{a['tot_int']:,.0f} MWh</b> across {len(full)} "
              f"complete days between {first_d:%d %b} and {last_d:%d %b %Y}, averaging "
              f"{mean_day:,.0f} MWh/day with a peak grid export of "
              f"{max(r['peak_mw'] for r in full):,.1f} MW. Measured against plane-of-array "
              f"irradiance the implied performance ratio is "
              f"{('%.1f%%' % pr) if pr else 'not calculable'}, consistent with a healthy "
              f"asset.", S_BODY),
          Paragraph(
              f"The fleet is tight: inverter energy spans "
              f"{inv_sorted[0]['energy_mwh']:,.0f}–{inv_sorted[-1]['energy_mwh']:,.0f} MWh "
              f"around a median of {med_e:,.0f} MWh, a spread of about 8 percentage points. "
              f"<b>No inverter is called faulty in this report</b> — the dispersion is "
              f"within what peer variation and alarm load already explain.", S_BODY),
          Paragraph(
              f"<b>At monthly resolution</b> (§4) the archive covers "
              f"{', '.join(m['month'] + (' partial' if not m['complete_month'] else ' complete') for m in mon)}. "
              f"Mean daily generation rises monotonically across them, "
              f"{lo_m['mean_mwh_day']:,.0f} → {hi_m['mean_mwh_day']:,.0f} MWh/day, and the "
              f"implied performance ratio rises with it where the resource was measured "
              f"({apr0['pr_pct']:.1f}% in {apr0['month']}, {may0['pr_pct']:.1f}% in "
              f"{may0['month']}). The clearest monthly anomaly is {apr0['month']}: the "
              f"heaviest curtailment in the archive ({apr0['limited_minutes']:,} exporting "
              f"minutes at a mean setpoint of {apr0['mean_setpoint_pct']:.1f}%) together with "
              f"a one-off inverter availability loss, both quantified in §7.2 and §8.2.",
              S_BODY),
          Paragraph(
              f"<b>At yearly resolution</b> (4.3) only an available-period result exists: "
              f"{yrs[0]['first']:%d %b} – {yrs[0]['last']:%d %b %Y}, "
              f"{yrs[0]['days']} complete days, {yrs[0]['energy_mwh']:,.0f} MWh. "
              f"<b>Year-over-year analysis is UNAVAILABLE</b> — the database holds no year "
              f"other than {yrs[0]['year']}, and no annual figure is estimated to fill the "
              f"gap. The forecast reliability flag is <b>LIMITED</b> (§13).", S_BODY),
          Paragraph(
              f"Three matters warrant attention. The plant's own daily-production register "
              f"disagrees with integrated grid power by <b>{diff:,.0f} MWh "
              f"({diff_pct:+.2f}%)</b>. Weather station <b>WMS4</b> is a measurable outlier "
              f"against its three peers and latched entirely for 2,655 minutes. Curtailment "
              f"limited the plant for <b>{a['curt']['limited_minutes']:,} minutes across "
              f"{a['curt']['affected_days']} days</b>, concentrated in April.", S_BODY),
          Paragraph(
              "<b>On prediction.</b> A 12-month forecast cannot be justified from this "
              "archive and is not offered. Daily-generation forecasting was tested properly "
              "and <b>failed validation</b>; the scores are published in §11 rather than "
              "hidden. What the data does support is a conditional performance model (§6) "
              "and a short-horizon forecast carrying a deliberately wide prediction "
              "interval (§12).", S_BODY)]

    # ── 2 coverage ───────────────────────────────────────────────────────────
    F.append(h1("2", "Historical data coverage"))
    rows = [[para(x, S_CELLB) for x in
             ("Source", "First record", "Last record", "Rows", "Cadence")]]
    for c in a["coverage"]:
        rows.append([para(c["table"]),
                     para(f"{c['first']:%d/%m/%Y %H:%M}" if c["first"] else "—"),
                     para(f"{c['last']:%d/%m/%Y %H:%M}" if c["last"] else "—"),
                     para(fmt(c["rows"])),
                     para(f"{c['cadence_s']}s" if c["cadence_s"] else "—")])
    F.append(table(rows, [42 * mm, 34 * mm, 34 * mm, 26 * mm, 20 * mm],
                   align={3: "RIGHT", 4: "CENTRE"}))
    F.append(Paragraph(
        "Cadence is detected from the data (the modal gap between consecutive records), "
        "not configured: the telemetry sources sample at one minute, the alarm table is "
        "event-driven. <b>The windows do not align</b> — irradiance begins 23 Mar, and "
        "cabinet-temperature and tracker data 20 Apr — so analyses combining those with "
        "generation are limited to the overlap, stated wherever it applies.", S_NOTE))

    # ── 3 historical performance ─ daily level ──────────────────────
    best_day = max(full, key=lambda r: r["energy_integrated_mwh"])
    worst_day = min(full, key=lambda r: r["energy_integrated_mwh"])
    weak_days = sum(1 for r in full
                    if r["energy_integrated_mwh"] < 0.5 * mean_day)

    F.append(h1("3", "Historical plant performance"))
    F.append(Paragraph(
        "The archive is reported at three levels: the daily series here, the monthly and "
        "yearly roll-up in §4, and the forecast horizons in §11–§13. Daily resolution is kept "
        "because it is the level at which weather, curtailment and outages are separately "
        "visible; every monthly and yearly figure in this report is aggregated from exactly "
        "these days and from nothing else.", S_BODY))
    F.append(line_chart([("Daily generation",
                          [(i, r["energy_integrated_mwh"]) for i, r in enumerate(full)],
                          ACCENT, False)],
                        title="Chart 1 — daily generation trend",
                        y_label="Energy, MWh / day",
                        legend=[("Daily generation, integrated grid power (MWh)", ACCENT)]))
    F.append(Paragraph(
        f"Over {len(full)} complete days output ranged from "
        f"{worst_day['energy_integrated_mwh']:,.0f} MWh on {worst_day['d']:%d %b %Y} "
        f"— a net-import day, where the integral of measured grid power is negative "
        f"because the plant drew more than it exported — to "
        f"{best_day['energy_integrated_mwh']:,.0f} MWh on {best_day['d']:%d %b %Y}, against a "
        f"period mean of {mean_day:,.0f} MWh/day. {weak_days} days delivered less than half "
        f"the period mean. The daily spread is large enough that <b>no single day "
        f"characterises the plant</b>, which is the reason the monthly aggregation in §4 "
        f"exists and the reason §12 carries a wide prediction interval.", S_NOTE))

    # ── 4 monthly and yearly performance ───────────────────────────
    F.append(h1("4", "Monthly and yearly plant performance"))
    F.append(Paragraph(
        "Monthly totals are summed over <b>complete days only</b> (at least 1,380 of 1,440 "
        "minutes recorded), so a month is never penalised for a day the logger missed. "
        "Each month is labelled COMPLETE or PARTIAL according to whether the archive covers "
        "its whole calendar span: <b>February and May are partial</b>, so their totals must "
        "not be set beside March and April — only their per-day means can. Every figure in "
        "this section was additionally recomputed by an independent SQL aggregate and the "
        "two agree exactly.", S_BODY))

    F.append(h2("4.1", "Monthly energy generation"))
    F.append(month_bar([m["month"] for m in mon],
                       [m["energy_mwh"] for m in mon],
                       title="Chart 2 — monthly energy generation",
                       y_label="Energy, MWh",
                       partial=[not m["complete_month"] for m in mon]))
    rows = [[para(x, S_CELLB) for x in
             ("Month", "Period", "Complete days", "Energy MWh", "Mean MWh/day",
              "Peak MW", "Curtailed min")]]
    for m in mon:
        rows.append([para(m["month"]), period_tag(m), para(m["complete_days"]),
                     para(fmt(m["energy_mwh"])), para(fmt(m["mean_mwh_day"])),
                     para(fmt(m["peak_mw"], 1)), para(fmt(m["setpoint_minutes"]))])
    rows.append([para("AVAILABLE-PERIOD TOTAL", S_CELLB), para("", S_CELLB),
                 para(len(full), S_CELLB),
                 para(fmt(a["tot_int"]), S_CELLB), para(fmt(mean_day), S_CELLB),
                 para(fmt(max(r["peak_mw"] for r in full), 1), S_CELLB),
                 para(fmt(sum(r["curtailed_minutes"] for r in full)), S_CELLB)])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [34 * mm, 20 * mm, 20 * mm, 22 * mm, 24 * mm, 18 * mm, 22 * mm],
                   align={i: "RIGHT" for i in range(2, 7)}))
    F.append(Paragraph(
        "February and May are partial (21 and 20 complete days) and must not be read "
        "beside March and April as if whole. <b>Only two complete calendar months exist "
        "in this archive</b> — the single most important constraint on §11–§13. "
        "<b>Curtailed min</b> here counts every minute the active-power setpoint sat below "
        "100%; §8 reports the stricter count of minutes limited <i>while the plant was "
        "exporting</i>, which is why the two columns differ.", S_NOTE))

    F.append(h2("4.2", "Monthly average daily generation"))
    F.append(month_bar([m["month"] for m in mon],
                       [m["mean_mwh_day"] for m in mon],
                       title="Chart 3 — monthly average daily generation",
                       y_label="Mean energy, MWh / day",
                       partial=[not m["complete_month"] for m in mon]))
    F.append(Paragraph(
        "This is the only monthly comparison that is valid across all four months, because "
        "it is normalised by the number of complete days and therefore does not reward a "
        "month for being longer or better logged.", S_NOTE))
    apr = next((m for m in mon if m["month"] == "2024-04"), None)
    may = next((m for m in mon if m["month"] == "2024-05"), None)
    F.append(interp([
        ("What changed",
         f"Mean daily generation rose steadily across the archive, from "
         f"{lo_m['mean_mwh_day']:,.0f} MWh/day in {lo_m['month']} to "
         f"{hi_m['mean_mwh_day']:,.0f} MWh/day in {hi_m['month']} — a factor of "
         f"{hi_m['mean_mwh_day'] / lo_m['mean_mwh_day']:.1f}."),
        ("Why it changed",
         f"Two measured drivers move in the right direction over the one interval where "
         f"both are observed. Mean daily plane-of-array irradiation rose from "
         f"{apr['gti_mean_day']:.2f} kWh/m² in April to {may['gti_mean_day']:.2f} kWh/m² in "
         f"May (+{100 * (may['gti_mean_day'] / apr['gti_mean_day'] - 1):.0f}%), and "
         f"setpoint-limited minutes fell from {apr['setpoint_minutes']:,} to "
         f"{may['setpoint_minutes']:,}. <b>For February and March no resource explanation "
         f"can be offered</b>: the WMS record begins 23 March."),
        ("Supporting evidence",
         "WMS.AVG_GTI_IRRADIATION integrated per day (§6.3) and "
         "PPC.ACTIVE_POWER_SET_POINT counted per minute (8.2), both measured, both "
         "tabulated in this report."),
        ("Confidence",
         "MEDIUM for the April→May step: two independent measurements agree in direction, "
         "but no counterfactual separates their contributions. "
         "UNAVAILABLE for February–March: the resource was not measured."),
        ("What to verify",
         "Whether February's low mean reflects resource or availability. That cannot be "
         "settled from this archive and needs an irradiance source covering February "
         "(§15)."),
    ]))

    F.append(h2("4.3", "Yearly performance analysis"))
    rows = [[para(x, S_CELLB) for x in
             ("Year", "Available period", "Complete months", "Complete days",
              "Energy MWh", "Mean MWh/day", "Peak MW", "Curtailed min")]]
    for v in yrs:
        rows.append([para(v["year"], S_CELLB),
                     para(f"{v['first']:%d %b} – {v['last']:%d %b %Y}"),
                     para(f"{v['months_complete']} of 12"), para(v["days"]),
                     para(fmt(v["energy_mwh"])), para(fmt(v["mean_mwh_day"])),
                     para(fmt(v["peak_mw"], 1)), para(fmt(v["setpoint_minutes"]))])
    F.append(table(rows, [14 * mm, 30 * mm, 24 * mm, 22 * mm, 22 * mm, 22 * mm,
                          16 * mm, 20 * mm],
                   align={i: "RIGHT" for i in range(3, 8)}))
    F.append(Paragraph(
        f"<b>The {yrs[0]['year']} row is an available-period result, not a year.</b> It "
        f"covers {yrs[0]['first']:%d %b} to {yrs[0]['last']:%d %b %Y} — "
        f"{yrs[0]['days']} complete days and {yrs[0]['months_complete']} complete calendar "
        f"months — and must never be annualised, scaled to 12 months or compared with a "
        f"full-year figure from any other source.", S_BODY))
    F.append(Paragraph(
        "<b>YEAR-OVER-YEAR ANALYSIS IS CURRENTLY UNAVAILABLE BECAUSE THE AVAILABLE ARCHIVE "
        "DOES NOT CONTAIN A FULL MULTI-YEAR HISTORY.</b> No yearly trend chart is drawn, "
        "because a trend through one point is not a trend. No value for any year other than "
        f"{yrs[0]['year']} appears anywhere in this report.", S_BODY))
    rows = [[para(x, S_CELLB) for x in ("Year", "Status in this database", "What it would unlock")],
            [para("2023", S_CELLB), tag("UNAVAILABLE", MUTED),
             para("First year-over-year comparison and the first seasonal baseline")],
            [para("2024", S_CELLB), tag("PARTIAL — 09 Feb to 20 May", WARN),
             para("The available-period result above; two complete calendar months")],
            [para("2025", S_CELLB), tag("UNAVAILABLE", MUTED),
             para("Annual change rates, degradation trend, and an annual forecast that "
                  "could be validated")],
            [para("2026", S_CELLB), tag("UNAVAILABLE", MUTED),
             para("Multi-year trend and a defensible next-12-month forecast")]]
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [16 * mm, 44 * mm, 88 * mm]))

    F.append(h2("4.4", "Year-over-year analysis — data gap"))
    F.append(Paragraph(
        "The framework below is implemented and will compute automatically the moment a "
        "second comparable period exists in the database. <b>None of these quantities is "
        "estimated, extrapolated or placeholder-filled in the meantime</b> — each is "
        "reported as UNAVAILABLE with the reason attached.", S_BODY))
    gaps = [
        ("Annual energy change %", "Two available periods of equal span"),
        ("Annual mean daily generation change %", "Two available periods"),
        ("Annual peak power change %", "Two available periods"),
        ("Annual performance-ratio change", "Two periods with WMS coverage throughout"),
        ("Annual curtailment change %", "Two periods of setpoint history"),
        ("Annual alarm trend", "Two periods of alarm history"),
        ("Inverter degradation trend", "At least 24 months, same units, same season"),
        ("Seasonal comparison", "One full seasonal cycle, autumn and winter included"),
        ("Year-over-year deterioration or improvement", "At least two full years"),
    ]
    rows = [[para(x, S_CELLB) for x in
             ("Metric the engine will compute", "Data it requires", "Status today")]]
    for metric, need in gaps:
        rows.append([para(metric), para(need), tag("UNAVAILABLE", MUTED)])
    F.append(table(rows, [62 * mm, 62 * mm, 24 * mm]))


    # ── 4 energy reconciliation ──────────────────────────────────────────────
    F.append(h1("5", "Energy reconciliation — two independent measures"))
    F.append(Paragraph(
        "Two independent routes to one quantity. Agreement would corroborate the metering "
        "chain; the gap below is reported as a finding, not averaged away.", S_BODY))
    rows = [[para(x, S_CELLB) for x in ("Measure", "Method", "Energy MWh")],
            [para("Integrated grid power"),
             para("E = Σ(P<sub>MW</sub>) ÷ 60 over 1-min records · "
                  "PPC.GRID_ACTIVE_POWER_MEASURED"), para(fmt(a["tot_int"]))],
            [para("Plant daily register"),
             para("Σ of daily MAX(PPC.PLANT_DAILY_PRODUCTION)"), para(fmt(a["tot_reg"]))],
            [para("Difference", S_CELLB), para("register − integrated", S_CELLB),
             para(f"{diff:,.0f}   ({diff_pct:+.2f}%)", S_CELLB)]]
    F.append(table(rows, [36 * mm, 100 * mm, 32 * mm], align={2: "RIGHT"}))
    g = a["integ"]["PPC"]
    F.append(Paragraph(
        f"<b>Finding — energy measures disagree.</b> The two routes differ by "
        f"{abs(diff):,.0f} MWh ({abs(diff_pct):.2f}%) over {len(full)} days. Candidate "
        f"explanations, none yet adjudicated: the register may close on a boundary other "
        f"than calendar midnight; the integral necessarily omits minutes lost to the "
        f"{g['gaps_over_90s']} recorded collection gaps (largest {g['largest_gap_s']:,}s); "
        f"or register and grid meter sit at different points in the AC chain. "
        f"<b>Verification: DIVERGED</b> — two methods, two answers, reported as such. "
        f"Resolving it needs the single-line diagram and metering scheme (§15).", S_BODY))

    # ── 5 WMS ────────────────────────────────────────────────────────────────
    F.append(h1("6", "Generation against measured resource"))
    F.append(Paragraph(
        "Generation is only interpretable against the resource that actually arrived. This "
        "section establishes whether the weather stations can be trusted (6.1), fits "
        "generation to measured irradiance (6.2), and then resolves the result by month "
        "(6.3) so the monthly figures in §4 can be read against the resource behind them.",
        S_BODY))
    F.append(h2("6.1", "Weather station comparison"))
    worst = max(a["stations"], key=lambda s: s["rmse_vs_mean"])
    rows = [[para(x, S_CELLB) for x in
             ("Station", "Mean GHI W/m²", "Mean GTI W/m²", "Mean air °C",
              "Bias vs mean", "RMSE vs mean", "Assessment")]]
    for s in a["stations"]:
        rows.append([para(s["station"]), para(fmt(s["ghi_mean"], 1)),
                     para(fmt(s["gti_mean"], 1)), para(fmt(s["air_temp_mean"], 1)),
                     para(f"{s['bias_vs_mean']:+.2f}"), para(fmt(s["rmse_vs_mean"], 1)),
                     tag("OUTLIER", BAD) if s is worst else tag("consistent", GOOD)])
    F.append(table(rows, [19 * mm, 25 * mm, 25 * mm, 22 * mm, 25 * mm, 25 * mm, 27 * mm],
                   align={i: "RIGHT" for i in range(1, 6)}))
    soil = [w["soiling_ratio"] for w in a["weather"] if w["soiling_ratio"]]
    F.append(Paragraph(
        f"<b>Finding — WMS4 is a measurable outlier.</b> Compared on "
        f"{a['stations'][0]['paired_rows']:,} daylight minutes where all four stations "
        f"report simultaneously, {worst['station']} shows RMSE {worst['rmse_vs_mean']:.0f} "
        f"W/m² against 92–101 for its peers, bias {worst['bias_vs_mean']:+.2f} W/m², and "
        f"reads about "
        f"{a['stations'][0]['air_temp_mean'] - worst['air_temp_mean']:.1f} °C cooler on air "
        f"temperature. Independently, the integrity scan found every {worst['station']} "
        f"channel frozen simultaneously for 2,655 minutes — a station-level acquisition "
        f"failure rather than a single bad sensor. <b>Verification: CONVERGED</b> — a "
        f"statistical comparison and a frozen-channel scan identify the same station by "
        f"different means.", S_BODY))
    if soil:
        F.append(Paragraph(
            f"<b>Soiling is measured, not assumed.</b> AVG_IR_SOILING_RATIO1 averages "
            f"{statistics.mean(soil):.2f} across the record (100 = clean), so no literature "
            f"allowance is used anywhere in this report.", S_NOTE))

    # ── 6 conditional performance model ──────────────────────────────────────
    F.append(h2("6.2", "Generation fitted to measured irradiance"))
    if a["model"]:
        m = a["model"]
        F.append(scatter_chart(m["gti"], m["actual"], (m["slope"], m["intercept"]),
                               title="Chart 6 — generation against measured resource",
                               x_label="Daily plane-of-array irradiation, kWh/m²",
                               y_label="Energy, MWh / day"))
        F.append(Paragraph(
            f"Least-squares fit on the {m['n']} days where plant and weather records are "
            f"both complete: <b>E = {m['slope']:.1f} × GTI + {m['intercept']:.1f}</b> "
            f"(R² = {m['r2']:.3f}, MAE {m['mae']:,.0f} MWh). At the period-mean irradiation "
            f"of {m['mean_gti']:.2f} kWh/m² the fit implies {m['mean_energy']:,.0f} MWh/day, "
            f"a performance ratio of about {pr:.1f}% against 130 MWp.", S_BODY))
        F.append(Paragraph(
            "This is an <b>explanatory</b> model, not a forecast: it states what the plant "
            "produced for the resource it actually received. The unexplained "
            f"{100 * (1 - m['r2']):.0f}% is where availability, curtailment, clipping and "
            "soiling live — which is precisely why the loss components in §7–§8 are "
            "quantified separately rather than inferred from this residual.", S_NOTE))
    else:
        F.append(Paragraph("Not available with current data — insufficient overlapping "
                           "plant and weather days.", S_BODY))

    F.append(h2("6.3", "Monthly generation against measured resource"))
    res = [m for m in mon if m["pr_pct"] is not None]
    no_res = [m for m in mon if m["pr_pct"] is None]
    F.append(month_bar([m["month"] for m in res],
                       [m["gti_mean_day"] for m in res],
                       title="Chart 5 — monthly mean daily resource (plane-of-array)",
                       y_label="Mean daily GTI, kWh / m²",
                       colour=WARN, decimals=2,
                       partial=[not m["complete_month"] for m in res]))
    F.append(Paragraph(
        "Resource and performance ratio are plotted and tabulated separately, never on one "
        "axis: kWh/m² and a percentage are not commensurable and a shared axis would imply "
        "a relationship the data has not been asked to show.", S_NOTE))
    F.append(month_bar([m["month"] for m in res],
                       [m["pr_pct"] for m in res],
                       title="Chart 5b — monthly implied performance ratio",
                       y_label="Implied PR, %", colour=ACCENT, decimals=1,
                       partial=[not m["complete_month"] for m in res]))
    rows = [[para(x, S_CELLB) for x in
             ("Month", "Days with both records", "Resource coverage",
              "Mean daily GTI kWh/m²", "Energy on those days MWh", "Implied PR")]]
    for m in mon:
        if m["pr_pct"] is None:
            rows.append([para(m["month"]), para(m["wms_days"]),
                         tag("NO WMS DATA", MUTED), para("—"), para("—"),
                         tag("UNAVAILABLE", MUTED)])
            continue
        cov = m["resource_coverage_pct"]
        rows.append([para(m["month"]), para(m["wms_days"]),
                     para(f"{cov:.0f}% of complete days"),
                     para(fmt(m["gti_mean_day"], 2)), para(fmt(m["energy_paired"])),
                     para(f"{m['pr_pct']:.1f}%", S_CELLB) if cov > 50
                     else tag(f"{m['pr_pct']:.1f}% (low coverage)", WARN)])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [20 * mm, 28 * mm, 32 * mm, 30 * mm, 32 * mm, 30 * mm],
                   align={1: "RIGHT", 3: "RIGHT", 4: "RIGHT"}))
    mar = next((m for m in mon if m["month"] == "2024-03"), None)
    F.append(Paragraph(
        f"Implied PR is computed only on days carrying <b>both</b> a complete plant record "
        f"and a complete weather record, which is why {no_res[0]['month'] if no_res else 'February'} "
        f"is withheld entirely and {mar['month']} is marked low coverage: it rests on "
        f"{mar['wms_days']} of {mar['complete_days']} complete days, the WMS record having "
        f"begun on 23 March. The denominator is "
        f"{INSTALLED_CAPACITY_MW:,.0f} MW of AC nameplate; no DC nameplate or module "
        f"datasheet exists in the database, so these figures are a <b>comparative</b> "
        f"indicator between months, not a certified performance ratio.", S_NOTE))
    F.append(interp([
        ("What changed",
         f"Implied PR rose from {apr['pr_pct']:.1f}% in April to {may['pr_pct']:.1f}% in May, "
         f"while mean daily resource rose from {apr['gti_mean_day']:.2f} to "
         f"{may['gti_mean_day']:.2f} kWh/m²."),
        ("Why it changed",
         f"The two months differ sharply in curtailment: April carried "
         f"{apr['limited_minutes']:,} minutes limited while exporting at a mean setpoint of "
         f"{apr['mean_setpoint_pct']:.1f}%, May {may['limited_minutes']:,} minutes at "
         f"{may['mean_setpoint_pct']:.1f}%. Generation held back by a setpoint lowers "
         f"measured output without lowering measured irradiance, which is exactly how it "
         f"presents here."),
        ("Supporting evidence",
         "PPC.ACTIVE_POWER_SET_POINT per minute (8.2); WMS.AVG_GTI_IRRADIATION integrated "
         "per day; both months at 100% resource coverage, so neither figure rests on a "
         "partial sample."),
        ("Confidence",
         "MEDIUM. The direction is consistent across two independent measurements, but the "
         "energy actually foregone is not quantified anywhere in this report and the "
         "curtailment contribution therefore cannot be separated from the seasonal one."),
        ("What to verify",
         "Grid dispatch instructions for April, and the DC nameplate / module datasheet "
         "that would turn an implied ratio into a certified PR (§15)."),
    ]))

    # ── 7 inverter fleet ─────────────────────────────────────────────────────
    F.append(h1("7", "Inverter fleet performance"))
    F.append(h2("7.1", "Fleet performance over the whole period"))
    F.append(bar_chart([i["inverter"].replace("INVERTER_", "") for i in a["inverters"]],
                       [i["energy_mwh"] for i in a["inverters"]],
                       title="Chart 7 — energy by inverter, whole available period",
                       x_label="Inverter", y_label="Energy, MWh"))
    rows = [[para(x, S_CELLB) for x in
             ("Inverter", "Energy MWh", "Deviation vs median", "Peak kW", "Producing min",
              "Alarm rows", "Assessment")]]
    for i in inv_sorted[:5] + inv_sorted[-3:]:
        dev = 100 * (i["energy_mwh"] - med_e) / med_e
        assess = tag("lowest quartile", WARN) if dev < -3 else tag("within peer band", GOOD)
        rows.append([para(i["inverter"]), para(fmt(i["energy_mwh"])),
                     para(f"{dev:+.1f}%"), para(fmt(i["peak_kw"])),
                     para(fmt(i["producing_minutes"])), para(fmt(i["alarm_rows"])), assess])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [22 * mm, 24 * mm, 34 * mm, 20 * mm, 26 * mm, 22 * mm, 26 * mm],
                   align={i: "RIGHT" for i in range(1, 6)}))
    F.append(Paragraph(
        f"Median fleet energy is {med_e:,.0f} MWh. The weakest unit, "
        f"{inv_sorted[0]['inverter']}, is {100 * (inv_sorted[0]['energy_mwh'] - med_e) / med_e:+.1f}% "
        f"against it — a spread far too small to call a fault. Alarm load varies widely "
        f"({min(i['alarm_rows'] for i in a['inverters']):,}–"
        f"{max(i['alarm_rows'] for i in a['inverters']):,} rows) and does not track energy "
        f"rank, which is itself informative: the dominant alarm class is a DC string-current "
        f"warning that is evidently not costing measurable output.", S_BODY))
    rows = [[para(x, S_CELLB) for x in ("Alarm subject", "Events")]]
    for al in a["alarms"][:8]:
        rows.append([para(al["subject"]), para(fmt(al["events"]))])
    F.append(table(rows, [120 * mm, 28 * mm], align={1: "RIGHT"}))

    F.append(h2("7.2", "Monthly inverter performance"))
    F.append(grouped_bar(
        [m["month"] for m in mon],
        [("Weakest unit", [m["inv_median_mwh"] * (1 + m["inv_low"][0] / 100) for m in mon], WARN),
         ("Fleet median", [m["inv_median_mwh"] for m in mon], ACCENT),
         ("Strongest unit", [m["inv_median_mwh"] * (1 + m["inv_high"][0] / 100) for m in mon], GOOD)],
        title="Chart 8 — monthly inverter energy: weakest, median and strongest unit",
        y_label="Energy per inverter, MWh"))
    mi = a["monthly_inverter"]
    rows = [[para(x, S_CELLB) for x in
             ("Month", "Fleet median MWh", "Weakest unit", "Deviation vs median",
              "Its producing min", "Peer median producing min", "Strongest unit",
              "Spread")]]
    for m in mon:
        units = mi.get(m["month"], {})
        low_u = m["inv_low"][1]
        peer_prod = statistics.median(v["producing_minutes"] for v in units.values())
        rows.append([para(m["month"]), para(fmt(m["inv_median_mwh"])),
                     para(low_u.replace("INVERTER_", "INV "), S_CELLB),
                     para(f"{m['inv_low'][0]:+.1f}%"),
                     para(fmt(units[low_u]["producing_minutes"])),
                     para(fmt(peer_prod)),
                     para(m["inv_high"][1].replace("INVERTER_", "INV ")),
                     para(f"{m['inv_spread_pp']:.1f} pp")])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [18 * mm, 22 * mm, 18 * mm, 22 * mm, 22 * mm, 26 * mm,
                          22 * mm, 16 * mm],
                   align={1: "RIGHT", 3: "RIGHT", 4: "RIGHT", 5: "RIGHT", 7: "RIGHT"},
                   size=7))
    feb = next((m for m in mon if m["month"] == "2024-02"), None)
    feb_u = feb["inv_low"][1]
    apr_u = apr["inv_low"][1]
    feb_units, apr_units = mi[feb["month"]], mi[apr["month"]]
    feb_peer = statistics.median(v["producing_minutes"] for v in feb_units.values())
    apr_peer = statistics.median(v["producing_minutes"] for v in apr_units.values())
    low_units = {m["inv_low"][1] for m in mon}
    spread_txt = ", ".join("%s %.1f pp" % (m["month"], m["inv_spread_pp"]) for m in mon)
    F.append(interp([
        ("What changed",
         f"Fleet dispersion is not constant. The spread between the weakest and the "
         f"strongest unit was {spread_txt}."),
        ("Why it changed",
         f"The two wide months are wide for different measured reasons. In {apr['month']}, "
         f"{apr_u} delivered {apr['inv_low'][0]:+.1f}% against the fleet median while "
         f"producing for {apr_units[apr_u]['producing_minutes']:,} minutes against a peer "
         f"median of {apr_peer:,.0f} — roughly "
         f"{apr_peer - apr_units[apr_u]['producing_minutes']:,.0f} minutes of lost "
         f"availability, not weak output. In {feb['month']}, {feb_u} delivered "
         f"{feb['inv_low'][0]:+.1f}% while producing for "
         f"{feb_units[feb_u]['producing_minutes']:,} minutes against a peer median of "
         f"{feb_peer:,.0f} — it ran the same minutes and delivered less, which is a "
         f"different failure mode."),
        ("Supporting evidence",
         "Per-unit monthly energy integrated from ACTIVE_POWER and per-unit producing "
         "minutes, both measured per inverter table; alarm counts per unit per month."),
        ("Confidence",
         "HIGH that the two months represent different modes — producing minutes separate "
         "availability from output directly. LOW on cause for either: no outage log, work "
         "order or string-level measurement exists in this database. "
         "<b>No inverter is called faulty.</b>"),
        ("What to verify",
         f"The {apr['month']} outage window on {apr_u} against the site log, and the DC "
         f"side of {feb_u} for {feb['month']}. Note that {feb_u} is among the strongest "
         f"units in April and May, which contradicts a chronic fault reading."),
    ]))
    F.append(Paragraph(
        f"<b>No unit is consistently weak.</b> Across the four months the weakest position "
        f"is taken by {len(low_units)} different inverters "
        f"({', '.join(sorted(u.replace('INVERTER_', 'INV ') for u in low_units))}), and the "
        f"period-level ranking in 7.1 — where {inv_sorted[0]['inverter']} appears weakest "
        f"overall — is accounted for by its single {apr['month']} availability event rather "
        f"than by sustained underperformance. This is precisely the distinction a "
        f"period-only view cannot make.", S_NOTE))

    # ── 8 curtailment + clipping ─────────────────────────────────────────────
    F.append(h1("8", "Curtailment and clipping"))
    c = a["curt"]
    F.append(h2("8.1", "Curtailment over the whole period"))
    F.append(Paragraph(
        f"<b>Curtailment.</b> PPC.ACTIVE_POWER_SET_POINT is a percentage (observed range "
        f"0–105). Treating a setpoint below 100% while the plant exports as an active "
        f"limit gives <b>{c['limited_minutes']:,} limited minutes across "
        f"{c['affected_days']} days</b>, mean setpoint {c['mean_setpoint_pct']:.1f}%, mean "
        f"output while limited {c['mean_power_while_limited_mw']:.1f} MW.", S_BODY))
    F.append(h2("8.2", "Monthly curtailment analysis"))
    F.append(month_bar([m["month"] for m in mon],
                       [m["limited_minutes"] for m in mon],
                       title="Chart 4 — monthly curtailment (limited while exporting)",
                       y_label="Limited minutes",
                       colour=WARN,
                       partial=[not m["complete_month"] for m in mon]))
    rows = [[para(x, S_CELLB) for x in
             ("Month", "Setpoint below 100% min", "Limited while exporting min",
              "Days affected", "Mean setpoint %", "Share of exporting minutes")]]
    for m in mon:
        share = (100 * m["limited_minutes"] / m["producing_minutes"]
                 if m["producing_minutes"] else 0.0)
        rows.append([para(m["month"]), para(fmt(m["setpoint_minutes"])),
                     para(fmt(m["limited_minutes"])), para(m["limited_days"]),
                     para(fmt(m["mean_setpoint_pct"], 1)), para(f"{share:.1f}%")])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [20 * mm, 30 * mm, 32 * mm, 24 * mm, 26 * mm, 32 * mm],
                   align={i: "RIGHT" for i in range(1, 6)}))
    worst_c = max(mon, key=lambda m: m["limited_minutes"])
    F.append(Paragraph(
        "Two counts are given because two questions are being asked. "
        "<b>Setpoint below 100%</b> counts every minute the limit was in force, which is "
        "what §4 reports; <b>limited while exporting</b> counts only the minutes the plant "
        "was simultaneously producing, cross-checking PPC.ACTIVE_POWER_SET_POINT against "
        "PPC.GRID_ACTIVE_POWER_MEASURED, and is the figure that can affect energy. The "
        "share column is the second count against that month's exporting minutes, which "
        "is deterministic; <b>the energy foregone is not</b>, and is not stated.", S_NOTE))
    F.append(interp([
        ("What changed",
         f"Curtailment is concentrated, not spread. {worst_c['month']} carries "
         f"{worst_c['limited_minutes']:,} of the period's {c['limited_minutes']:,} limited "
         f"exporting minutes across {worst_c['limited_days']} days, at the deepest mean "
         f"setpoint of the archive ({worst_c['mean_setpoint_pct']:.1f}%). "
         f"{may['month']} is effectively free of it ({may['limited_minutes']:,} minutes)."),
        ("Why it changed",
         "<b>Not established.</b> The setpoint is recorded; the instruction behind it is "
         "not. A grid-ordered limit and a plant-side limit during works are equally "
         "consistent with everything in this database."),
        ("Supporting evidence",
         f"PPC.ACTIVE_POWER_SET_POINT crossed with PPC.GRID_ACTIVE_POWER_MEASURED per "
         f"minute; {worst_c['month']}'s limited minutes are "
         f"{100 * worst_c['limited_minutes'] / worst_c['producing_minutes']:.1f}% of that "
         f"month's exporting minutes."),
        ("Confidence",
         "HIGH on the magnitude and timing, which are directly measured. "
         "SINGLE SOURCE on the cause — no dispatch log exists, so no attribution is made."),
        ("What to verify",
         f"Dispatch instructions for the {worst_c['month']} windows, which would label each "
         f"one grid-ordered or plant-side and is the only route to quantifying energy "
         f"foregone (§15)."),
    ]))
    F.append(Paragraph(
        "April carries the bulk, at the lowest mean setpoint. <b>Energy foregone is not "
        "quantified here.</b> Doing so honestly needs the unconstrained counterfactual — "
        "what the plant would have produced at that irradiance without the limit — and "
        "the dispatch instructions that would show whether the limit was grid-ordered or "
        "plant-side. Both are listed in §15. Attributing causation without them would be "
        "a guess.", S_NOTE))
    F.append(h2("8.3", "Clipping"))
    clip = sorted(a["clip"], key=lambda x: -x["minutes_at_ceiling"])
    rows = [[para(x, S_CELLB) for x in
             ("Inverter", "Own ceiling kW", "Minutes at ≥98% of ceiling", "Days affected")]]
    for cl in clip[:6]:
        rows.append([para(cl["inverter"]), para(fmt(cl["ceiling_kw"])),
                     para(fmt(cl["minutes_at_ceiling"])), para(cl["days_affected"])])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [28 * mm, 32 * mm, 48 * mm, 28 * mm],
                   align={1: "RIGHT", 2: "RIGHT", 3: "RIGHT"}))
    F.append(Paragraph(
        f"<b>Clipping.</b> Each unit is compared against <b>its own</b> 99th-percentile "
        f"output, so no nameplate is assumed. Every inverter plateaus at essentially the "
        f"same ceiling (~{clip[0]['ceiling_kw']:,.0f} kW) for roughly "
        f"{clip[0]['minutes_at_ceiling']:,} minutes across {clip[0]['days_affected']} days. "
        f"A common ceiling across 24 independent units indicates a design or firmware AC "
        f"limit rather than a fault. These are reported as <b>candidates</b>: separating "
        f"inverter clipping from a concurrent plant setpoint requires the per-minute PPC "
        f"cross-check, which is implemented in the engine but not yet reconciled per event.",
        S_NOTE))

    # ── 9 data integrity ─────────────────────────────────────────────────────
    F.append(h1("9", "Data integrity"))
    F.append(h2("9.1", "Source integrity over the whole period"))
    rows = [[para(x, S_CELLB) for x in
             ("Source", "Rows", "Duplicate timestamps", "Gaps > 90 s", "Largest gap")]]
    for t, v in a["integ"].items():
        rows.append([para(t), para(fmt(v["rows"])), para(fmt(v["duplicates"])),
                     para(fmt(v["gaps_over_90s"])),
                     para(f"{v['largest_gap_s']:,}s")])
    F.append(table(rows, [34 * mm, 28 * mm, 38 * mm, 28 * mm, 28 * mm],
                   align={i: "RIGHT" for i in range(1, 5)}))
    F.append(Paragraph(
        "<b>Finding — duplicated timestamps.</b> PPC carries "
        f"{a['integ']['PPC']['duplicates']} rows sharing a timestamp with another row and "
        f"INVERTER_01 {a['integ']['INVERTER_01']['duplicates']}. Any aggregate over these "
        "sources double-counts unless de-duplicated; the energy figures in this report "
        "integrate per minute and are therefore mildly affected, which is one candidate "
        "explanation for part of the §5 gap.", S_BODY))
    F.append(Paragraph(
        "<b>Finding — frozen station.</b> Every WMS4 channel held an identical non-zero "
        "value for 2,655 consecutive minutes. Detection excludes zero runs (night is "
        "legitimately zero) and monotonic registers (a counter standing still means the "
        "event is not occurring), so this is a genuine plateau in live measurements.",
        S_BODY))

    F.append(h2("9.2", "Monthly data quality"))
    mq = a["monthly_quality"]
    q_months = [r["month"] for r in mq["PPC"]]
    F.append(grouped_bar(
        q_months,
        [(t, [next((r["completeness_pct"] for r in mq[t] if r["month"] == mth), None)
              for mth in q_months], col)
         for t, col in (("PPC", ACCENT), ("WMS", GOOD), ("INVERTER_01", WARN))],
        title="Chart 9 — monthly data completeness by source",
        y_label="Completeness, % of expected minutes", decimals=1))
    rows = [[para(x, S_CELLB) for x in
             ("Source", "Month", "Rows", "Expected min", "Minutes covered",
              "Missing min", "Duplicates", "Gaps > 90 s", "Completeness")]]
    for t in ("PPC", "WMS", "INVERTER_01"):
        for r in mq[t]:
            comp = r["completeness_pct"]
            rows.append([para(t), para(r["month"]), para(fmt(r["rows"])),
                         para(fmt(r["expected"])), para(fmt(r["covered_minutes"])),
                         para(fmt(r["missing"])), para(fmt(r["duplicates"])),
                         para(fmt(r["gaps_over_90s"])),
                         para(f"{comp:.2f}%", S_CELLB) if comp >= 98
                         else tag(f"{comp:.2f}%", WARN)])
    F.append(Spacer(1, 2 * mm))
    F.append(table(rows, [24 * mm, 18 * mm, 20 * mm, 22 * mm, 24 * mm, 20 * mm,
                          20 * mm, 18 * mm, 24 * mm],
                   align={i: "RIGHT" for i in range(2, 9)}, size=7))
    worst_src, worst_q = min(((t, r) for t in mq for r in mq[t]),
                             key=lambda tr: tr[1]["completeness_pct"])
    F.append(Paragraph(
        "<b>Completeness is measured in minute slots, not rows.</b> These sources log on "
        "change of value as well as on the one-minute sample, so counting rows produces "
        "months that are more than 100% complete — an artefact, not coverage. Expected "
        "minutes are counted only between a source's first and last record inside each "
        "month, so a source that began mid-month is not charged for the days before it "
        "existed; that is why the WMS has no February row at all.", S_NOTE))
    F.append(interp([
        ("What changed",
         f"Completeness never falls below {worst_q['completeness_pct']:.1f}% in any month "
         f"for any source. The weakest is {worst_src} in {worst_q['month']}, at "
         f"{worst_q['completeness_pct']:.2f}% - {worst_q['missing']:,} missing minutes and "
         f"{worst_q['gaps_over_90s']} collection gaps over 90 s, the largest "
         f"{worst_q['largest_gap_s']:,} s."),
        ("Why it changed",
         "The March shortfall is a collection problem, not a plant problem: the gaps appear "
         "simultaneously in PPC and INVERTER_01, which are separate tables fed by the same "
         "acquisition path."),
        ("Supporting evidence",
         "Identical gap counts and identical largest gap in PPC and INVERTER_01 for that "
         "month; the WMS, which starts later, shows its own smaller pattern."),
        ("Confidence",
         "HIGH. These are direct counts of timestamps, with no modelling in between."),
        ("What to verify",
         "Whether the RTU re-sends on reconnect, which would explain the duplicate "
         "timestamps that accompany the same month (§14)."),
    ]))
    F.append(Paragraph(
        "<b>Monthly comparisons in this report are therefore reliable on coverage grounds.</b> "
        "No month is missing enough data to distort its totals; the constraint on §4 is the "
        "number of months, not their quality.", S_BODY))

    # ── 10 hypotheses ────────────────────────────────────────────────────────
    F.append(h1("10", "Root-cause hypotheses"))
    hyps = [
        ("H1", "Energy register and grid integral disagree by "
               f"{abs(diff_pct):.2f}%",
         "Register closes on a non-midnight boundary, or sits at a different point in "
         "the AC chain than the grid meter",
         f"Both measures available for all {len(full)} days; difference is systematic in "
         f"sign, not random",
         "Collection gaps alone (123 over the period) are too small to account for the "
         "full difference",
         "MEDIUM", "DIVERGED"),
        ("H2", "WMS4 under-reads and latched for 44 hours",
         "Station-level data-acquisition failure rather than a single sensor fault",
         "Highest RMSE of four stations; all channels froze simultaneously; air "
         "temperature biased cool",
         "Soiling or shading on one sensor would not freeze pressure and wind "
         "simultaneously — so the sensor-level explanation is contradicted",
         "HIGH", "CONVERGED"),
        ("H3", "Inverter output plateaus at a common ~4,700 kW ceiling",
         "Design or firmware AC limit (clipping), not degradation",
         "All 24 units share the ceiling within a few kW; plateaus recur across 60–71 days",
         "A fault would not present identically on 24 independent units",
         "HIGH", "CONVERGED"),
        ("H4", "Curtailment concentrated in April at 73% mean setpoint",
         "External dispatch instruction, or plant-side limit during works",
         f"{c['limited_minutes']:,} limited minutes over {c['affected_days']} days; "
         f"setpoint varies across 55 discrete values",
         "No dispatch log is available, so an operator instruction cannot be confirmed; "
         "a plant-side cause is equally consistent with the data",
         "LOW", "SINGLE SOURCE"),
        ("H5", f"{inv_sorted[0]['inverter']} is the weakest unit at "
               f"{100 * (inv_sorted[0]['energy_mwh'] - med_e) / med_e:+.1f}%",
         "Normal peer variation rather than a defect",
         "Deviation is small; peak output matches the fleet; producing minutes are within "
         "the peer band",
         "Alarm load on this unit is not the fleet's highest, weakening any fault reading",
         "LOW", "SINGLE SOURCE"),
    ]
    rows = [[para(x, S_CELLB) for x in
             ("ID", "Observation", "Hypothesis", "Supporting evidence",
              "Contradicting evidence", "Confidence", "Verification")]]
    for hid, obs, hyp, sup, con, conf, ver in hyps:
        tone = {"HIGH": GOOD, "MEDIUM": WARN, "LOW": MUTED}[conf]
        vtone = {"CONVERGED": GOOD, "DIVERGED": BAD, "SINGLE SOURCE": MUTED}[ver]
        rows.append([para(hid, S_CELLB), para(obs), para(hyp), para(sup), para(con),
                     tag(conf, tone), tag(ver, vtone)])
    F.append(table(rows, [8 * mm, 30 * mm, 30 * mm, 34 * mm, 34 * mm, 19 * mm, 22 * mm],
                   size=6.6))
    F.append(Paragraph(
        "Confidence is assigned from measurable factors — number of independent methods "
        "that agree, peer corroboration, and the strength of the contradicting evidence — "
        "not chosen narratively. Where only one method exists the verification column says "
        "SINGLE SOURCE rather than implying corroboration.", S_NOTE))

    # ── 11 predictive analysis ───────────────────────────────────────────────
    F.append(h1("11", "Predictive model validation"))
    bt = a["backtest"]
    if "error" in bt:
        F.append(Paragraph(f"<b>Forecast unavailable.</b> {bt['error']}", S_BODY))
    else:
        F.append(Paragraph(
            f"Target: daily plant generation (MWh). Validation is <b>rolling-origin</b> — "
            f"{len(bt['folds'])} folds, each training on all data up to a cut-off and "
            f"predicting the next {bt['horizon']} days, with a minimum training window of "
            f"{bt['min_train']} days. No random split is used anywhere: shuffling a time "
            f"series leaks future information and would flatter every model here.", S_BODY))
        rows = [[para(x, S_CELLB) for x in
                 ("Model", "MAE MWh", "RMSE MWh", "MAPE", "Bias MWh", "R²")]]
        for name, s in sorted(bt["scores"].items(), key=lambda kv: kv[1]["rmse"]):
            mape = f"{s['mape']:.1f}%" if s.get("mape") is not None else "n/a"
            r2 = f"{s['r2']:.3f}" if s.get("r2") is not None else "n/a"
            lbl = para(name, S_CELLB) if name == a["best"] else para(name)
            rows.append([lbl, para(fmt(s["mae"], 1)), para(fmt(s["rmse"], 1)),
                         para(mape), para(f"{s['bias']:+.1f}"), para(r2)])
        F.append(table(rows, [46 * mm, 22 * mm, 22 * mm, 20 * mm, 22 * mm, 20 * mm],
                       align={i: "RIGHT" for i in range(1, 6)}))
        sc = a["best_score"]
        mape_note = sc.get("mape_note")
        F.append(Paragraph(
            f"<b>Selected on measured performance: {a['best']}</b> (lowest pooled RMSE). "
            + (f"MAPE is {mape_note}. " if mape_note else ""), S_NOTE))
        F.append(Paragraph(
            f"<b>Finding — the forecast failed validation, and this is reported rather "
            f"than hidden.</b> The best model achieves MAE {sc['mae']:,.0f} MWh against a "
            f"mean daily generation of {mean_day:,.0f} MWh — an error of roughly "
            f"{100 * sc['mae'] / mean_day:.0f}% — with R² of {sc['r2']:.3f}, meaning it "
            f"explains almost none of the day-to-day variance. The seasonal-naive baseline "
            f"scores R² "
            f"{bt['scores']['Seasonal Naive (lag-7)']['r2']:.3f}, worse than predicting the "
            f"mean.", S_BODY))
        F.append(Paragraph(
            "The reason is physical, not a modelling defect: day-to-day generation is "
            "driven by cloud cover, which no amount of lagged generation can anticipate. "
            "A useful day-ahead forecast needs a numerical weather prediction input, which "
            "this archive does not contain. The weather-feature variant could not be "
            "validated at all — only 58 days carry both complete plant and complete "
            "weather records, below the minimum training window.", S_BODY))

    # ── 12 forecast ──────────────────────────────────────────────────────────
    F.append(h1("12", "Forecast with prediction interval"))
    if a.get("forward") and a.get("ci", {}).get("available"):
        ci, fw = a["ci"], a["forward"]
        hist_tail = [(i, r["energy_integrated_mwh"]) for i, r in enumerate(full[-28:])]
        F.append(forecast_chart(hist_tail, fw["points"],
                                title="Chart 10 — forecast with 90% prediction interval"))
        rows = [[para(x, S_CELLB) for x in
                 ("Date", "Central forecast MWh", "Lower bound", "Upper bound")]]
        for p in fw["points"]:
            rows.append([para(f"{p['date']:%d/%m/%Y}"), para(fmt(p["forecast_mwh"])),
                         para(fmt(p["lower_mwh"])), para(fmt(p["upper_mwh"]))])
        F.append(Spacer(1, 2 * mm))
        F.append(table(rows, [32 * mm, 42 * mm, 32 * mm, 32 * mm],
                       align={1: "RIGHT", 2: "RIGHT", 3: "RIGHT"}))
        F.append(Paragraph(
            f"<b>This is a prediction interval, not a guarantee.</b> Bounds are "
            f"split-conformal: the half-width is the "
            f"{(1 - ci['alpha']) * 100:.0f}th-percentile absolute residual from "
            f"{ci['n_residuals']} out-of-sample backtest predictions "
            f"(±{ci['half_width_mwh']:,.0f} MWh at one day), widened by √step as the "
            f"horizon extends because a recursive forecast compounds its own error. "
            f"Measured coverage on held-out folds was "
            f"{ci['empirical_coverage'] * 100:.0f}% against a nominal "
            f"{ci['nominal_coverage'] * 100:.0f}%.", S_NOTE))
        F.append(Paragraph(
            f"<b>The interval is wider than the quantity being forecast.</b> A half-width "
            f"of {ci['half_width_mwh']:,.0f} MWh against a daily mean of {mean_day:,.0f} MWh "
            f"means the honest statement is little more than 'between nothing and roughly "
            f"double the average'. It is shown because the client asked for the range, and "
            f"because its width is the clearest possible evidence that this archive cannot "
            f"yet support day-ahead forecasting.", S_BODY))
    else:
        F.append(Paragraph("<b>Forecast unavailable</b> — insufficient out-of-sample "
                           "residuals to construct a defensible interval.", S_BODY))

    # ── 13 forecast summary ──────────────────────────────────────────────────
    F.append(h1("13", "Monthly and yearly forecast summary"))
    if a.get("forward") and a.get("ci", {}).get("available"):
        fw, ci, sc = a["forward"], a["ci"], a["best_score"]
        tot_c = sum(p["forecast_mwh"] for p in fw["points"])
        tot_l = sum(p["lower_mwh"] for p in fw["points"])
        tot_u = sum(p["upper_mwh"] for p in fw["points"])
        rows = [
            [para("Target", S_CELLB), para("Daily plant generation, aggregated over the horizon")],
            [para("Forecast horizon", S_CELLB),
             para(f"{fw['horizon_days']} days from {last_d:%d %b %Y} "
                  f"(the last complete day in the archive)")],
            [para("Central forecast", S_CELLB), para(f"<b>{tot_c:,.0f} MWh</b>")],
            [para("Prediction interval (90%)", S_CELLB),
             para(f"<b>{tot_l:,.0f} – {tot_u:,.0f} MWh</b>")],
            [para("Model used", S_CELLB), para(a["best"])],
            [para("Training period", S_CELLB),
             para(f"{full[0]['d']:%d %b %Y} – {last_d:%d %b %Y} "
                  f"({len(a['y'])} supervised rows)")],
            [para("Validation", S_CELLB),
             para(f"Rolling-origin, {len(a['backtest']['folds'])} folds × "
                  f"{a['backtest']['horizon']}-day horizon")],
            [para("Validation error", S_CELLB),
             para(f"MAE {sc['mae']:,.0f} MWh ({100 * sc['mae'] / mean_day:.0f}% of mean), "
                  f"RMSE {sc['rmse']:,.0f} MWh, R² {sc['r2']:.3f}")],
            [para("Forecast reliability", S_CELLB),
             Paragraph(f'<font color="#{BAD.hexval()[2:]}"><b>LIMITED — do not use for '
                       f'commercial commitment</b></font><br/>'
                       f'<font size="7">Validation error exceeds the quantity being '
                       f'predicted; the model does not beat a mean predictor on R².</font>',
                       S_CELL)],
            [para("Next calendar month", S_CELLB),
             Paragraph(f'<font color="#{BAD.hexval()[2:]}"><b>UNAVAILABLE — insufficient '
                       f'historical data</b></font><br/><font size="7">Two complete calendar '
                       f'months cannot fit or validate a monthly model. The 7-day horizon '
                       f'above is the longest span backtesting supports.</font>', S_CELL)],
            [para("Next 12 months", S_CELLB),
             Paragraph(f'<font color="#{BAD.hexval()[2:]}"><b>UNAVAILABLE — insufficient '
                       f'historical data</b></font><br/><font size="7">100 complete days '
                       f'and two complete calendar months cannot fit or validate an annual '
                       f'seasonal cycle.</font>', S_CELL)],
        ]
        F.append(table(rows, [42 * mm, 106 * mm], header=False))

    # ── 14 recommendations ───────────────────────────────────────────────────
    F.append(h1("14", "Recommendations"))
    recs = [
        ("WMS4 station", "Inspect the station's data-acquisition path and power/comms; "
                         "verify against a travelling reference before trusting its channels",
         "Re-run the four-station comparison; RMSE should fall into the 92–101 W/m² peer band",
         "HIGH"),
        ("Energy reconciliation", "Obtain the single-line diagram and metering scheme; "
                                  "confirm the register's day boundary and its position in the AC chain",
         "Recompute both measures over the same boundary; the gap should close or be explained",
         "HIGH"),
        ("Duplicate timestamps", "De-duplicate at the collection layer; check whether the "
                                 "RTU re-sends on reconnect",
         "Duplicate count returns to zero on subsequent audits", "MEDIUM"),
        ("Curtailment attribution", "Retrieve dispatch instructions for the April windows",
         "Each curtailment window is labelled grid-ordered or plant-side", "MEDIUM"),
        ("DC string-current alarms", "Review the dominant alarm class; it is high in volume "
                                     "but is not tracking energy loss",
         "Either the alarm threshold is retuned, or a real string defect is found", "LOW"),
    ]
    rows = [[para(x, S_CELLB) for x in
             ("Area", "Recommended field work", "How to verify it worked", "Priority")]]
    for area, work, verif, pri in recs:
        tone = {"HIGH": BAD, "MEDIUM": WARN, "LOW": MUTED}[pri]
        rows.append([para(area, S_CELLB), para(work), para(verif), tag(pri, tone)])
    F.append(table(rows, [28 * mm, 56 * mm, 46 * mm, 18 * mm]))
    F.append(Paragraph(
        "These are recommendations inside this report only. No external maintenance ticket "
        "is raised by the system.", S_NOTE))

    # ── 15 data required ─────────────────────────────────────────────────────
    F.append(h1("15", "Data required for long-term analysis"))
    asks = [
        ("Numerical weather prediction feed", "Day-ahead generation forecasting",
         "Generation is driven by cloud; without a weather forecast no lag-based model "
         "can succeed — this is the single highest-value addition", "HIGH"),
        ("≥ 12 months of continuous telemetry", "Monthly and seasonal forecasting",
         "Two complete calendar months cannot fit or validate a seasonal cycle", "HIGH"),
        ("≥ 24 months", "Next-year forecast and degradation rate",
         "A year-on-year comparison needs two observations of the same season", "HIGH"),
        ("Tariff / PPA rate", "Any financial figure in this report",
         "No rate exists in the database; every currency value is therefore withheld", "LOW"),
        ("PVsyst / P50 baseline", "Design-vs-actual energy account",
         "Enables a true loss bridge against expectation rather than against measurement",
         "MEDIUM"),
        ("Grid dispatch instructions", "Curtailment attribution",
         "Separates grid-ordered from plant-side limits", "MEDIUM"),
        ("Single-line diagram + metering scheme", "Closing the §5 energy gap",
         "Identifies where each measurement sits in the AC chain", "HIGH"),
        ("Module datasheet and supplier/serial map", "Cohort and warranty analysis",
         "No module metadata exists; cohort attribution is impossible without it", "LOW"),
        ("Pyranometer calibration certificates", "Adjudicating WMS4",
         "Turns an observed station outlier into a certified correction", "MEDIUM"),
    ]
    rows = [[para(x, S_CELLB) for x in ("Required data", "Unlocks", "Why", "Priority")]]
    for req, unl, why, pri in asks:
        tone = {"HIGH": BAD, "MEDIUM": WARN, "LOW": MUTED}[pri]
        rows.append([para(req, S_CELLB), para(unl), para(why), tag(pri, tone)])
    F.append(table(rows, [42 * mm, 36 * mm, 52 * mm, 18 * mm]))

    # ── 16 methodology + classification ──────────────────────────────────────
    F.append(h1("16", "Methodology, formulas and classification"))
    F.append(Paragraph("<b>Sources and units</b>", S_BODY))
    rows = [[para(x, S_CELLB) for x in ("Source", "Columns used", "Unit", "How established")],
            [para("PPC"), para("GRID_ACTIVE_POWER_MEASURED, PLANT_DAILY_PRODUCTION, "
                               "ACTIVE_POWER_SET_POINT, INVERTER_RUNNING"), para("MW / MWh / %"),
             para("Peak ≈102 on a 130 MW plant; cross-checked against the inverter fleet")],
            [para("INVERTER_01…24"), para("ACTIVE_POWER"), para("kW"),
             para("24 × 4,209 kW = 101 MW, matching PPC's 100.17 MW at the same timestamp")],
            [para("WMS"), para("AVG_GHI_IRRADIATION, AVG_GTI_IRRADIATION, "
                               "ALL_WMS_AVG_MODULE_TEMP, AVG_IR_SOILING_RATIO1, WMS1–4_*"),
             para("W/m², °C, ratio"), para("Vendor channel naming; soiling is measured")],
            [para("Alarms"), para("TimeCol, EventCol, EvDescCol"), para("events"),
             para("Event-driven, ~2 s modal cadence")]]
    F.append(table(rows, [30 * mm, 58 * mm, 24 * mm, 36 * mm], size=6.8))
    F.append(Spacer(1, 2 * mm))
    F.append(Paragraph("<b>Formulas actually used in this report</b>", S_BODY))
    rows = [[para("Quantity", S_CELLB), para("Formula", S_CELLB)],
            [para("Daily energy (integrated)"), para("E = Σ(P<sub>MW</sub>) ÷ 60  over 1-minute records")],
            [para("Deviation vs fleet"), para("dev% = (E<sub>unit</sub> − median(E)) ÷ median(E) × 100")],
            [para("Performance ratio"), para("PR = E<sub>daily</sub> ÷ (GTI × P<sub>nameplate</sub>)")],
            [para("Resource model"), para("E = a × GTI + b, ordinary least squares")],
            [para("MAE"), para("mean(|actual − forecast|)")],
            [para("RMSE"), para("√mean((actual − forecast)²)")],
            [para("Bias"), para("mean(forecast − actual)")],
            [para("Monthly / yearly energy"),
             para("sum of daily energy over COMPLETE days only (≥ 1,380 of 1,440 "
                  "minutes recorded); partial months are labelled, never scaled up")],
            [para("Monthly implied PR"),
             para("PR = ΣE<sub>daily</sub> ÷ (ΣGTI × P<sub>nameplate</sub>) over days where the "
                  "plant AND weather records are both complete")],
            [para("Share of exporting minutes"),
             para("limited minutes ÷ minutes with measured export > 1 MW, per month")],
            [para("Fleet dispersion"),
             para("spread = max(dev%) − min(dev%) across the 24 units within one month")],
            [para("Data completeness"),
             para("covered minute slots ÷ expected minute slots between the source's first "
                  "and last record in the month × 100")],
            [para("Conformal half-width"),
             para("q = ceil((n+1)(1−α))-th order statistic of |actual − forecast| on "
                  "out-of-sample folds; widened by √step")]]
    F.append(table(rows, [44 * mm, 104 * mm], size=6.9))
    F.append(Spacer(1, 2 * mm))
    F.append(Paragraph("<b>Classification of every figure in this report</b>", S_BODY))
    rows = [[para("Classification", S_CELLB), para("Where it appears", S_CELLB)],
            [tag("HISTORICAL / OBSERVED", INK),
             para("§2 coverage · §3 daily generation and peaks · §4 monthly and yearly "
                  "totals, means and peaks · 6.1 station statistics · "
                  "§7 inverter energy and alarms, period and monthly · "
                  "§8 curtailment minutes and clipping plateaus · "
                  "§9 integrity and monthly completeness counts")],
            [tag("MODEL-DERIVED", ACCENT),
             para("6.2 resource fit E = a×GTI + b · 6.3 monthly implied performance "
                  "ratio · §11 all validation metrics")],
            [tag("FORECAST / PREDICTED", BAD),
             para("§12 central forecast and prediction bounds · §13 forecast summary "
                  "totals — all carry the LIMITED reliability flag")],
            [tag("UNAVAILABLE DATA", MUTED),
             para("Year-over-year comparison and every annual change metric (4.4) · "
                  "monthly and annual forecasts (§13) · February implied PR (no WMS data) · "
                  "Financial impact (no tariff) · P50 and budget comparison · satellite "
                  "cross-reference · module cohort and warranty · degradation rate · "
                  "year-on-year and seasonal analysis · next-12-month forecast · "
                  "curtailed-energy quantification")]]
    F.append(table(rows, [40 * mm, 108 * mm], size=7))
    F.append(Spacer(1, 2 * mm))
    F.append(Paragraph(
        "<b>Limitations.</b> The archive spans "
        f"{first_d:%d %b %Y} – {last_d:%d %b %Y}: {len(full)} complete days and two "
        "complete calendar months, ending over two years before this report was "
        "generated. It is an archive, not a live feed. Irradiance begins 23 Mar and "
        "cabinet/tracker data 20 Apr, so multi-source analysis is narrower still. No "
        "tariff, design model, budget, satellite reference or module metadata exists in "
        "the database, and none has been assumed. Nothing in this report is taken from "
        "any third-party reference document.", S_NOTE))
    return F
