# ceo_state.py
"""
Dataclasses and state containers for the CEO's cognitive state.
Includes advanced reasoning fields, native tracking for the
domain-agnostic Company Constitution (domain_manifest.md), and
final‑product deliverable tracking.
"""

from dataclasses import dataclass, field
from typing import Any, Optional, Dict, List

@dataclass
class CEOScratchpad:
    """The CEO's internal reasoning state (persistent across turns)."""
    hypothesis: str = "Initial system assessment — no data yet."
    prediction: str = "Workers will return current codebase/environment state."
    alternative_hypotheses: List[str] = field(default_factory=list)
    evidence_gap: str = "Missing environmental context and domain rules."
    confidence: int = 100
    blockers: List[str] = field(default_factory=list)
    dead_ends: List[str] = field(default_factory=list)
    inferred_facts: List[str] = field(default_factory=list)
    information_completeness: str = "0%"
    token_exhaustion_risk: str = "LOW"

    # ── Cognitive Phase & Domain Tracking ──
    current_phase: str = "RECONNAISSANCE"
    phase_transition_logic: str = "Mission just started. Scanning environment and reading user prompt."
    framework_status: str = "PENDING"
    constitution_status: str = "LOADED"      # Tracks if domain_manifest.md exists
    active_domain: str = "UNKNOWN"           # Tracks the detected industry/project domain

    qa_gate_status: str = "PENDING"
    current_puzzle_step: str = "Step 1 — Initial reconnaissance and domain discovery."

    # ── 🆕 Final Product Definition (enforced by DEFINE_PRODUCT gate) ──
    final_product_defined: bool = False
    final_product_description: str = ""
    required_deliverable_files: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if not k.startswith("_")}

@dataclass
class SharedState:
    """Cross-worker shared state (ground truth)."""
    files_modified: Dict[str, str] = field(default_factory=dict)
    verified_facts: List[str] = field(default_factory=list)
    warm_facts: List[str] = field(default_factory=list)
    blockers: List[str] = field(default_factory=list)
    task_results: Dict[str, Any] = field(default_factory=dict)
    rework_counts: Dict[str, int] = field(default_factory=dict)  # 🆕 track rework attempts per task

    def to_dict(self) -> dict:
        return {
            "files_modified": dict(list(self.files_modified.items())[-10:]),
            "verified_facts": self.verified_facts[-5:],
            "warm_facts": self.warm_facts[-5:],
            "blockers": self.blockers[:5],
            "task_results": self.task_results,
            "rework_counts": self.rework_counts,
        }

@dataclass
class GoalStack:
    """Hierarchical goal tracking."""
    goals: List[Dict[str, str]] = field(default_factory=lambda: [
        {"goal": "Architect the Master Plan (plan.md)", "status": "active"},
        {"goal": "Execute Mission Requirements", "status": "pending"},
        {"goal": "Verify Holistic System Health", "status": "pending"},
    ])

    def update(self, new_goals: List[Dict[str, str]]) -> None:
        self.goals = new_goals

    def get_active(self) -> Optional[str]:
        for g in self.goals:
            if g.get("status") == "active":
                return g.get("goal", "")
        return None

    def to_dict(self) -> List[Dict[str, str]]:
        return self.goals
