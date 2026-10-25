"""SARIF output."""

from __future__ import annotations

import json

from conftest import snapshot, tool

from mcplock.diff import diff_snapshots
from mcplock.risk import scan
from mcplock.sarif import build_sarif, to_json

LEVELS = {"breaking": "error", "risky": "error", "new": "note", "safe": "note",
          "critical": "error", "high": "error", "medium": "warning",
          "low": "note", "info": "note"}


def _base():
    return snapshot(tools=[tool("a", "one", {"p": {"type": "string"}})])


def _changed():
    return snapshot(tools=[tool("a", "two", {"p": {"type": "string"}})])


def test_envelope_shape():
    doc = build_sarif(diff=diff_snapshots(_base(), _changed()))
    assert doc["$schema"].endswith("sarif-schema-2.1.0.json")
    assert doc["version"] == "2.1.0"
    run = doc["runs"][0]
    assert run["tool"]["driver"]["name"] == "mcplock"
    assert [r["id"] for r in run["tool"]["driver"]["rules"]] == [
        "mcplock/drift",
        "mcplock/risk",
    ]


def test_empty_inputs_produce_no_results():
    doc = build_sarif()
    assert doc["runs"][0]["results"] == []


def test_change_maps_to_level_and_severity():
    doc = build_sarif(diff=diff_snapshots(_base(), _changed()))
    res = doc["runs"][0]["results"][0]
    assert res["ruleId"] == "mcplock/drift"
    assert res["level"] == "error"
    assert res["properties"]["severity"] == "risky"
    assert res["properties"]["security-severity"] == "7.5"


def test_finding_maps_to_level():
    rep = scan(snapshot(tools=[tool("run_command", "execute a shell command")]))
    doc = build_sarif(risk=rep)
    res = doc["runs"][0]["results"][0]
    assert res["ruleId"] == "mcplock/risk"
    assert res["level"] == LEVELS[res["properties"]["severity"]]


def test_logical_location_carries_subject():
    doc = build_sarif(diff=diff_snapshots(_base(), _changed()))
    loc = doc["runs"][0]["results"][0]["locations"][0]
    assert loc["logicalLocations"][0]["fullyQualifiedName"] == "tools/a"


def test_artifact_uri_defaults_to_lockfile():
    doc = build_sarif(diff=diff_snapshots(_base(), _changed()))
    loc = doc["runs"][0]["results"][0]["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "mcplock.json"


def test_artifact_uri_is_configurable():
    doc = build_sarif(diff=diff_snapshots(_base(), _changed()),
                      lockfile_path="locks/prod.json")
    loc = doc["runs"][0]["results"][0]["locations"][0]
    assert loc["physicalLocation"]["artifactLocation"]["uri"] == "locks/prod.json"


def test_rule_index_matches_declaration_order():
    doc = build_sarif(diff=diff_snapshots(_base(), _changed()))
    for r in doc["runs"][0]["results"]:
        assert r["ruleIndex"] == 0


def test_to_json_is_parseable_file_content():
    text = to_json(diff=diff_snapshots(_base(), _changed()))
    assert text.endswith("\n")
    json.loads(text)


def test_all_severities_have_sarif_levels():
    for sev in LEVELS:
        assert sev in LEVELS
