"""Snapshot model.

A snapshot is the *contract* an MCP server advertises: server identity,
protocol version, and the declared tool / prompt / resource surface.

Everything mcplock compares is normalized first, so that:

* key reordering in a JSON schema is not drift,
* whitespace inside a description *is* drift (it is the rug-pull vector),
* ``required`` list ordering is not drift.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from .client import CLIENT_INFO

SNAPSHOT_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canon(value: Any) -> Any:
    """Recursively order maps by key so hashing is stable across rewrites."""
    if isinstance(value, dict):
        return {k: _canon(value[k]) for k in sorted(value)}
    if isinstance(value, list):
        return [_canon(v) for v in value]
    return value


def canon_json(value: Any) -> str:
    return json.dumps(_canon(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _short_digest(value: Any, n: int = 16) -> str:
    return hashlib.sha256(canon_json(value).encode("utf-8")).hexdigest()[:n]


@dataclass
class Tool:
    name: str
    title: str | None
    description: str
    input_schema: dict[str, Any]
    output_schema: dict[str, Any] | None
    annotations: dict[str, Any]
    schema_digest: str = ""
    description_digest: str = ""

    def __post_init__(self) -> None:
        self.schema_digest = _short_digest(self.input_schema)
        self.description_digest = _short_digest(self.description)

    @classmethod
    def from_server(cls, raw: dict[str, Any]) -> Tool:
        schema = raw.get("inputSchema") or raw.get("input_schema") or {}
        if not isinstance(schema, dict):
            schema = {}
        out = raw.get("outputSchema") or raw.get("output_schema")
        ann = raw.get("annotations")
        return cls(
            name=str(raw.get("name") or ""),
            title=_opt_str(raw.get("title")),
            description=str(raw.get("description") or ""),
            input_schema=_normalize_schema(schema),
            output_schema=_normalize_schema(out) if isinstance(out, dict) else None,
            annotations=ann if isinstance(ann, dict) else {},
        )

    def to_dict(self) -> dict[str, Any]:
        """Serialize, omitting every empty optional field.

        A lockfile is diffed by humans too, so ``"title": null`` noise on every
        tool is worse than a missing key.
        """
        d = asdict(self)
        for key in ("title", "output_schema", "annotations"):
            if not d.get(key):
                d.pop(key, None)
        return {k: v for k, v in d.items() if v is not None}


@dataclass
class Prompt:
    name: str
    title: str | None
    description: str
    arguments: list[dict[str, Any]]
    digest: str = ""

    def __post_init__(self) -> None:
        self.digest = _short_digest([self.arguments, self.description])

    @classmethod
    def from_server(cls, raw: dict[str, Any]) -> Prompt:
        args = raw.get("arguments") or []
        norm = []
        if isinstance(args, list):
            for a in args:
                if not isinstance(a, dict):
                    continue
                norm.append(
                    {
                        "name": str(a.get("name") or ""),
                        "description": str(a.get("description") or ""),
                        "required": bool(a.get("required", False)),
                    }
                )
        norm.sort(key=lambda x: x["name"])
        return cls(
            name=str(raw.get("name") or ""),
            title=_opt_str(raw.get("title")),
            description=str(raw.get("description") or ""),
            arguments=norm,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "arguments": self.arguments,
            "digest": self.digest,
        }


@dataclass
class Resource:
    uri: str
    name: str
    title: str | None
    description: str
    mime_type: str | None
    digest: str = ""

    def __post_init__(self) -> None:
        self.digest = _short_digest([self.uri, self.name, self.description, self.mime_type])

    @classmethod
    def from_server(cls, raw: dict[str, Any]) -> Resource:
        return cls(
            uri=str(raw.get("uri") or ""),
            name=str(raw.get("name") or ""),
            title=_opt_str(raw.get("title")),
            description=str(raw.get("description") or ""),
            mime_type=_opt_str(raw.get("mimeType") or raw.get("mime_type")),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Snapshot:
    schema: str = "mcplock-snapshot"
    snapshot_version: int = SNAPSHOT_VERSION
    generated_at: str = field(default_factory=_now)
    generator: dict[str, str] = field(
        default_factory=lambda: {
            "name": CLIENT_INFO["name"],
            "version": CLIENT_INFO["version"],
        }
    )
    target: dict[str, Any] = field(default_factory=dict)
    server: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)
    protocol_version: str | None = None
    tools: list[Tool] = field(default_factory=list)
    prompts: list[Prompt] = field(default_factory=list)
    resources: list[Resource] = field(default_factory=list)
    resource_templates: list[Resource] = field(default_factory=list)
    surface_digest: str = ""

    def __post_init__(self) -> None:
        self.tools.sort(key=lambda t: t.name)
        self.prompts.sort(key=lambda p: p.name)
        self.resources.sort(key=lambda r: r.uri)
        self.resource_templates.sort(key=lambda r: r.uri)
        self.surface_digest = self.compute_surface_digest()

    def compute_surface_digest(self) -> str:
        """Digest over the *declared surface only*.

        Deliberately excludes ``generated_at`` and server version strings so
        the digest is stable across runs of an unchanged server.
        """
        payload = {
            "tools": [(t.name, t.schema_digest, t.description_digest) for t in self.tools],
            "prompts": [(p.name, p.digest) for p in self.prompts],
            "resources": [(r.uri, r.digest) for r in self.resources],
            "templates": [(r.uri, r.digest) for r in self.resource_templates],
        }
        return hashlib.sha256(canon_json(payload).encode("utf-8")).hexdigest()

    @property
    def fingerprint(self) -> str:
        """Short human-quotable id of this exact surface."""
        return self.surface_digest[:12]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "snapshot_version": self.snapshot_version,
            "generated_at": self.generated_at,
            "generator": self.generator,
            "target": self.target,
            "server": self.server,
            "protocol_version": self.protocol_version,
            "capabilities": self.capabilities,
            "surface_digest": self.surface_digest,
            "tools": [t.to_dict() for t in self.tools],
            "prompts": [p.to_dict() for p in self.prompts],
            "resources": [r.to_dict() for r in self.resources],
            "resource_templates": [r.to_dict() for r in self.resource_templates],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, indent=indent) + "\n"

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Snapshot:
        if not isinstance(data, dict):
            raise ValueError("snapshot must be a JSON object")
        if data.get("schema") != "mcplock-snapshot":
            raise ValueError(
                f"not an mcplock snapshot (schema={data.get('schema')!r})"
            )
        version = int(data.get("snapshot_version") or 0)
        if version != SNAPSHOT_VERSION:
            raise ValueError(
                f"snapshot_version {version} != supported {SNAPSHOT_VERSION}"
            )
        snap = cls(
            generated_at=str(data.get("generated_at") or _now()),
            generator=data.get("generator") or {},
            target=data.get("target") or {},
            server=data.get("server") or {},
            capabilities=data.get("capabilities") or {},
            protocol_version=data.get("protocol_version"),
            tools=[_revive_tool(t) for t in data.get("tools") or []],
            prompts=[_revive_prompt(p) for p in data.get("prompts") or []],
            resources=[_revive_resource(r) for r in data.get("resources") or []],
            resource_templates=[
                _revive_resource(r) for r in data.get("resource_templates") or []
            ],
        )
        return snap

    @classmethod
    def from_json(cls, text: str) -> Snapshot:
        return cls.from_dict(json.loads(text))


def _revive_tool(d: dict[str, Any]) -> Tool:
    return Tool(
        name=str(d.get("name") or ""),
        title=d.get("title"),
        description=str(d.get("description") or ""),
        input_schema=d.get("input_schema") or {},
        output_schema=d.get("output_schema"),
        annotations=d.get("annotations") or {},
    )


def _revive_prompt(d: dict[str, Any]) -> Prompt:
    return Prompt(
        name=str(d.get("name") or ""),
        title=d.get("title"),
        description=str(d.get("description") or ""),
        arguments=d.get("arguments") or [],
    )


def _revive_resource(d: dict[str, Any]) -> Resource:
    return Resource(
        uri=str(d.get("uri") or ""),
        name=str(d.get("name") or ""),
        title=d.get("title"),
        description=str(d.get("description") or ""),
        mime_type=d.get("mime_type"),
    )


def _opt_str(v: Any) -> str | None:
    if v is None:
        return None
    s = str(v)
    return s or None


_TYPE_ALIASES = {
    # JS/TS schema builders and Python json-schema libs spell these differently.
    "int": "integer",
    "number": "number",
    "bool": "boolean",
    "str": "string",
}


def _normalize_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Canonicalize a JSON Schema fragment.

    Only the parts that matter for call compatibility are kept: structure,
    property types and required-ness. ``title``/``examples``/``$schema`` noise
    is dropped because servers regenerate it on every release and it never
    changes how a client must call the tool.
    """
    out: dict[str, Any] = {}
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    stype = schema.get("type")
    if isinstance(stype, str):
        out["type"] = _TYPE_ALIASES.get(stype.lower(), stype)
    elif isinstance(stype, list):
        out["type"] = sorted(_TYPE_ALIASES.get(str(t).lower(), str(t)) for t in stype)

    props = schema.get("properties")
    if isinstance(props, dict):
        norm: dict[str, Any] = {}
        for key, sub in props.items():
            norm[str(key)] = _normalize_schema(sub) if isinstance(sub, dict) else {}
        out["properties"] = norm

    required = schema.get("required")
    if isinstance(required, list):
        out["required"] = sorted(str(x) for x in required)
    elif isinstance(required, bool) and required and isinstance(props, dict):
        out["required"] = sorted(str(k) for k in props)

    for key in ("enum", "const"):
        if key in schema:
            out[key] = schema[key]

    for key in ("items", "additionalProperties"):
        sub = schema.get(key)
        if isinstance(sub, dict):
            out[key] = _normalize_schema(sub)
        elif isinstance(sub, bool):
            out[key] = sub

    for key in ("anyOf", "oneOf", "allOf"):
        subs = schema.get(key)
        if isinstance(subs, list):
            out[key] = [
                _normalize_schema(s) if isinstance(s, dict) else {} for s in subs
            ]

    if "default" in schema:
        out["default"] = schema["default"]
    return out


_ENV_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def env_refs(text: str) -> list[str]:
    """Environment variable names referenced by a string (``$FOO``, ``${FOO}``)."""
    return sorted(set(_ENV_RE.findall(text or "")))


def runtime_context() -> dict[str, str]:
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
    }
