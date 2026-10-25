"""Drift classification.

A raw JSON diff of two snapshots tells you *that* something changed. It does
not tell you whether agent prompts will break at 3am. mcplock classifies every
change into one of four severities:

``BREAKING``
    Previously valid calls stop working: a tool disappeared, a required
    argument was added or widened, a property type narrowed, an enum lost a
    member, a prompt argument became required.

``RISKY``
    Calls still work but the *semantics* the model relies on moved: tool or
    prompt description changed, a resource URI changed, the server dropped a
    declared capability. This is the rug-pull / tool-poisoning class.

``SAFE``
    Strictly widening: optional argument added, required argument dropped,
    enum gained a member, type widened. Old callers keep working.

``NEW``
    Addition: new tool / prompt / resource / template.

Unknown-change fallback is BREAKING: if mcplock cannot prove a change is
harmless, CI should hear about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .snapshot import Resource, Snapshot, Tool

BREAKING = "breaking"
RISKY = "risky"
SAFE = "safe"
NEW = "new"

SEVERITIES = (BREAKING, RISKY, SAFE, NEW)

# Display order (most severe first) vs. comparison rank - keep them separate,
# the direction trap already bit a sibling project.
SEVERITY_ORDER = SEVERITIES
SEVERITY_RANK = {BREAKING: 0, RISKY: 1, NEW: 2, SAFE: 3}


@dataclass
class Change:
    severity: str
    kind: str  # tool_removed, tool_desc_changed, arg_added_required, ...
    subject: str  # "tools/run_command" or "prompts/review"
    detail: str
    before: Any = None
    after: Any = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "severity": self.severity,
            "kind": self.kind,
            "subject": self.subject,
            "detail": self.detail,
        }
        if self.before is not None:
            d["before"] = self.before
        if self.after is not None:
            d["after"] = self.after
        return d


@dataclass
class DiffResult:
    changes: list[Change] = field(default_factory=list)
    baseline_digest: str = ""
    current_digest: str = ""

    @property
    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(SEVERITIES, 0)
        for c in self.changes:
            out[c.severity] = out.get(c.severity, 0) + 1
        return out

    @property
    def max_severity(self) -> str | None:
        if not self.changes:
            return None
        return min(self.changes, key=lambda c: SEVERITY_RANK[c.severity]).severity

    def at_least(self, threshold: str) -> bool:
        """True if any change is at or above ``threshold`` in severity."""
        if not self.changes:
            return False
        limit = SEVERITY_RANK[threshold]
        return any(SEVERITY_RANK[c.severity] <= limit for c in self.changes)

    def filtered(self, threshold: str) -> list[Change]:
        limit = SEVERITY_RANK[threshold]
        return [c for c in self.changes if SEVERITY_RANK[c.severity] <= limit]

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline_digest": self.baseline_digest,
            "current_digest": self.current_digest,
            "counts": self.counts,
            "max_severity": self.max_severity,
            "changes": [c.to_dict() for c in self.changes],
        }


# JSON Schema types are disjoint, with exactly one containment relation:
# every integer is also a number. So "widening" an *input* type is only safe
# for integer -> number; every other change means previously accepted
# arguments stop validating (string -> array is BREAKING, not a widening).
_COMPATIBLE_WIDENINGS = {("integer", "number")}


def _type_allows(before: str | None, after: str | None) -> bool:
    """True if arguments valid under ``before`` are still valid under ``after``.

    Input schemas constrain what the *caller* sends. A change is safe only
    when the accepted set strictly grows - which in practice means dropping a
    type constraint, or integer -> number.
    """
    if before == after:
        return True
    if after is None:
        # Constraint removed: strictly more inputs accepted.
        return True
    if before is None:
        # Constraint added where there was none: strictly fewer.
        return False
    return (before, after) in _COMPATIBLE_WIDENINGS


def diff_snapshots(baseline: Snapshot, current: Snapshot) -> DiffResult:
    res = DiffResult(
        baseline_digest=baseline.surface_digest,
        current_digest=current.surface_digest,
    )
    _diff_protocol(res, baseline, current)
    _diff_capabilities(res, baseline, current)
    _diff_tools(res, baseline, current)
    _diff_prompts(res, baseline, current)
    _diff_resources(res, baseline, current, "resources", baseline.resources, current.resources)
    _diff_resources(
        res,
        baseline,
        current,
        "resource_templates",
        baseline.resource_templates,
        current.resource_templates,
    )
    # Deterministic output: severity then subject then kind.
    res.changes.sort(
        key=lambda c: (SEVERITY_RANK[c.severity], c.subject, c.kind, c.detail)
    )
    return res


def _diff_protocol(res: DiffResult, base: Snapshot, cur: Snapshot) -> None:
    if (
        base.protocol_version
        and cur.protocol_version
        and base.protocol_version != cur.protocol_version
    ):
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind="protocol_version_changed",
                    subject="protocol",
                    detail=(
                        f"protocolVersion {base.protocol_version} -> {cur.protocol_version}"
                    ),
                    before=base.protocol_version,
                    after=cur.protocol_version,
                )
            )
    bname = base.server.get("name")
    cname = cur.server.get("name")
    if bname and cname and bname != cname:
        res.changes.append(
            Change(
                severity=RISKY,
                kind="server_name_changed",
                subject="server",
                detail=f"serverInfo.name {bname} -> {cname}",
                before=bname,
                after=cname,
            )
        )


def _diff_capabilities(res: DiffResult, base: Snapshot, cur: Snapshot) -> None:
    for key in sorted(set(base.capabilities) | set(cur.capabilities)):
        b = base.capabilities.get(key)
        c = cur.capabilities.get(key)
        if b == c:
            continue
        if key not in cur.capabilities:
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind="capability_dropped",
                    subject=f"capabilities/{key}",
                    detail=f"server no longer advertises capability '{key}'",
                    before=b,
                    after=None,
                )
            )
        elif key not in base.capabilities:
            res.changes.append(
                Change(
                    severity=NEW,
                    kind="capability_added",
                    subject=f"capabilities/{key}",
                    detail=f"server now advertises capability '{key}'",
                    before=None,
                    after=c,
                )
            )
        else:
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind="capability_changed",
                    subject=f"capabilities/{key}",
                    detail=f"capability '{key}' changed",
                    before=b,
                    after=c,
                )
            )


def _diff_tools(res: DiffResult, base: Snapshot, cur: Snapshot) -> None:
    btools = {t.name: t for t in base.tools}
    ctools = {t.name: t for t in cur.tools}

    for name in sorted(set(btools) - set(ctools)):
        res.changes.append(
            Change(
                severity=BREAKING,
                kind="tool_removed",
                subject=f"tools/{name}",
                detail=f"tool '{name}' is gone; any prompt or workflow calling it breaks",
                before=btools[name].to_dict(),
                after=None,
            )
        )
    for name in sorted(set(ctools) - set(btools)):
        res.changes.append(
            Change(
                severity=NEW,
                kind="tool_added",
                subject=f"tools/{name}",
                detail=f"new tool '{name}'",
                before=None,
                after=ctools[name].to_dict(),
            )
        )
    for name in sorted(set(btools) & set(ctools)):
        _diff_tool(res, btools[name], ctools[name])


def _diff_tool(res: DiffResult, b: Tool, c: Tool) -> None:
    subj = f"tools/{b.name}"

    if b.description != c.description:
        res.changes.append(
            Change(
                severity=RISKY,
                kind="tool_description_changed",
                subject=subj,
                detail=(
                    "description changed - this is the text the model reads to "
                    "decide when to call the tool; review for rug-pull wording"
                ),
                before=b.description,
                after=c.description,
            )
        )
    if b.title != c.title:
        res.changes.append(
            Change(
                severity=SAFE,
                kind="tool_title_changed",
                subject=subj,
                detail=f"title {b.title!r} -> {c.title!r}",
                before=b.title,
                after=c.title,
            )
        )
    if b.annotations != c.annotations:
        res.changes.append(
            Change(
                severity=RISKY,
                kind="tool_annotations_changed",
                subject=subj,
                detail="annotations changed (readOnlyHint/destructiveHint etc.)",
                before=b.annotations,
                after=c.annotations,
            )
        )

    _diff_schema(res, subj, b.input_schema, c.input_schema)
    if (b.output_schema or {}) != (c.output_schema or {}) and (
        b.output_schema or c.output_schema
    ):
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind="tool_output_schema_changed",
                    subject=subj,
                    detail="outputSchema changed; downstream parsing may break",
                    before=b.output_schema,
                    after=c.output_schema,
                )
            )


def _diff_schema(res: DiffResult, subj: str, b: dict[str, Any], c: dict[str, Any]) -> None:
    bprops: dict[str, Any] = b.get("properties") or {}
    cprops: dict[str, Any] = c.get("properties") or {}
    breq = set(b.get("required") or [])
    creq = set(c.get("required") or [])

    for p in sorted(set(bprops) - set(cprops)):
        was_required = p in breq
        res.changes.append(
            Change(
                severity=BREAKING if was_required else SAFE,
                kind="arg_removed_required" if was_required else "arg_removed_optional",
                subject=f"{subj}/{p}",
                detail=(
                    f"required argument '{p}' removed" if was_required
                    else f"optional argument '{p}' removed"
                ),
                before=bprops[p],
                after=None,
            )
        )
    for p in sorted(set(cprops) - set(bprops)):
        now_required = p in creq
        res.changes.append(
            Change(
                severity=BREAKING if now_required else SAFE,
                kind="arg_added_required" if now_required else "arg_added_optional",
                subject=f"{subj}/{p}",
                detail=(
                    f"new REQUIRED argument '{p}' - existing calls now fail validation"
                    if now_required
                    else f"new optional argument '{p}'"
                ),
                before=None,
                after=cprops[p],
            )
        )

    for p in sorted(set(bprops) & set(cprops)):
        _diff_property(res, subj, p, bprops[p], cprops[p], p in breq, p in creq)


def _diff_property(
    res: DiffResult,
    subj: str,
    name: str,
    b: dict[str, Any],
    c: dict[str, Any],
    b_required: bool,
    c_required: bool,
) -> None:
    target = f"{subj}/{name}"

    if b_required and not c_required:
        res.changes.append(
            Change(
                severity=SAFE,
                kind="arg_became_optional",
                subject=target,
                detail=f"'{name}' is no longer required (widening)",
                before=True,
                after=False,
            )
        )
    elif c_required and not b_required:
        res.changes.append(
            Change(
                severity=BREAKING,
                kind="arg_became_required",
                subject=target,
                detail=f"'{name}' is now required (narrowing)",
                before=False,
                after=True,
            )
        )

    bt = b.get("type")
    ct = c.get("type")
    if bt != ct:
        if _type_allows(bt, ct):
            sev = SAFE
            word = "widened"
        else:
            sev = BREAKING
            word = "narrowed"
        res.changes.append(
            Change(
                severity=sev,
                kind="arg_type_widened" if sev == SAFE else "arg_type_narrowed",
                subject=target,
                detail=f"type {word or 'changed'} {bt} -> {ct}",
                before=bt,
                after=ct,
            )
        )

    benum = b.get("enum")
    cenum = c.get("enum")
    if isinstance(benum, list) and isinstance(cenum, list):
        lost = [x for x in benum if x not in cenum]
        gained = [x for x in cenum if x not in benum]
        if lost:
            res.changes.append(
                Change(
                    severity=BREAKING,
                    kind="arg_enum_narrowed",
                    subject=target,
                    detail=f"accepted values removed: {lost}",
                    before=benum,
                    after=cenum,
                )
            )
        if gained and not lost:
            res.changes.append(
                Change(
                    severity=SAFE,
                    kind="arg_enum_widened",
                    subject=target,
                    detail=f"accepted values added: {gained}",
                    before=benum,
                    after=cenum,
                )
            )

    if b.get("default") != c.get("default"):
        res.changes.append(
            Change(
                severity=RISKY,
                kind="arg_default_changed",
                subject=target,
                detail=f"default {b.get('default')!r} -> {c.get('default')!r}",
                before=b.get("default"),
                after=c.get("default"),
            )
        )


def _diff_prompts(res: DiffResult, base: Snapshot, cur: Snapshot) -> None:
    bmap = {p.name: p for p in base.prompts}
    cmap = {p.name: p for p in cur.prompts}
    for name in sorted(set(bmap) - set(cmap)):
        res.changes.append(
            Change(
                severity=BREAKING,
                kind="prompt_removed",
                subject=f"prompts/{name}",
                detail=f"prompt '{name}' removed",
                before=bmap[name].to_dict(),
                after=None,
            )
        )
    for name in sorted(set(cmap) - set(bmap)):
        res.changes.append(
            Change(
                severity=NEW,
                kind="prompt_added",
                subject=f"prompts/{name}",
                detail=f"new prompt '{name}'",
                before=None,
                after=cmap[name].to_dict(),
            )
        )
    for name in sorted(set(bmap) & set(cmap)):
        b, c = bmap[name], cmap[name]
        if b.description != c.description:
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind="prompt_description_changed",
                    subject=f"prompts/{name}",
                    detail="prompt description changed",
                    before=b.description,
                    after=c.description,
                )
            )
        bargs = {a["name"]: a for a in b.arguments}
        cargs = {a["name"]: a for a in c.arguments}
        for an in sorted(set(bargs) - set(cargs)):
            res.changes.append(
                Change(
                    severity=BREAKING if bargs[an]["required"] else SAFE,
                    kind="prompt_arg_removed",
                    subject=f"prompts/{name}/{an}",
                    detail=f"argument '{an}' removed",
                    before=bargs[an],
                    after=None,
                )
            )
        for an in sorted(set(cargs) - set(bargs)):
            res.changes.append(
                Change(
                    severity=BREAKING if cargs[an]["required"] else SAFE,
                    kind="prompt_arg_added",
                    subject=f"prompts/{name}/{an}",
                    detail=f"argument '{an}' added (required={cargs[an]['required']})",
                    before=None,
                    after=cargs[an],
                )
            )
        for an in sorted(set(bargs) & set(cargs)):
            if bargs[an]["required"] != cargs[an]["required"]:
                sev = SAFE if bargs[an]["required"] else BREAKING
                res.changes.append(
                    Change(
                        severity=sev,
                        kind="prompt_arg_required_changed",
                        subject=f"prompts/{name}/{an}",
                        detail=(
                            f"required {bargs[an]['required']} -> {cargs[an]['required']}"
                        ),
                        before=bargs[an]["required"],
                        after=cargs[an]["required"],
                    )
                )


def _diff_resources(
    res: DiffResult,
    base: Snapshot,
    cur: Snapshot,
    label: str,
    blist: list[Resource],
    clist: list[Resource],
) -> None:
    bmap = {r.uri: r for r in blist}
    cmap = {r.uri: r for r in clist}
    for uri in sorted(set(bmap) - set(cmap)):
        res.changes.append(
            Change(
                severity=BREAKING,
                kind=f"{label}_removed",
                subject=f"{label}/{uri}",
                detail=f"{label} '{uri}' removed",
                before=bmap[uri].to_dict(),
                after=None,
            )
        )
    for uri in sorted(set(cmap) - set(bmap)):
        res.changes.append(
            Change(
                severity=NEW,
                kind=f"{label}_added",
                subject=f"{label}/{uri}",
                detail=f"new {label} '{uri}'",
                before=None,
                after=cmap[uri].to_dict(),
            )
        )
    for uri in sorted(set(bmap) & set(cmap)):
        b, c = bmap[uri], cmap[uri]
        if b.description != c.description:
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind=f"{label}_description_changed",
                    subject=f"{label}/{uri}",
                    detail="description changed",
                    before=b.description,
                    after=c.description,
                )
            )
        if b.mime_type != c.mime_type:
            res.changes.append(
                Change(
                    severity=RISKY,
                    kind=f"{label}_mime_changed",
                    subject=f"{label}/{uri}",
                    detail=f"mimeType {b.mime_type} -> {c.mime_type}",
                    before=b.mime_type,
                    after=c.mime_type,
                )
            )
