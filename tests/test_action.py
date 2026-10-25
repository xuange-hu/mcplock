"""Composite action: action.yml contract + entrypoint behaviour.

The entrypoint is exercised as a subprocess against the real example server, so
the exit codes CI relies on are verified, not assumed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "entrypoint.py"
ACTION = ROOT / "action.yml"


def run_entrypoint(tmp_path, python_exe, echo_server, **env):
    out_file = tmp_path / "gh_output.txt"
    summary = tmp_path / "summary.md"
    lock = env.pop("lockfile", str(tmp_path / "mcplock.json"))
    base = {
        "GITHUB_OUTPUT": str(out_file),
        "GITHUB_STEP_SUMMARY": str(summary),
        "INPUT_LOCKFILE": lock,
        "INPUT_FAIL_ON": "breaking",
        "PATH": os.environ.get("PATH", ""),
        "PYTHONPATH": str(ROOT / "src"),
    }
    base.update(env)
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=base,
        check=False,
        cwd=str(ROOT),
    )
    outputs = {}
    if out_file.exists():
        for line in out_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                outputs[k] = v
    return proc, outputs, lock, summary


def test_action_yml_declares_inputs_and_outputs():
    text = ACTION.read_text(encoding="utf-8")
    for key in ("lockfile", "command", "url", "fail-on", "update"):
        assert f"{key}:" in text
    for key in ("drift", "max-severity", "fingerprint", "report"):
        assert key in text
    assert "runs:" in text and "using: composite" in text


def test_action_yml_has_branding_for_marketplace():
    text = ACTION.read_text(encoding="utf-8")
    assert "branding:" in text and "icon: lock" in text


def test_action_yml_uses_pinned_actions_only():
    """Every `uses:` in the action must be a 40-hex SHA."""
    import re

    text = ACTION.read_text(encoding="utf-8")
    refs = re.findall(r"uses:\s*\S+@(\S+)", text)
    for ref in refs:
        assert re.fullmatch(r"[0-9a-f]{40}", ref), ref


def test_missing_command_and_url_is_error(tmp_path):
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        env={"GITHUB_OUTPUT": str(tmp_path / "o.txt"), "INPUT_LOCKFILE": "x.json"},
        check=False,
        cwd=str(ROOT),
    )
    assert proc.returncode == 2
    assert "command" in (proc.stderr + proc.stdout).lower()


def test_first_run_creates_baseline(tmp_path, python_exe, echo_server):
    proc, outputs, lock, _ = run_entrypoint(
        tmp_path, python_exe, echo_server,
        INPUT_COMMAND=f"{python_exe} {echo_server}",
    )
    assert proc.returncode == 0, proc.stderr
    assert Path(lock).exists()
    assert outputs["drift"] == "false"
    assert len(outputs["fingerprint"]) == 12


def test_unchanged_server_reports_no_drift(tmp_path, python_exe, echo_server):
    run_entrypoint(tmp_path, python_exe, echo_server,
                   INPUT_COMMAND=f"{python_exe} {echo_server}")
    proc, outputs, _, _ = run_entrypoint(
        tmp_path, python_exe, echo_server,
        INPUT_COMMAND=f"{python_exe} {echo_server}",
        lockfile=str(tmp_path / "mcplock.json"),
    )
    assert proc.returncode == 0
    assert outputs["drift"] == "false"
    assert outputs["max-severity"] == ""


def test_mutated_server_reports_breaking_drift(tmp_path, python_exe, echo_server):
    run_entrypoint(tmp_path, python_exe, echo_server,
                   INPUT_COMMAND=f"{python_exe} {echo_server}")
    proc, outputs, _, _ = run_entrypoint(
        tmp_path, python_exe, echo_server,
        INPUT_COMMAND=f"{python_exe} {echo_server} --mutated",
        lockfile=str(tmp_path / "mcplock.json"),
    )
    assert proc.returncode == 1
    assert outputs["drift"] == "true"
    assert outputs["max-severity"] == "breaking"
    assert "cwd" in outputs["report"]


def test_drift_writes_job_summary(tmp_path, python_exe, echo_server):
    run_entrypoint(tmp_path, python_exe, echo_server,
                   INPUT_COMMAND=f"{python_exe} {echo_server}")
    _, _, _, summary = run_entrypoint(
        tmp_path, python_exe, echo_server,
        INPUT_COMMAND=f"{python_exe} {echo_server} --mutated",
        lockfile=str(tmp_path / "mcplock.json"),
    )
    text = summary.read_text(encoding="utf-8")
    assert "mcplock capability drift" in text
    assert "breaking" in text


def test_fail_on_risky_is_stricter(tmp_path, python_exe, echo_server):
    run_entrypoint(tmp_path, python_exe, echo_server,
                   INPUT_COMMAND=f"{python_exe} {echo_server}")
    proc, outputs, _, _ = run_entrypoint(
        tmp_path, python_exe, echo_server,
        INPUT_COMMAND=f"{python_exe} {echo_server} --mutated",
        INPUT_FAIL_ON="risky",
        lockfile=str(tmp_path / "mcplock.json"),
    )
    assert proc.returncode == 1
    assert outputs["max-severity"] == "breaking"


def test_update_rewrites_the_lockfile(tmp_path, python_exe, echo_server):
    lock = str(tmp_path / "mcplock.json")
    run_entrypoint(tmp_path, python_exe, echo_server,
                   INPUT_COMMAND=f"{python_exe} {echo_server}", lockfile=lock)
    before = json.loads(Path(lock).read_text(encoding="utf-8"))["surface_digest"]
    run_entrypoint(tmp_path, python_exe, echo_server,
                   INPUT_COMMAND=f"{python_exe} {echo_server} --mutated",
                   INPUT_UPDATE="true", lockfile=lock)
    after = json.loads(Path(lock).read_text(encoding="utf-8"))["surface_digest"]
    assert before != after


@pytest.mark.parametrize("missing", ["command", "url"])
def test_unreachable_server_is_an_error_not_drift(tmp_path, missing):
    """Exit 2 (tool error) must never be reported as drift - a broken runner
    and a rug pull are different failures."""
    proc = subprocess.run(
        [sys.executable, str(ENTRYPOINT)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env={
            "GITHUB_OUTPUT": str(tmp_path / "o.txt"),
            "GITHUB_STEP_SUMMARY": str(tmp_path / "s.md"),
            "INPUT_LOCKFILE": str(tmp_path / "mcplock.json"),
            "INPUT_URL": "http://127.0.0.1:1/mcp" if missing == "url" else "",
            "INPUT_COMMAND": "definitely-not-a-real-server-xyz" if missing == "command" else "",
            "PYTHONPATH": str(ROOT / "src"),
        },
        check=False,
        cwd=str(ROOT),
    )
    assert proc.returncode == 2
    assert "::error::" in proc.stderr
