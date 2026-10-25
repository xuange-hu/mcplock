"""Capability-surface risk scan.

Two questions, answered from the declared surface alone (no tool is ever
called):

1. *What could this server do to me if the model decides to use it?*
   A tool named ``run_command`` with a ``command`` argument is arbitrary
   execution, whatever its description says. mcplock derives the permission
   surface from names, argument names and schema shape.

2. *Is the declared text trying to steer the model?*
   Prompt-injection / tool-poisoning signals in descriptions: hidden
   instructions, requests to read credential files, "before using this tool
   first ...", exfiltration wording.

Everything is deterministic and offline - the same input always yields the
same findings, which is what makes it usable as a CI gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .snapshot import Snapshot, Tool

CRITICAL = "critical"
HIGH = "high"
MEDIUM = "medium"
LOW = "low"
INFO = "info"

SEVERITY_ORDER = (CRITICAL, HIGH, MEDIUM, LOW, INFO)
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITY_ORDER)}

# capability -> (severity, regex over the *signal text*)
# Signal text = tool name + argument names + description, lowercased.
CAPABILITY_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("process-exec", CRITICAL, re.compile(
        r"\b(exec|execute|spawn|popen|subprocess|system\(|shell|cmd|command|"
        r"run_?command|terminal|bash|sh -c|powershell)\b")),
    ("code-eval", CRITICAL, re.compile(
        r"\b(eval|exec_?code|run_?code|python_?exec|new function|"
        r"interpret(er)?|sandbox_?exec)\b")),
    ("file-write", HIGH, re.compile(
        r"\b(write|overwrite|create_?file|save_?file|append|mkdir|rm|delete|"
        r"remove|unlink|truncate|patch|apply_?diff|edit_?file)\b")),
    ("file-read", LOW, re.compile(
        r"\b(read|cat|open|load|fetch_?file|get_?file|list_?dir|glob|grep|tail)\b")),
    ("network-egress", HIGH, re.compile(
        r"\b(http|https|curl|wget|request|fetch|post_?to|webhook|download|"
        r"upload|socket|dns|api_?call|send_?to)\b")),
    ("credential-access", CRITICAL, re.compile(
        r"(\.ssh|\.aws|id_rsa|\.env|credentials|secrets?/|token file|"
        r"password file|keychain|\.npmrc|\.netrc|kubeconfig|service.?account)")),
    ("database", HIGH, re.compile(
        r"\b(sql|query_?db|database|sqlite|postgres|mysql|mongo|insert into|"
        r"drop table|alter table)\b")),
    ("env-access", MEDIUM, re.compile(
        r"\b(env(ironment)?[ _-]?var\w*|getenv|process\.env|os\.environ|dotenv)")),
    ("git-write", MEDIUM, re.compile(
        r"\b(git (push|commit|checkout|reset|clean)|force push|"
        r"git_?(push|commit))\b")),
    ("privilege", HIGH, re.compile(
        r"\b(sudo|chmod|chown|setuid|escalate|uac|runas|admin(istrator)?)\b")),
    ("agent-control", HIGH, re.compile(
        r"\b(spawn_?agent|sub_?agent|create_?agent|delegate|"
        r"prompt_?injection|override_?instructions)\b")),
]

# Tool name alone is a strong signal even if the description is innocuous.
NAME_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("process-exec", CRITICAL, re.compile(
        r"(^|_)(run|exec|execute|shell|bash|powershell|terminal|cmd)(_|$)")),
    ("code-eval", CRITICAL, re.compile(r"(^|_)(eval|exec_code|run_code)(_|$)")),
    ("file-write", HIGH, re.compile(
        r"(^|_)(write|create|delete|remove|patch|edit|move|rename)(_|$)")),
    ("network-egress", HIGH, re.compile(
        r"(^|_)(fetch|request|post|send|upload|download|curl|http)(_|$)")),
]

INJECTION_PATTERNS: list[tuple[str, str, re.Pattern[str]]] = [
    ("hidden-instruction", CRITICAL, re.compile(
        r"(ignore (all )?(previous|prior|above)|disregard (the |all )?"
        r"(previous|prior|instructions)|you must|system override|"
        r"<\|?im_start\|?>|\[\[system\]\])", re.I)),
    ("credential-harvest", CRITICAL, re.compile(
        r"(reads? (the )?(file |\~/.?ssh|id_rsa|\.env)|cat ~/.?ssh|"
        r"contents of .{0,40}(\.env|id_rsa|credentials)|"
        r"include (your |the )?(api[-_ ]?key|token|password))", re.I)),
    ("exfiltration", CRITICAL, re.compile(
        r"(send|post|upload|transmit|forward) .{0,60}(to (https?://|the server|"
        r"this (url|endpoint))|along with|together with)", re.I)),
    ("precondition-steering", HIGH, re.compile(
        r"(before (using|calling|running) this (tool|function)|"
        r"first,? (you must|read|run|call)|"
        r"always (call|run|read) .{0,40}(before|first)|"
        r"do not tell (the )?user)", re.I)),
    ("concealment", HIGH, re.compile(
        r"(do not (mention|disclose|reveal|tell)|"
        r"keep this (hidden|secret)|silently|without (telling|informing))", re.I)),
    ("description-instructs-model", MEDIUM, re.compile(
        r"^\s*(you are|act as|your (task|job) is|as an? )", re.I)),
]

_WEIGHT = {CRITICAL: 40, HIGH: 25, MEDIUM: 12, LOW: 5, INFO: 1}


@dataclass
class Finding:
    rule: str
    severity: str
    subject: str
    evidence: str
    message: str
    capabilities: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "subject": self.subject,
            "evidence": self.evidence,
            "message": self.message,
            "capabilities": self.capabilities,
        }


@dataclass
class RiskReport:
    findings: list[Finding] = field(default_factory=list)
    capabilities: list[str] = field(default_factory=list)
    risk_score: int = 0
    tool_count: int = 0

    @property
    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(SEVERITY_ORDER, 0)
        for f in self.findings:
            out[f.severity] += 1
        return out

    @property
    def max_severity(self) -> str | None:
        if not self.findings:
            return None
        return min(self.findings, key=lambda f: SEVERITY_RANK[f.severity]).severity

    def filtered(self, threshold: str) -> list[Finding]:
        limit = SEVERITY_RANK[threshold]
        return [f for f in self.findings if SEVERITY_RANK[f.severity] <= limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "risk_score": self.risk_score,
            "capabilities": self.capabilities,
            "counts": self.counts,
            "max_severity": self.max_severity,
            "findings": [f.to_dict() for f in self.findings],
        }


def scan(snapshot: Snapshot, fail_on_capability: list[str] | None = None) -> RiskReport:
    """Scan a snapshot's declared surface for permission and steering risk."""
    report = RiskReport(tool_count=len(snapshot.tools))
    caps: set[str] = set()

    for tool in snapshot.tools:
        caps.update(_scan_tool(report, tool))

    # Server-wide signals: capability flags and instruction text.
    server_caps = snapshot.capabilities or {}
    if "sampling" in server_caps:
        report.findings.append(
            Finding(
                rule="MCP-SAMPLING",
                severity=MEDIUM,
                subject="capabilities/sampling",
                evidence="server declares sampling support",
                message=(
                    "server can ask the client to run model completions; "
                    "it can steer generation and spend your tokens"
                ),
                capabilities=["sampling"],
            )
        )
        caps.add("sampling")
    if "completions" in server_caps:
        caps.add("completions")

    if fail_on_capability:
        for wanted in fail_on_capability:
            if wanted in caps:
                report.findings.append(
                    Finding(
                        rule="MCP-POLICY",
                        severity=HIGH,
                        subject=f"capabilities/{wanted}",
                        evidence=f"--deny-capability {wanted}",
                        message=f"server exposes denied capability '{wanted}'",
                        capabilities=[wanted],
                    )
                )

    report.capabilities = sorted(caps)
    report.findings.sort(key=lambda f: (SEVERITY_RANK[f.severity], f.subject, f.rule))
    # Score is capped at 100; each additional finding of the same severity
    # adds less than the first one (sqrt-style decay) so a noisy server with
    # 30 LOW findings does not outrank one CRITICAL exec tool.
    raw = 0.0
    per_sev: dict[str, int] = {}
    for f in report.findings:
        n = per_sev.get(f.severity, 0)
        raw += _WEIGHT[f.severity] * (1.0 / (1.0 + 0.6 * n))
        per_sev[f.severity] = n + 1
    report.risk_score = int(min(100, round(raw)))
    return report


