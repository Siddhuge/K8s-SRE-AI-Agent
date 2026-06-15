"""Typed result model for RCA, matching the spec's example output shape."""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Severity(str, Enum):
    critical = "Critical"
    high = "High"
    medium = "Medium"
    low = "Low"
    info = "Info"


class Evidence(BaseModel):
    """One correlated signal supporting (or weighing against) a hypothesis."""

    source: str               # events | logs | metrics | gitops | cicd | k8s | kb
    summary: str
    detail: str = ""
    weight: float = 0.0        # contribution to the confidence score, 0..1
    timestamp: str = ""        # ISO8601 when known — drives the timeline


class Hypothesis(BaseModel):
    issue: str                 # e.g. "CrashLoopBackOff"
    root_cause: str
    confidence: int = 0        # 0..100
    evidence: list[Evidence] = Field(default_factory=list)
    suggested_fix: str = ""
    rollback_required: bool = False
    rollback_target: str = ""
    runbook: str = ""          # from RAG, if matched


class RCAReport(BaseModel):
    severity: Severity
    cluster: str
    namespace: str
    subject: str               # the pod/deployment/node under analysis
    issue: str
    root_cause: str
    confidence: int
    evidence: list[Evidence]
    suggested_fix: str
    rollback_required: bool
    rollback_target: str = ""
    timeline: list[str] = Field(default_factory=list)
    incident_summary: str = ""
    alternative_hypotheses: list[Hypothesis] = Field(default_factory=list)

    def to_markdown(self) -> str:
        ev = "\n".join(f"- {e.summary}" for e in self.evidence)
        rb = "Yes — roll back to " + self.rollback_target if self.rollback_required else "No"
        return (
            f"**Severity:** {self.severity.value}\n\n"
            f"**Cluster / Namespace:** {self.cluster} / {self.namespace}\n\n"
            f"**Subject:** {self.subject}\n\n"
            f"**Issue:** {self.issue}\n\n"
            f"**Root Cause:** {self.root_cause}\n\n"
            f"**Evidence:**\n{ev}\n\n"
            f"**Confidence:** {self.confidence}%\n\n"
            f"**Suggested Fix:** {self.suggested_fix}\n\n"
            f"**Rollback Required:** {rb}\n"
        )
