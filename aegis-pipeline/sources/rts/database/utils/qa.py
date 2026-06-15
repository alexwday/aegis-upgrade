"""Structured QA findings and report helpers."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any

QA_SEVERITIES = ("low", "medium", "high")


@dataclass(frozen=True)
class QAFinding:
    """One deterministic extraction QA finding."""

    code: str
    severity: str
    message: str
    page_number: int | None = None
    sheet_number: int | None = None
    region_id: str = ""
    artifact_path: str = ""

    def to_record(self) -> dict[str, Any]:
        """Return a JSON-serializable finding record."""
        record: dict[str, Any] = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.page_number is not None:
            record["page_number"] = self.page_number
        if self.sheet_number is not None:
            record["sheet_number"] = self.sheet_number
        if self.region_id:
            record["region_id"] = self.region_id
        if self.artifact_path:
            record["artifact_path"] = self.artifact_path
        return record


class QAGateError(RuntimeError):
    """Raised when extraction QA finds a high-severity issue."""


def qa_status(findings: list[QAFinding] | list[dict[str, Any]]) -> str:
    """Return document QA status from finding severities."""
    severities = [_severity(finding) for finding in findings]
    if "high" in severities:
        return "failed"
    if severities:
        return "warning"
    return "passed"


def qa_counts(findings: list[QAFinding] | list[dict[str, Any]]) -> dict[str, int]:
    """Return severity counts with stable low/medium/high keys."""
    counts = Counter(_severity(finding) for finding in findings)
    return {severity: int(counts.get(severity, 0)) for severity in QA_SEVERITIES}


def finding_records(findings: list[QAFinding]) -> list[dict[str, Any]]:
    """Convert findings to JSON-serializable records."""
    return [finding.to_record() for finding in findings]


def _severity(finding: QAFinding | dict[str, Any]) -> str:
    """Extract a normalized severity from a finding object or record."""
    value = finding.severity if isinstance(finding, QAFinding) else finding["severity"]
    severity = str(value)
    if severity not in QA_SEVERITIES:
        raise ValueError(f"Unknown QA severity: {severity}")
    return severity
