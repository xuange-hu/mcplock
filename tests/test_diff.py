"""Drift classification.

The severity of a change is the whole product, so every rule has a test that
pins the direction - widening safe, narrowing breaking.
"""

from __future__ import annotations

import pytest
from conftest import roundtrip, snapshot, tool

from mcplock.diff import (
    BREAKING,
    NEW,
    RISKY,
    SAFE,
    SEVERITY_RANK,
    diff_snapshots,
)
from mcplock.snapshot import Prompt, Resource


def kinds(res):
    return [c.kind for c in res.changes]


def severities(res):
    return [c.severity for c in res.changes]


def diff(a, b):
    """Compare two snapshots through a serialize round-trip, like real usage."""
    return diff_snapshots(roundtrip(a), roundtrip(b))


# -- severity ordering --------------------------------------------------------

def test_severity_rank_direction():
    """Lower rank = more severe. Get this backwards and --fail-on never fires."""
    assert SEVERITY_RANK[BREAKING] < SEVERITY_RANK[RISKY]
    assert SEVERITY_RANK[RISKY] < SEVERITY_RANK[NEW]
    assert SEVERITY_RANK[NEW] < SEVERITY_RANK[SAFE]


def test_at_least_threshold_semantics():
    res = diff(
        snapshot(tools=[tool("a", "one")]),
        snapshot(tools=[tool("a", "two")]),
    )
    assert res.changes and res.max_severity == RISKY
    assert res.at_least(RISKY) is True
    assert res.at_least(BREAKING) is False
    assert res.at_least(SAFE) is True


def test_empty_diff_has_no_max_severity():
    res = diff(snapshot(tools=[tool("a")]), snapshot(tools=[tool("a")]))
    assert res.changes == []
    assert res.max_severity is None
    assert res.at_least(SAFE) is False
    assert res.counts == {BREAKING: 0, RISKY: 0, NEW: 0, SAFE: 0}


# -- tools --------------------------------------------------------------------

def test_tool_removed_is_breaking():
    res = diff(snapshot(tools=[tool("a"), tool("b")]), snapshot(tools=[tool("a")]))
    assert "tool_removed" in kinds(res)
    change = next(c for c in res.changes if c.kind == "tool_removed")
    assert change.severity == BREAKING
    assert change.subject == "tools/b"


def test_tool_added_is_new():
    res = diff(snapshot(tools=[tool("a")]), snapshot(tools=[tool("a"), tool("b")]))
    change = next(c for c in res.changes if c.kind == "tool_added")
    assert change.severity == NEW


def test_description_change_is_risky_not_breaking():
    res = diff(
        snapshot(tools=[tool("a", "read a file")]),
        snapshot(tools=[tool("a", "read a file (v2)")]),
    )
    change = next(c for c in res.changes if c.kind == "tool_description_changed")
    assert change.severity == RISKY


def test_new_required_arg_is_breaking():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {"type": "string"}})]),
        snapshot(tools=[tool("a", props={"p": {"type": "string"},
                                         "q": {"type": "string"}}, required=["q"])]),
    )
    assert "arg_added_required" in kinds(res)


def test_new_optional_arg_is_safe():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {"type": "string"}})]),
        snapshot(tools=[tool("a", props={"p": {"type": "string"},
                                         "q": {"type": "string"}})]),
    )
    change = next(c for c in res.changes if c.kind == "arg_added_optional")
    assert change.severity == SAFE


def test_removed_required_arg_is_breaking():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {}, "q": {}}, required=["p", "q"])]),
        snapshot(tools=[tool("a", props={"p": {}}, required=["p"])]),
    )
    change = next(c for c in res.changes if c.kind == "arg_removed_required")
    assert change.severity == BREAKING


def test_removed_optional_arg_is_safe():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {}, "q": {}}, required=["p"])]),
        snapshot(tools=[tool("a", props={"p": {}}, required=["p"])]),
    )
    change = next(c for c in res.changes if c.kind == "arg_removed_optional")
    assert change.severity == SAFE


def test_arg_becoming_required_is_breaking():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {}})]),
        snapshot(tools=[tool("a", props={"p": {}}, required=["p"])]),
    )
    assert "arg_became_required" in kinds(res)
    assert next(c for c in res.changes if c.kind == "arg_became_required").severity == BREAKING


def test_arg_becoming_optional_is_safe():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {}}, required=["p"])]),
        snapshot(tools=[tool("a", props={"p": {}})]),
    )
    assert "arg_became_optional" in kinds(res)
    assert next(c for c in res.changes if c.kind == "arg_became_optional").severity == SAFE


def test_integer_to_number_is_safe_widening():
    res = diff(
        snapshot(tools=[tool("a", props={"n": {"type": "integer"}})]),
        snapshot(tools=[tool("a", props={"n": {"type": "number"}})]),
    )
    assert "arg_type_widened" in kinds(res)


@pytest.mark.parametrize(
    "before,after",
    [("string", "array"), ("number", "integer"), ("array", "string"), ("object", "string")],
)
def test_incompatible_type_change_is_breaking(before, after):
    """JSON Schema types are disjoint; only integer->number is a widening.
    Widening an *input* type to a disjoint one breaks existing callers."""
    res = diff(
        snapshot(tools=[tool("a", props={"p": {"type": before}})]),
        snapshot(tools=[tool("a", props={"p": {"type": after}})]),
    )
    assert "arg_type_narrowed" in kinds(res)


def test_type_removed_is_widening():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {"type": "string"}})]),
        snapshot(tools=[tool("a", props={"p": {}})]),
    )
    assert "arg_type_widened" in kinds(res)


