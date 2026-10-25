"""SARIF 2.1.0 output so results land in GitHub Code Scanning."""

from __future__ import annotations

from typing import Any

from .diff import Change, DiffResult
from .risk import Finding, RiskReport

SARIF_SCHEMA = (
    "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/"
    "sarif-schema-2.1.0.json"
)
SARIF_VERSION = "2.1.0"

_TOOL_NAME = "mcplock"
_TOOL_VERSION = "0.1.0"
_INFORMATION_URI = "https://github.com/xuange-hu/mcplock"

_RULES: dict[str, dict[str, Any]] = {
    "drift": {
        "name": "McpCapabilityDrift",
        "shortDescription": {"text": "MCP server capability drift"},
        "fullDescription": {
            "text": (
                "The declared MCP capability surface changed relative to the "
                "lockfile. Breaking changes invalidate existing agent workflows."
            )
        },
        "helpUri": _INFORMATION_URI,
    },
    "risk": {
        "name": "McpCapabilityRisk",
        "shortDescription": {"text": "Risky capability declared by an MCP server"},
        "fullDescription": {
            "text": (
                "A tool, prompt or resource declares a permission surface or "
                "contains text that steers the model."
            )
        },
        "helpUri": _INFORMATION_URI,
    },
}

_SARIF_LEVEL = {
    "breaking": "error",
    "risky": "error",
    "new": "note",
    "safe": "note",
    "critical": "error",
    "high": "error",
    "medium": "warning",
    "low": "note",
    "info": "note",
}

_SECURITY_SEVERITY = {
    "breaking": "8.0",
    "risky": "7.5",
    "new": "2.0",
    "safe": "1.0",
    "critical": "9.5",
    "high": "7.5",
    "medium": "5.0",
    "low": "3.0",
    "info": "1.0",
}


def _region_hint(subject: str) -> dict[str, Any]:
    """Map a subject path onto a logicalLocation - MCP has no file/line."""
    parts = [p for p in subject.split("/") if p]
    return {
        "logicalLocations": [
            {"name": "/".join(parts), "fullyQualifiedName": subject, "kind": "module"}
        ]
    }


def build_sarif(
    diff: DiffResult | None = None,
    risk: RiskReport | None = None,
    lockfile_path: str = "mcplock.json",
) -> dict[str, Any]:
    results: list[dict[str, Any]] = []

    if diff is not None:
        for c in diff.changes:
            results.append(_change_result(c, lockfile_path))
    if risk is not None:
        for f in risk.findings:
            results.append(_finding_result(f, lockfile_path))

    return {
        "$schema": SARIF_SCHEMA,
        "version": SARIF_VERSION,
        "runs": [
            {
                "tool": {
                    "driver": {
                        "name": _TOOL_NAME,
                        "semanticVersion": _TOOL_VERSION,
                        "version": _TOOL_VERSION,
                        "informationUri": _INFORMATION_URI,
                        "rules": [
                            dict(_RULES["drift"], id="mcplock/drift"),
                            dict(_RULES["risk"], id="mcplock/risk"),
                        ],
                    }
                },
                "results": results,
            }
        ],
    }


def _change_result(c: Change, artifact: str) -> dict[str, Any]:
    return {
        "ruleId": "mcplock/drift",
        "ruleIndex": 0,
        "level": _SARIF_LEVEL.get(c.severity, "warning"),
        "message": {"text": f"[{c.severity.upper()}] {c.kind}: {c.detail}"},
        "locations": [
            {
                "physicalLocation": {
                    "artifactLocation": {"uri": artifact},
                },
                **_region_hint(c.subject),
            }
        ],
        "properties": {
            "severity": c.severity,
            "kind": c.kind,
            "subject": c.subject,
            "security-severity": _SECURITY_SEVERITY.get(c.severity, "5.0"),
        },
    }


def _finding_result(f: Finding, artifact: str) -> dict[str, Any]:
    return {
        "ruleId": "mcplock/risk",
        "ruleIndex": 1,
        "level": _SARIF_LEVEL.get(f.severity, "warning"),
        "message": {"text": f"[{f.severity.upper()}] {f.rule}: {f.message}"},
        "locations": [
            {
                "physicalLocation": {"artifactLocation": {"uri": artifact}},
                **_region_hint(f.subject),
            }
        ],
        "properties": {
            "severity": f.severity,
            "rule": f.rule,
            "evidence": f.evidence,
            "capabilities": f.capabilities,
            "security-severity": _SECURITY_SEVERITY.get(f.severity, "5.0"),
        },
    }


def to_json(
    diff: DiffResult | None = None,
    risk: RiskReport | None = None,
    lockfile_path: str = "mcplock.json",
) -> str:
    import json

    return (
        json.dumps(
            build_sarif(diff=diff, risk=risk, lockfile_path=lockfile_path),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
