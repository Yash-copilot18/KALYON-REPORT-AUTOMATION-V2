"""
Runs the analysis modules and assembles the evidence package.

The orchestrator owns three responsibilities and nothing else:

* **Gate before compute.** It builds the source inventory first, then asks each
  module whether it can run. A module that reports BLOCKED never executes a query;
  its data ask is carried into the package instead of a fabricated result.
* **Clamp honestly.** The requested window is intersected with what the database
  actually holds. If the caller asks for a period the archive does not cover, that
  is stated, not silently returned as "no activity".
* **State the limits.** Every claim the archive cannot support - annual figures,
  year-on-year, degradation, P50, budget, tariff, satellite, supplier cohort - is
  listed as a limitation on the package itself, so a reader cannot mistake absence
  for a zero.

It performs no analysis of its own and contains no LLM code.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from sqlalchemy.orm import Session

from app.ai import evidence as ev
from app.ai.schemas import (AnalysisStatus, AnalysisWindow, DataAsk, EvidencePackage,
                            Finding, ModuleResult, utcstamp)
from app.analytics import data_quality

logger = logging.getLogger(__name__)

# Claims the 2024 archive cannot support. Surfaced on every package so the report can
# show them as "Unavailable with current data" rather than omitting them silently.
STRUCTURAL_LIMITATIONS: List[Dict[str, Any]] = [
    {"topic": "P50 / design comparison", "requires": ["PVsyst or P50 monthly baseline"],
     "unlocks": "Design-vs-actual energy account and loss bridge vs P50"},
    {"topic": "Budget variance", "requires": ["Owner's operating budget"],
     "unlocks": "Actual-vs-budget reporting"},
    {"topic": "Financial impact", "requires": ["Tariff / PPA rate"],
     "unlocks": "Currency values for every energy figure in this report"},
    {"topic": "Satellite cross-reference", "requires": ["Satellite irradiance feed"],
     "unlocks": "Independent validation of ground irradiance and timestamp convention"},
    {"topic": "Module supplier cohort", "requires": ["Module supplier / serial / Wp metadata"],
     "unlocks": "Cohort attribution and warranty position"},
    {"topic": "Degradation rate", "requires": ["At least one further year of telemetry"],
     "unlocks": "Annual degradation (%/yr)"},
    {"topic": "Year-on-year and seasonal analysis",
     "requires": ["Telemetry spanning multiple years and all seasons"],
     "unlocks": "Seasonal deviation matrix and year-on-year comparison"},
]


def _limitation_asks() -> List[DataAsk]:
    return [
        DataAsk(ask_id=f"ask_limit_{i}", analysis=item["topic"],
                required_data=item["requires"], unlocks=item["unlocks"],
                reason="Not present in the current archive.", effort="unknown")
        for i, item in enumerate(STRUCTURAL_LIMITATIONS)
    ]


def _default_integrity_tables(inventory: Dict[str, Any]) -> List[str]:
    """
    The sources worth auditing: the whole inverter fleet plus the plant-level and
    weather sources. Taken from the inventory, so a plant with a different number of
    inverters audits correctly without a code change.
    """
    groups = inventory.get("groups", {})
    tables: List[str] = []
    for key in ("inverters", "ppc", "weather", "generation", "thermal", "tracker_status"):
        for entry in groups.get(key, {}).get("tables", []):
            if entry.get("exists") and entry.get("rows"):
                tables.append(entry["table"])
    return tables


def run_report(db: Session, requested_start: str, requested_end: str,
               progress: Optional[Callable[[int, str], None]] = None,
               modules: Optional[List[str]] = None) -> EvidencePackage:
    """
    Produce a complete evidence package for [requested_start, requested_end].

    `progress(pct, message)` mirrors the stages the UI shows. The function is
    deliberately synchronous and side-effect free: the caller runs it on a worker
    thread with its own session.
    """
    def tick(pct: int, msg: str) -> None:
        if progress:
            try:
                progress(pct, msg)
            except Exception:                         # noqa: BLE001
                pass                                  # reporting must never fail a run

    report_id = uuid.uuid4().hex
    started = datetime.now()

    tick(3, "Loading data inventory…")
    inventory = ev.build_inventory(db)

    avail_start = inventory.get("plant_earliest")
    avail_end = inventory.get("plant_latest")
    eff_start, eff_end, clamped = ev.clamp_window(
        requested_start, requested_end, avail_start, avail_end)

    window = AnalysisWindow(
        requested_start=requested_start, requested_end=requested_end,
        effective_start=eff_start, effective_end=eff_end, clamped=clamped,
        note=("" if not clamped else
              f"Clamped to the archive, which holds {avail_start} to {avail_end}."),
    )

    package = EvidencePackage(
        report_id=report_id, generated_at=utcstamp(), analysis_window=window,
        source_inventory=inventory,
        data_asks=_limitation_asks(),
        limitations=[f"{i['topic']}: requires {', '.join(i['requires'])}"
                     for i in STRUCTURAL_LIMITATIONS],
    )

    if not eff_start or not eff_end:
        # The request lies outside the archive. Returning an empty analysis would read
        # as "nothing happened"; refusing is the honest answer.
        package.module_results.append(ModuleResult.blocked(
            "analysis_window",
            f"The requested window has no overlap with the archive "
            f"({avail_start} to {avail_end}).",
            ["Telemetry covering the requested period"],
            unlocks="Any analysis over the requested window"))
        tick(100, "No data in the requested window.")
        return package

    wanted = set(modules or ["data_integrity"])
    results: List[ModuleResult] = []

    # ── Phase 3: data integrity ──────────────────────────────────────────────
    if "data_integrity" in wanted:
        tick(10, "Validating data integrity…")
        tables = _default_integrity_tables(inventory)
        results.append(data_quality.run(
            db, tables, eff_start, eff_end, inventory=inventory,
            progress=lambda p, m: tick(10 + int(p * 0.55), m)))

    tick(70, "Collecting findings…")
    package.module_results = results
    for r in results:
        package.findings.extend(r.findings)
        package.data_asks.extend(r.data_asks)

    package.findings.sort(key=lambda f: _severity_rank(f), reverse=True)

    tick(100, "Deterministic analysis complete.")
    logger.info("AI report %s built in %.1fs | window %s..%s | %d finding(s)",
                report_id, (datetime.now() - started).total_seconds(),
                eff_start, eff_end, len(package.findings))
    return package


_SEVERITY_ORDER = {"CRITICAL": 4, "HIGH": 3, "MEDIUM": 2, "LOW": 1, "INFO": 0}


def _severity_rank(f: Finding) -> int:
    return _SEVERITY_ORDER.get(getattr(f.severity, "value", str(f.severity)), 0)


def preflight(db: Session) -> Dict[str, Any]:
    """
    What the engine could analyse right now, without running anything.

    The page calls this on load so the operator sees the real coverage and the known
    blocked analyses before choosing a window.
    """
    inventory = ev.build_inventory(db)
    groups = inventory.get("groups", {})
    return {
        "plant_earliest": inventory.get("plant_earliest"),
        "plant_latest": inventory.get("plant_latest"),
        "all_source_overlap_start": inventory.get("all_source_overlap_start"),
        "all_source_overlap_end": inventory.get("all_source_overlap_end"),
        "groups": {
            k: {"present": v["present_count"], "expected": v["expected_count"],
                "earliest": v["earliest"], "latest": v["latest"],
                "common_start": v["common_start"], "common_end": v["common_end"],
                "cadence_seconds": v["cadence_seconds"], "rows": v["total_rows"]}
            for k, v in groups.items()
        },
        "available_modules": [
            {"key": "data_integrity", "title": "Data integrity / trust",
             "status": AnalysisStatus.AVAILABLE.value},
        ],
        "blocked_analyses": [
            {"analysis": i["topic"], "status": AnalysisStatus.BLOCKED.value,
             "reason": "Not present in the current archive.",
             "required_data": i["requires"]}
            for i in STRUCTURAL_LIMITATIONS
        ],
    }