def test_type_added_is_narrowing():
    res = diff(
        snapshot(tools=[tool("a", props={"p": {}})]),
        snapshot(tools=[tool("a", props={"p": {"type": "string"}})]),
    )
    assert "arg_type_narrowed" in kinds(res)


def test_enum_member_removed_is_breaking():
    res = diff(
        snapshot(tools=[tool("a", props={"m": {"type": "string",
                                               "enum": ["fast", "deep"]}})]),
        snapshot(tools=[tool("a", props={"m": {"type": "string", "enum": ["fast"]}})]),
    )
    assert "arg_enum_narrowed" in kinds(res)
    assert next(c for c in res.changes if c.kind == "arg_enum_narrowed").severity == BREAKING


def test_enum_member_added_is_safe():
    res = diff(
        snapshot(tools=[tool("a", props={"m": {"type": "string", "enum": ["fast"]}})]),
        snapshot(tools=[tool("a", props={"m": {"type": "string",
                                               "enum": ["fast", "deep"]}})]),
    )
    assert "arg_enum_widened" in kinds(res)


def test_default_change_is_risky():
    res = diff(
        snapshot(tools=[tool("a", props={"n": {"type": "integer", "default": 5}})]),
        snapshot(tools=[tool("a", props={"n": {"type": "integer", "default": 10}})]),
    )
    assert "arg_default_changed" in kinds(res)


def test_annotations_change_is_risky():
    res = diff(
        snapshot(tools=[tool("a", annotations={"readOnlyHint": True})]),
        snapshot(tools=[tool("a", annotations={"readOnlyHint": False})]),
    )
    assert "tool_annotations_changed" in kinds(res)


def test_reordered_properties_are_not_drift():
    a = snapshot(tools=[tool("a", props={"x": {"type": "string"}, "y": {"type": "string"}})])
    b = snapshot(tools=[tool("a", props={"y": {"type": "string"}, "x": {"type": "string"}})])
    assert diff_snapshots(a, b).changes == []


# -- protocol / capabilities --------------------------------------------------

def test_protocol_version_change_is_risky():
    res = diff(snapshot(protocol="2025-06-18"), snapshot(protocol="2025-03-26"))
    assert "protocol_version_changed" in kinds(res)


def test_server_name_change_is_risky():
    res = diff(
        snapshot(server={"name": "svc", "version": "1"}),
        snapshot(server={"name": "other", "version": "1"}),
    )
    assert "server_name_changed" in kinds(res)


def test_capability_dropped_is_risky():
    res = diff(snapshot(caps={"tools": {}, "prompts": {}}), snapshot(caps={"tools": {}}))
    assert "capability_dropped" in kinds(res)


def test_capability_added_is_new():
    res = diff(snapshot(caps={"tools": {}}), snapshot(caps={"tools": {}, "sampling": {}}))
    change = next(c for c in res.changes if c.kind == "capability_added")
    assert change.severity == NEW


# -- prompts / resources ------------------------------------------------------

def test_prompt_removed_is_breaking():
    p = Prompt(name="p", title=None, description="", arguments=[])
    res = diff(snapshot(prompts=[p]), snapshot())
    assert "prompt_removed" in kinds(res)


def test_prompt_arg_becoming_required_is_breaking():
    a = snapshot(prompts=[Prompt(name="p", title=None, description="",
                                 arguments=[{"name": "x", "description": "",
                                             "required": False}])])
    b = snapshot(prompts=[Prompt(name="p", title=None, description="",
                                 arguments=[{"name": "x", "description": "",
                                             "required": True}])])
    assert "prompt_arg_required_changed" in kinds(diff(a, b))
    change = next(c for c in diff(a, b).changes if c.kind == "prompt_arg_required_changed")
    assert change.severity == BREAKING


def test_resource_uri_change_is_remove_plus_add():
    a = snapshot(resources=[Resource(uri="file:///a", name="a", title=None,
                                     description="", mime_type=None)])
    b = snapshot(resources=[Resource(uri="file:///b", name="b", title=None,
                                     description="", mime_type=None)])
    ks = kinds(diff(a, b))
    assert "resources_removed" in ks and "resources_added" in ks


def test_resource_description_change_is_risky():
    a = snapshot(resources=[Resource(uri="u", name="n", title=None,
                                     description="old", mime_type=None)])
    b = snapshot(resources=[Resource(uri="u", name="n", title=None,
                                     description="new", mime_type=None)])
    assert "resources_description_changed" in kinds(diff(a, b))


# -- output shape -------------------------------------------------------------

def test_changes_sorted_by_severity_then_subject():
    res = diff(
        snapshot(tools=[tool("a", "one"), tool("z", "x"), tool("gone")]),
        snapshot(tools=[tool("a", "two"), tool("z", "y")]),
    )
    ranks = [SEVERITY_RANK[c.severity] for c in res.changes]
    assert ranks == sorted(ranks)


def test_to_dict_is_json_serializable():
    import json

    res = diff(snapshot(tools=[tool("a", "one")]), snapshot(tools=[tool("a", "two")]))
    payload = json.dumps(res.to_dict())
    assert "tool_description_changed" in payload


def test_filtered_and_max_severity():
    res = diff(
        snapshot(tools=[tool("a", "one"), tool("b")]),
        snapshot(tools=[tool("a", "two")]),
    )
    breaking_only = res.filtered(BREAKING)
    assert all(c.severity == BREAKING for c in breaking_only)
    assert len(breaking_only) == 1
