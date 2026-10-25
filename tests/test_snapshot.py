"""Snapshot model: normalization, digests, serialization, version gates."""

from __future__ import annotations

import json

import pytest
from conftest import roundtrip, snapshot, tool

from mcplock.snapshot import (
    SNAPSHOT_VERSION,
    Prompt,
    Resource,
    Snapshot,
    Tool,
    _normalize_schema,
    canon_json,
)


def test_canonical_json_is_key_order_independent():
    a = {"b": 1, "a": {"d": 2, "c": 3}}
    b = {"a": {"c": 3, "d": 2}, "b": 1}
    assert canon_json(a) == canon_json(b)


def test_canonical_json_is_list_order_sensitive():
    assert canon_json([1, 2]) != canon_json([2, 1])


@pytest.mark.parametrize(
    "schema,expected",
    [
        ({"type": "object"}, {"type": "object"}),
        ({"type": "str"}, {"type": "string"}),
        ({"type": "int"}, {"type": "integer"}),
        ({"type": "bool"}, {"type": "boolean"}),
        ({"type": ["string", "null"]}, {"type": ["null", "string"]}),
    ],
)
def test_type_aliases_normalized(schema, expected):
    assert _normalize_schema(schema) == expected


def test_normalize_drops_presentation_noise():
    raw = {
        "type": "object",
        "title": "Args",
        "$schema": "http://json-schema.org/draft-07/schema#",
        "examples": [{"a": 1}],
        "properties": {
            "a": {"type": "string", "title": "A", "description": "the a", "default": "x"}
        },
    }
    out = _normalize_schema(raw)
    assert "title" not in out
    assert "$schema" not in out
    assert "examples" not in out
    assert out["properties"]["a"] == {"type": "string", "default": "x"}


def test_required_list_is_sorted():
    raw = {"type": "object", "properties": {"a": {}, "b": {}}, "required": ["b", "a"]}
    assert _normalize_schema(raw)["required"] == ["a", "b"]


def test_non_dict_schema_degrades_to_empty_object():
    assert _normalize_schema("not a schema") == {"type": "object", "properties": {}}


def test_tool_digests_recomputed_on_construction():
    t = Tool.from_server(
        {
            "name": "x",
            "description": "d",
            "inputSchema": {"type": "object", "properties": {"p": {"type": "string"}}},
        }
    )
    assert len(t.schema_digest) == 16
    assert len(t.description_digest) == 16


def test_schema_digest_stable_across_key_reorder():
    a = Tool.from_server(
        {"name": "x", "inputSchema": {"properties": {"p": {"type": "string"}}, "type": "object"}}
    )
    b = Tool.from_server(
        {"name": "x", "inputSchema": {"type": "object", "properties": {"p": {"type": "string"}}}}
    )
    assert a.schema_digest == b.schema_digest


def test_description_digest_is_whitespace_sensitive():
    """A single trailing space must change the digest - descriptions are the
    rug-pull vector, so they are compared byte for byte."""
    a = Tool.from_server({"name": "x", "description": "run a command"})
    b = Tool.from_server({"name": "x", "description": "run a command "})
    assert a.description_digest != b.description_digest


def test_snapshot_sorts_all_collections():
    snap = snapshot(
        tools=[tool("b"), tool("a")],
        prompts=[Prompt(name="z", title=None, description="", arguments=[]),
                 Prompt(name="y", title=None, description="", arguments=[])],
        resources=[Resource(uri="file:///b", name="b", title=None, description="", mime_type=None),
                   Resource(uri="file:///a", name="a", title=None, description="", mime_type=None)],
    )
    assert [t.name for t in snap.tools] == ["a", "b"]
    assert [p.name for p in snap.prompts] == ["y", "z"]
    assert [r.uri for r in snap.resources] == ["file:///a", "file:///b"]


def test_surface_digest_excludes_timestamps_and_server_version():
    a = snapshot(tools=[tool("a")], server={"name": "svc", "version": "1.0.0"})
    b = snapshot(tools=[tool("a")], server={"name": "svc", "version": "9.9.9"})
    assert a.surface_digest == b.surface_digest


def test_surface_digest_changes_when_tool_changes():
    a = snapshot(tools=[tool("a", props={"p": {"type": "string"}})])
    b = snapshot(tools=[tool("a", props={"p": {"type": "integer"}})])
    assert a.surface_digest != b.surface_digest


def test_fingerprint_is_short_prefix_of_digest():
    snap = snapshot(tools=[tool("a")])
    assert snap.fingerprint == snap.surface_digest[:12]


def test_roundtrip_preserves_surface_digest():
    snap = snapshot(
        tools=[tool("a", "desc", {"p": {"type": "string"}}, ["p"])],
        prompts=[Prompt(name="p1", title=None, description="d",
                        arguments=[{"name": "x", "description": "", "required": True}])],
        resources=[Resource(uri="file:///a", name="a", title=None,
                            description="d", mime_type="text/plain")],
    )
    back = roundtrip(snap)
    assert back.surface_digest == snap.surface_digest
    assert [t.name for t in back.tools] == ["a"]
    assert back.tools[0].input_schema == snap.tools[0].input_schema
    assert back.prompts[0].arguments == snap.prompts[0].arguments
    assert back.resources[0].mime_type == "text/plain"


def test_from_dict_rejects_foreign_schema():
    with pytest.raises(ValueError, match="not an mcplock snapshot"):
        Snapshot.from_dict({"schema": "something-else"})


def test_from_dict_rejects_future_snapshot_version():
    data = snapshot().to_dict()
    data["snapshot_version"] = SNAPSHOT_VERSION + 1
    with pytest.raises(ValueError, match="snapshot_version"):
        Snapshot.from_dict(data)


def test_from_dict_rejects_non_object():
    with pytest.raises(ValueError, match="must be a JSON object"):
        Snapshot.from_dict(["nope"])


def test_to_json_is_parseable_and_indented():
    text = snapshot(tools=[tool("a")]).to_json()
    assert text.endswith("\n")
    parsed = json.loads(text)
    assert parsed["schema"] == "mcplock-snapshot"


def test_prompt_arguments_sorted_and_normalized():
    p = Prompt.from_server(
        {
            "name": "p",
            "arguments": [
                {"name": "b", "required": True},
                {"name": "a"},
                "not-a-dict",
            ],
        }
    )
    assert [a["name"] for a in p.arguments] == ["a", "b"]
    assert p.arguments[0]["required"] is False
    assert p.arguments[1]["required"] is True


def test_tool_accepts_camel_and_snake_schema_keys():
    for key in ("inputSchema", "input_schema"):
        t = Tool.from_server({"name": "x", key: {"type": "object"}})
        assert t.input_schema == {"type": "object"}


def test_resource_accepts_mime_type_aliases():
    for key in ("mimeType", "mime_type"):
        r = Resource.from_server({"uri": "u", key: "text/plain"})
        assert r.mime_type == "text/plain"


def test_tool_to_dict_omits_empty_optionals():
    d = Tool.from_server({"name": "x", "description": "d"}).to_dict()
    assert "title" not in d
    assert "output_schema" not in d
    assert "annotations" not in d
    assert d["name"] == "x"
    assert d["description"] == "d"
