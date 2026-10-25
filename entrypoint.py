#!/usr/bin/env python3
"""Entrypoint for the mcplock composite action.

Reads the action inputs from the environment, runs the CLI as a subprocess
(so exit codes and output formatting stay identical to local usage), and
writes step outputs plus a job summary.

Why a subprocess instead of importing the package: the action must behave
exactly like the documented `mcplock verify ...` one-liner, including its exit
code semantics (0 clean / 1 drift / 2 error).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _set_output(name: str, value: str) -> None:
    """Write to GITHUB_OUTPUT, falling back to the deprecated ::set-output
    syntax only if the file is unavailable."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")
    else:  # pragma: no cover
        print(f"::set-output name={name}::{value}")


def _digest_of(lockfile: str) -> str:
    """Read surface_digest without importing the package (the action installs
    mcplock into the runner env, not necessarily into this interpreter)."""
    try:
        with open(lockfile, encoding="utf-8") as fh:
            return str(json.load(fh).get("surface_digest", ""))
    except (OSError, json.JSONDecodeError):
        return ""


def _mask(value: str) -> str:
    """GitHub forbids newlines in step outputs."""
    return value.replace("\r", " ").replace("\n", " ")


def main() -> int:
    lockfile = os.environ.get("INPUT_LOCKFILE", "mcplock.json")
    command = os.environ.get("INPUT_COMMAND", "").strip()
    url = os.environ.get("INPUT_URL", "").strip()
    fail_on = os.environ.get("INPUT_FAIL_ON", "breaking")
    update = os.environ.get("INPUT_UPDATE", "false").lower() in ("1", "true", "yes")
    extra = os.environ.get("INPUT_EXTRA_ARGS", "").strip()

    if not command and not url:
        print("::error::either `command` or `url` must be provided", file=sys.stderr)
        return 2

    # First run: no lockfile yet. Create the baseline instead of failing, so
    # adopting the action is a one-line change and not a bootstrap dance.
    if not Path(lockfile).exists():
        snap_argv = [sys.executable, "-m", "mcplock", "snap", "--out", lockfile,
                     "--no-color", "--quiet"]
        if url:
            snap_argv.extend(["--url", url])
        else:
            snap_argv.append("--")
            snap_argv.extend(command.split())
        print("+ " + " ".join(snap_argv), flush=True)
        snap = subprocess.run(
            snap_argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", check=False,
        )
        if snap.returncode != 0:
            print(snap.stderr, file=sys.stderr)
            print("::error::mcplock could not create a baseline", file=sys.stderr)
            return 2
        digest = _digest_of(lockfile)
        message = (
            f"no lockfile at {lockfile} - created baseline, fingerprint {digest[:12]}"
        )
        print(f"::notice::{message}")
        _set_output("drift", "false")
        _set_output("max-severity", "")
        _set_output("fingerprint", digest[:12])
        _set_output("report", "baseline created; no drift to compare")
        return 0

    argv = [sys.executable, "-m", "mcplock", "verify", "--lock", lockfile,
            "--fail-on", fail_on, "--no-color", "--json"]
    if update:
        argv.append("--update")
    if extra:
        argv.extend(extra.split())
    if url:
        argv.extend(["--url", url])
    else:
        argv.append("--")
        argv.extend(command.split())

    print("+ " + " ".join(argv), flush=True)
    proc = subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8",
        errors="replace", check=False,
    )

    report = (proc.stderr or "").strip() or (proc.stdout or "").strip()
    max_severity = ""
    fingerprint = ""
    drift = "false"

    if proc.returncode in (0, 1):
        try:
            payload = json.loads(proc.stdout)
            max_severity = payload.get("max_severity") or ""
            counts = payload.get("counts") or {}
            lines = [
                f"baseline {str(payload.get('baseline_digest', ''))[:12]}"
                f"  ->  current {str(payload.get('current_digest', ''))[:12]}"
            ]
            lines.append("  ".join(f"{k}={v}" for k, v in counts.items() if v) or "no changes")
            for ch in payload.get("changes", []):
                lines.append(f"[{ch['severity'].upper()}] {ch['subject']}: {ch['detail']}")
            report = "\n".join(lines)
            fingerprint = str(payload.get("current_digest", ""))[:12]
        except (json.JSONDecodeError, TypeError):
            pass
        drift = "true" if proc.returncode == 1 else "false"

    if proc.returncode == 2:
        print(report, file=sys.stderr)
        print(f"::error::mcplock failed to run (exit {proc.returncode})", file=sys.stderr)
        _set_output("report", _mask(report[:4000]))
        return 2

    print(report)
    _set_output("drift", drift)
    _set_output("max-severity", _mask(max_severity))
    _set_output("fingerprint", _mask(fingerprint))
    _set_output("report", _mask(report[:4000]))

    summary = Path(os.environ.get("GITHUB_STEP_SUMMARY", "")) if os.environ.get(
        "GITHUB_STEP_SUMMARY"
    ) else None
    if summary is not None:
        with summary.open("a", encoding="utf-8") as fh:
            fh.write("## mcplock capability drift\n\n")
            fh.write(f"drift: **{drift}**  ·  max severity: **{max_severity or 'none'}**\n\n")
            fh.write("```\n" + report[:8000] + "\n```\n")

    if drift == "true":
        print(
            f"::error::MCP capability drift at or above '{fail_on}' "
            f"(max severity: {max_severity or 'unknown'})",
            file=sys.stderr,
        )
    return 0 if drift == "false" else 1


if __name__ == "__main__":
    raise SystemExit(main())