def _scan_tool(report: RiskReport, tool: Tool) -> set[str]:
    name = tool.name or ""
    desc = tool.description or ""
    argnames = sorted((tool.input_schema or {}).get("properties", {}).keys())
    signal = " ".join([name, " ".join(argnames), desc]).lower()

    caps: set[str] = set()
    for cap, sev, pat in CAPABILITY_PATTERNS:
        m = pat.search(signal)
        if not m:
            continue
        caps.add(cap)
        report.findings.append(
            Finding(
                rule=f"MCP-CAP-{cap.upper()}",
                severity=sev,
                subject=f"tools/{name}",
                evidence=_snippet(desc or name, m.group(0)),
                message=f"declares {cap.replace('-', ' ')} capability",
                capabilities=[cap],
            )
        )

    for cap, sev, pat in NAME_PATTERNS:
        if cap in caps:
            continue
        if pat.search(name.lower()):
            caps.add(cap)
            report.findings.append(
                Finding(
                    rule=f"MCP-CAP-{cap.upper()}",
                    severity=sev,
                    subject=f"tools/{name}",
                    evidence=f"tool name matches {cap}",
                    message=f"tool name alone implies {cap.replace('-', ' ')}",
                    capabilities=[cap],
                )
            )

    # Unbounded free-text arguments feeding a dangerous capability.
    dangerous = caps & {"process-exec", "code-eval", "network-egress", "file-write"}
    if dangerous:
        for arg in argnames:
            sub = (tool.input_schema.get("properties") or {}).get(arg) or {}
            if not isinstance(sub, dict):
                continue
            if sub.get("type") == "string" and not sub.get("enum") and re.search(
                r"(cmd|command|code|script|url|path|query|payload|expr)", arg
            ):
                    report.findings.append(
                        Finding(
                            rule="MCP-ARG-UNBOUNDED",
                            severity=HIGH,
                            subject=f"tools/{name}/{arg}",
                            evidence=f"unconstrained string argument '{arg}'",
                            message=(
                                f"'{arg}' is a free-form string feeding "
                                f"{sorted(dangerous)}; no enum or pattern restricts it"
                            ),
                            capabilities=sorted(dangerous),
                        )
                    )

    for rule, sev, pat in INJECTION_PATTERNS:
        m = pat.search(desc)
        if not m:
            continue
        report.findings.append(
            Finding(
                rule=f"MCP-INJ-{rule.upper()}",
                severity=sev,
                subject=f"tools/{name}",
                evidence=_snippet(desc, m.group(0)),
                message=_INJ_MESSAGES[rule],
                capabilities=sorted(caps),
            )
        )

    return caps


_INJ_MESSAGES = {
    "hidden-instruction": (
        "description contains instruction-override text; classic tool-poisoning "
        "payload - it is shown to the model, not to you"
    ),
    "credential-harvest": (
        "description asks for credential file contents to be passed as an argument"
    ),
    "exfiltration": (
        "description instructs data to be sent to an external endpoint"
    ),
    "precondition-steering": (
        "description imposes a precondition on the model ('first read X', "
        "'always call Y before') - steering behaviour outside your prompt"
    ),
    "concealment": (
        "description tells the model to hide an action from the user"
    ),
    "description-instructs-model": (
        "description is written as a directive to the model rather than a "
        "description of what the tool does"
    ),
}


def _snippet(text: str, needle: str, width: int = 90) -> str:
    """Return the matched region with enough context to be checkable."""
    idx = text.lower().find(needle.lower())
    if idx < 0:
        return text[:width]
    start = max(0, idx - 20)
    end = min(len(text), idx + len(needle) + width - 20)
    frag = text[start:end].replace("\n", " ")
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{frag}{suffix}"
