"""Terminal rendering. Zero dependencies: hand-rolled ANSI, no rich.

Colour is emitted only when stdout is a TTY or ``FORCE_COLOR`` is set, so
piping into ``less`` or a CI log stays clean.
"""

from __future__ import annotations

import os
import sys
from typing import Any

from .diff import BREAKING, NEW, RISKY, SAFE, DiffResult
from .risk import RiskReport
from .snapshot import Snapshot

_RESET = "\033[0m"
_BOLD = "\033[1m"
_DIM = "\033[2m"
_RED = "\033[31m"
_YELLOW = "\033[33m"
_GREEN = "\033[32m"
_BLUE = "\033[34m"
_CYAN = "\033[36m"

_COLOR = {
    BREAKING: _RED,
    RISKY: _YELLOW,
    NEW: _BLUE,
    SAFE: _GREEN,
    "critical": _RED,
    "high": _RED,
    "medium": _YELLOW,
    "low": _CYAN,
    "info": _DIM,
}


def _enabled() -> bool:
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    try:
        return bool(sys.stdout.isatty())
    except (AttributeError, ValueError):
        return False


class Renderer:
    def __init__(self, color: bool | None = None, width: int = 88) -> None:
        self.color = _enabled() if color is None else color
        self.width = width

    # -- primitives -----------------------------------------------------------
    def paint(self, text: str, *styles: str) -> str:
        if not self.color:
            return text
        return "".join(styles) + text + _RESET

    def rule(self, title: str = "") -> str:
        if not title:
            return "-" * self.width
        pad = self.width - len(title) - 3
        if pad < 1:
            pad = 1
        return f"-- {title} " + "-" * pad

    def head(self, text: str) -> str:
        return self.paint(text, _BOLD)

    def bold(self, text: str) -> str:
        return self.paint(text, _BOLD)

    def dim(self, text: str) -> str:
        return self.paint(text, _DIM)

    def ok(self, text: str) -> str:
        return self.paint(text, _GREEN)

    def danger(self, text: str) -> str:
        return self.paint(text, _RED)

    def warn(self, text: str) -> str:
        return self.paint(text, _YELLOW)

    # -- sections -------------------------------------------------------------
    def snapshot_summary(self, snap: Snapshot) -> str:
        lines = [
            self.head("mcplock snapshot"),
            f"  server      : {snap.server.get('name') or '(unknown)'} "
            f"{snap.server.get('version') or ''}".rstrip(),
            f"  protocol    : {snap.protocol_version or '(unspecified)'}",
            f"  tools       : {len(snap.tools)}",
            f"  prompts     : {len(snap.prompts)}",
            f"  resources   : {len(snap.resources)} "
            f"(+{len(snap.resource_templates)} templates)",
            f"  fingerprint : {self.paint(snap.fingerprint, _BOLD, _CYAN)}",
        ]
        return "\n".join(lines)

    def tool_table(self, snap: Snapshot, show_schema: bool = False) -> str:
        if not snap.tools:
            return "  (no tools declared)"
        widths = [max(len(t.name) for t in snap.tools), 8]
        lines = []
        for t in snap.tools:
            req = sorted(t.input_schema.get("required") or [])
            args = ", ".join(sorted((t.input_schema.get("properties") or {}).keys()))
            req_mark = ""
            if req:
                req_mark = " req=" + ",".join(req)
            line = (
                f"  {t.name:<{widths[0]}}  {t.schema_digest}  {args}{req_mark}"
            )
            if len(line) > self.width:
                line = line[: self.width - 1] + "…"
            lines.append(line)
            if show_schema:
                import json

                lines.append(
                    self.paint(
                        "      " + json.dumps(t.input_schema, sort_keys=True), _DIM
                    )
                )
            elif t.description:
                desc = t.description.strip().replace("\n", " ")
                if len(desc) > self.width - 8:
                    desc = desc[: self.width - 9] + "…"
                lines.append(self.paint(f"      {desc}", _DIM))
        return "\n".join(lines)

    def diff_report(self, res: DiffResult, threshold: str = SAFE) -> str:
        counts = res.counts
        out = [
            self.head("capability drift"),
            f"  baseline {res.baseline_digest[:12]}  ->  "
            f"current {res.current_digest[:12]}",
            "  "
            + "  ".join(
                f"{self.paint(f'{k}={v}', _BOLD, _COLOR.get(k, ''))}"
                for k, v in counts.items()
                if v
            )
            or "  (no changes)",
        ]
        if not res.changes:
            out.append(self.paint("  no drift detected", _GREEN))
            return "\n".join(out)
        for sev in (BREAKING, RISKY, NEW, SAFE):
            group = [c for c in res.changes if c.severity == sev]
            if not group:
                continue
            out.append(self.rule(sev.upper()))
            for c in group:
                tag = self.paint(f"[{sev[:4].upper()}]", _BOLD, _COLOR.get(sev, ""))
                out.append(f"{tag} {c.subject}")
                out.append(self.paint(f"       {c.kind}: {c.detail}", _DIM))
                if c.before is not None or c.after is not None:
                    out.append(
                        self.paint(
                            f"       - {_short(c.before)}", _DIM, _RED if sev == BREAKING else ""
                        )
                    )
                    out.append(
                        self.paint(
                            f"       + {_short(c.after)}", _DIM, _GREEN
                        )
                    )
        return "\n".join(out)

    def risk_report(self, rep: RiskReport) -> str:
        bar_len = 20
        filled = int(round(bar_len * rep.risk_score / 100))
        bar = "#" * filled + "." * (bar_len - filled)
        colour = _GREEN if rep.risk_score < 25 else (
            _YELLOW if rep.risk_score < 60 else _RED
        )
        out = [
            self.head("capability risk"),
            f"  score  {self.paint(bar, _BOLD, colour)} {rep.risk_score}/100",
            f"  surfaces: {', '.join(rep.capabilities) or '(none detected)'}",
        ]
        if not rep.findings:
            out.append(self.paint("  no risky capabilities declared", _GREEN))
            return "\n".join(out)
        for f in rep.findings:
            tag = self.paint(
                f"[{f.severity.upper()[:4]}]", _BOLD, _COLOR.get(f.severity, "")
            )
            out.append(f"{tag} {f.rule}  {f.subject}")
            out.append(self.paint(f"       {f.message}", _DIM))
            out.append(f"       evidence: {_short(f.evidence, 100)}")
        return "\n".join(out)


def _short(value: Any, n: int = 88) -> str:
    if value is None:
        return "(none)"
    if isinstance(value, (dict, list)):
        import json

        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    s = str(value).replace("\n", " ")
    if len(s) > n:
        return s[: n - 1] + "…"
    return s
