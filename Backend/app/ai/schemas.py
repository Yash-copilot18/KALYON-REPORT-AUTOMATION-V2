"""
The contract every part of the assurance engine speaks.

Two rules are encoded here rather than left to convention:

1. A module states whether it CAN run before it runs. `AnalysisStatus` is returned
   by `required_inputs()` checks, and a BLOCKED module never executes a query - it
   returns the reason and the data it would need instead of a fabricated answer.

2. Every number that reaches a reader carries its provenance. `Derivation` records
   the table, the columns, the window, the formula and the inputs that produced a
   value, so "why did the engine reach this conclusion?" is answered from the data
   package rather than from a model.

Nothing in this module talks to an LLM, and nothing here performs analysis.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional


# ── status vocabulary ────────────────────────────────────────────────────────
class AnalysisStatus(str, Enum):
    """Whether an analysis could be performed at all."""
    AVAILABLE = "AVAILABLE"   # every required input present; result is complete
    PARTIAL   = "PARTIAL"     # ran, but over a reduced window or with a caveat
    BLOCKED   = "BLOCKED"     # a required input is missing; nothing was computed


class Verification(str, Enum):
    """Agreement between independent derivations of the same quantity."""
    CONVERGED     = "CONVERGED"       # >=2 methods agree within tolerance
    DIVERGED      = "DIVERGED"        # >=2 methods disagree - reported, not hidden
    SINGLE_SOURCE = "SINGLE_SOURCE"   # only one method is possible with our data
    NOT_VERIFIED  = "NOT_VERIFIED"    # verification not attempted for this finding


class Severity(str, Enum):
    INFO     = "INFO"
    LOW      = "LOW"
    MEDIUM   = "MEDIUM"
    HIGH     = "HIGH"
    CRITICAL = "CRITICAL"


class Classification(str, Enum):
    """Can the loss or defect be acted on?"""
    RECOVERABLE           = "RECOVERABLE"            # action plausibly recovers it
    STRUCTURAL            = "STRUCTURAL"             # inherent; not recoverable
    DATA_INTEGRITY        = "DATA_INTEGRITY"         # about the record, not the plant
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"  # observed, not adjudicated


# ── provenance ───────────────────────────────────────────────────────────────
@dataclass
class Derivation:
    """
    One traceable route to a number.

    `formula` is the human-readable expression; `code_ref` names the function that
    implements it, so a reviewer can read both. `inputs` holds the operands that
    produced `value`, which is what makes the result reproducible by hand.
    """
    method: str
    value: Optional[float]
    units: str = ""
    formula: str = ""
    code_ref: str = ""
    source_tables: List[str] = field(default_factory=list)
    source_columns: List[str] = field(default_factory=list)
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    row_count: Optional[int] = None
    inputs: Dict[str, Any] = field(default_factory=dict)
    notes: str = ""


@dataclass
class Evidence:
    """A single observation for or against a statement."""
    statement: str
    supports: bool                      # False = this evidence argues against
    derivation: Optional[Derivation] = None
    detail: str = ""


@dataclass
class ConfidenceBreakdown:
    """
    A transparent score. The engine computes `score` from `factors`; the factors are
    kept so a reader can see WHY it is what it is, and so the model can never choose
    a confidence number of its own.
    """
    score: float                        # 0.0 - 1.0
    factors: Dict[str, float] = field(default_factory=dict)
    rationale: str = ""

    @property
    def percent(self) -> int:
        return int(round(self.score * 100))


# ── findings ─────────────────────────────────────────────────────────────────
@dataclass
class Finding:
    """
    Something the engine observed and is prepared to defend.

    A finding is never a bare assertion: it carries both sides of the evidence, the
    derivations behind its numbers, how confident the engine is and why, and whether
    an independent method agreed.
    """
    finding_id: str
    title: str
    summary: str
    analysis: str                                   # which module produced it
    severity: Severity = Severity.INFO
    classification: Classification = Classification.INSUFFICIENT_EVIDENCE
    verification: Verification = Verification.NOT_VERIFIED
    confidence: Optional[ConfidenceBreakdown] = None
    impact_value: Optional[float] = None
    impact_units: str = ""
    impact_note: str = ""
    derivations: List[Derivation] = field(default_factory=list)
    supporting_evidence: List[Evidence] = field(default_factory=list)
    contradicting_evidence: List[Evidence] = field(default_factory=list)
    source_tables: List[str] = field(default_factory=list)
    source_columns: List[str] = field(default_factory=list)
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    recommended_action: str = ""
    metrics: Dict[str, Any] = field(default_factory=dict)


@dataclass
class WorkOrder:
    """
    A recommendation for field work. Advisory only - the engine never raises a ticket
    in an external system.
    """
    work_order_id: str
    finding_id: str
    title: str
    field_work: str
    verification_method: str            # how to prove the work fixed it
    priority: Severity = Severity.MEDIUM
    expected_outcome: str = ""


@dataclass
class DataAsk:
    """
    What is needed to turn a BLOCKED or PARTIAL analysis into a finished one. This is
    a first-class output: an honest gap is more useful than an invented answer.
    """
    ask_id: str
    analysis: str
    required_data: List[str]
    unlocks: str
    reason: str
    effort: str = "unknown"             # trivial | low | medium | high


# ── module result ────────────────────────────────────────────────────────────
@dataclass
class ModuleResult:
    """What one analysis module returns to the orchestrator."""
    analysis: str
    status: AnalysisStatus
    reason: str = ""
    required_data: List[str] = field(default_factory=list)
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    metrics: Dict[str, Any] = field(default_factory=dict)
    findings: List[Finding] = field(default_factory=list)
    derivations: List[Derivation] = field(default_factory=list)
    data_asks: List[DataAsk] = field(default_factory=list)
    duration_ms: Optional[int] = None
    error: Optional[str] = None

    @classmethod
    def blocked(cls, analysis: str, reason: str, required: List[str],
                unlocks: str = "", effort: str = "unknown") -> "ModuleResult":
        """A module that cannot run says so, and says what it would need."""
        return cls(
            analysis=analysis, status=AnalysisStatus.BLOCKED, reason=reason,
            required_data=list(required),
            data_asks=[DataAsk(ask_id=f"ask_{analysis}", analysis=analysis,
                               required_data=list(required),
                               unlocks=unlocks or f"Enables {analysis}",
                               reason=reason, effort=effort)],
        )


@dataclass
class AnalysisWindow:
    """The period the report covers, and what the caller asked for."""
    requested_start: str
    requested_end: str
    effective_start: Optional[str] = None     # after clamping to available data
    effective_end: Optional[str] = None
    clamped: bool = False
    note: str = ""


@dataclass
class EvidencePackage:
    """
    The complete deterministic output. This - and only this - is what the narration
    stage is given, which is what keeps the model from inventing numbers.
    """
    report_id: str
    generated_at: str
    analysis_window: AnalysisWindow
    source_inventory: Dict[str, Any] = field(default_factory=dict)
    module_results: List[ModuleResult] = field(default_factory=list)
    findings: List[Finding] = field(default_factory=list)
    work_orders: List[WorkOrder] = field(default_factory=list)
    data_asks: List[DataAsk] = field(default_factory=list)
    limitations: List[str] = field(default_factory=list)
    narrative: Optional[Dict[str, Any]] = None      # filled by the LLM stage, later

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def utcstamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
