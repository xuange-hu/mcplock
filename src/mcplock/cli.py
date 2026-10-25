"""``mcplock`` command line interface.

Commands
    snap      connect to a server and write a lockfile
    diff      compare two lockfiles (offline)
    verify    diff a live server against a stored lockfile (CI gate)
    risk      scan a lockfile's declared surface (offline)
    inspect   human-readable dump of a lockfile
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any, Sequence

from . import __version__
from .capture import capture
from .client import DEFAULT_TIMEOUT, McpError, build_transport
from .diff import BREAKING, NEW, RISKY, SAFE, SEVERITIES, diff_snapshots
from .report import Renderer
from .risk import SEVERITY_ORDER as RISK_SEVERITIES
from .risk import scan as risk_scan
from .snapshot import Snapshot

EXIT_OK = 0
EXIT_DRIFT = 1
EXIT_ERROR = 2

DEFAULT_LOCKFILE = "mcplock.json"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="mcplock",
        description=(
            "Capability lockfile and drift detection for MCP servers. "
            "Snapshot the tool/prompt/resource surface, classify changes as "
            "breaking / risky / new / safe, and gate CI on it."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  mcplock snap -- npx -y @modelcontextprotocol/server-everything\n"
            "  mcplock verify --lock mcplock.json -- npx -y my-server\n"
            "  mcplock diff baseline.json current.json --fail-on breaking\n"
            "  mcplock risk mcplock.json\n"
        ),
    )
    p.add_argument("--version", action="version", version=f"mcplock {__version__}")

    # Global flags live on a shared parent so they parse either side of the
    # subcommand: `mcplock --no-color diff a b` and `mcplock diff a b --no-color`
    # both work. argparse does not do this by default.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--no-color", action="store_true", help="disable ANSI output")
    common.add_argument("--color", action="store_true", help="force ANSI output")
    p.add_argument("--no-color", action="store_true", help=argparse.SUPPRESS)
    p.add_argument("--color", action="store_true", help=argparse.SUPPRESS)

    sub = p.add_subparsers(dest="subcommand", required=True)

    def add_transport(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "command",
            nargs="*",
            help="server command, after a bare -- (e.g. -- npx -y my-server)",
        )
        sp.add_argument("--url", help="Streamable HTTP endpoint instead of a command")
        sp.add_argument(
            "--env",
            action="append",
            default=[],
            metavar="K=V",
            help="extra environment variable for the server process",
        )
        sp.add_argument(
            "--header",
            action="append",
            default=[],
            metavar="K: V",
            help="extra HTTP header (repeatable)",
        )
        sp.add_argument(
            "--cwd", help="working directory for the server process"
        )
        sp.add_argument(
            "--timeout",
            type=float,
            default=DEFAULT_TIMEOUT,
            help=f"per-request timeout in seconds (default {DEFAULT_TIMEOUT})",
        )

    # -- snap --
    sp = sub.add_parser("snap", parents=[common], help="capture a server's declared surface")
    add_transport(sp)
    sp.add_argument("-o", "--out", default=DEFAULT_LOCKFILE, help="lockfile path")
    sp.add_argument(
        "--stdin-server",
        action="store_true",
        help="read a pre-recorded snapshot JSON from stdin instead of connecting",
    )
    sp.add_argument("--no-prompts", action="store_true")
    sp.add_argument("--no-resources", action="store_true")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--json", action="store_true", help="print JSON to stdout")

    # -- diff --
    sp = sub.add_parser("diff", parents=[common], help="compare two lockfiles offline")
    sp.add_argument("baseline")
    sp.add_argument("current")
    sp.add_argument(
        "--fail-on",
        choices=SEVERITIES,
        default=None,
        help="exit 1 if any change is at least this severe",
    )
    sp.add_argument(
        "--only",
        choices=SEVERITIES,
        default=None,
        help="show only changes of this exact severity",
    )
    sp.add_argument(
        "--sarif", metavar="PATH", help="also write SARIF 2.1.0 to PATH"
    )
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--json", action="store_true")

    # -- verify --
    sp = sub.add_parser("verify", parents=[common], help="check a live server against a lockfile")
    add_transport(sp)
    sp.add_argument("--lock", default=DEFAULT_LOCKFILE)
    sp.add_argument(
        "--fail-on",
        choices=SEVERITIES,
        default=BREAKING,
        help="exit 1 at or above this severity (default breaking)",
    )
    sp.add_argument("--update", action="store_true", help="rewrite the lockfile")
    sp.add_argument("--sarif", metavar="PATH")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("risk", parents=[common], help="scan a lockfile's declared surface offline")
    sp.add_argument("lockfile", nargs="?", default=DEFAULT_LOCKFILE)
    sp.add_argument(
        "--fail-on",
        choices=RISK_SEVERITIES,
        default=None,
        help="exit 1 if any finding is at least this severe",
    )
    sp.add_argument(
        "--deny-capability",
        action="append",
        default=[],
        help="treat presence of this capability as high severity (repeatable)",
    )
    sp.add_argument("--sarif", metavar="PATH")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("inspect", parents=[common], help="print a lockfile in human form")
    sp.add_argument("lockfile", nargs="?", default=DEFAULT_LOCKFILE)
    sp.add_argument("--schema", action="store_true", help="show input schemas")
    sp.add_argument("--json", action="store_true")

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    color = None
    if args.color:
        color = True
    if args.no_color and not args.color:
        color = False
    r = Renderer(color=color)

    handler = {
        "snap": _cmd_snap,
        "diff": _cmd_diff,
        "verify": _cmd_verify,
        "risk": _cmd_risk,
        "inspect": _cmd_inspect,
    }[args.subcommand]
    try:
        return handler(args, r)
    except McpError as exc:
        print(f"mcplock: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except FileNotFoundError as exc:
        print(f"mcplock: error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ValueError as exc:
        print(f"mcplock: error: {exc}", file=sys.stderr)
        return EXIT_ERROR


# -- helpers ------------------------------------------------------------------

def _load(path: str) -> Snapshot:
    with open(path, encoding="utf-8") as fh:
        return Snapshot.from_json(fh.read())


def _write_sarif(path: str, diff=None, risk=None, lockfile: str = DEFAULT_LOCKFILE) -> None:
    from . import sarif

    text = sarif.to_json(diff=diff, risk=risk, lockfile_path=lockfile)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _env_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in pairs:
        if "=" not in kv:
            raise ValueError(f"--env expects K=V, got {kv!r}")
        k, v = kv.split("=", 1)
        out[k] = v
    return out


def _header_pairs(pairs: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for kv in pairs:
        sep = ":" if ":" in kv else "="
        if sep not in kv:
            raise ValueError(f"--header expects 'Name: value', got {kv!r}")
        k, v = kv.split(sep, 1)
        out[k.strip()] = v.strip()
    return out


def _connect(args: argparse.Namespace) -> tuple[Snapshot, dict[str, Any]]:
    cmd = list(args.command) or None
    target: dict[str, Any] = {}
    transport = build_transport(
        command=cmd,
        url=args.url,
        env=_env_pairs(args.env),
        cwd=args.cwd,
        headers=_header_pairs(args.header),
        timeout=args.timeout,
    )
    if cmd:
        target = {"transport": "stdio", "command": cmd}
    else:
        target = {"transport": "http", "url": args.url}
    try:
        snap = capture(
            transport,
            target,
            include_prompts=not getattr(args, "no_prompts", False),
            include_resources=not getattr(args, "no_resources", False),
        )
    finally:
        transport.close()
    return snap, target


def _emit_json(obj: Any) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2))


# -- commands -----------------------------------------------------------------

def _cmd_snap(args: argparse.Namespace, r: Renderer) -> int:
    if args.stdin_server:
        snap = Snapshot.from_json(sys.stdin.read())
    else:
        if not args.command and not args.url:
            raise ValueError(
                "provide a server command after '--' or --url "
                "(e.g. mcplock snap -- npx -y my-server)"
            )
        snap, _ = _connect(args)

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(snap.to_json())
    if args.json:
        _emit_json(snap.to_dict())
    elif not args.quiet:
        print(r.snapshot_summary(snap))
        print(r.rule("tools"))
        print(r.tool_table(snap))
        print(r.paint(f"\nwrote {args.out}  fingerprint {snap.fingerprint}", r.paint("", "") ,))
    return EXIT_OK


def _cmd_diff(args: argparse.Namespace, r: Renderer) -> int:
    base = _load(args.baseline)
    cur = _load(args.current)
    res = diff_snapshots(base, cur)
    if args.only:
        res.changes = [c for c in res.changes if c.severity == args.only]

    if args.sarif:
        _write_sarif(args.sarif, diff=res, lockfile=args.current)
    if args.json:
        _emit_json(res.to_dict())
    elif not args.quiet:
        print(r.diff_report(res))
        if args.only:
            print(f"(filtered to {args.only})")

    if args.fail_on and res.at_least(args.fail_on):
        return EXIT_DRIFT
    return EXIT_OK


def _cmd_verify(args: argparse.Namespace, r: Renderer) -> int:
    base = _load(args.lock)
    if not args.command and not args.url:
        raise ValueError("provide a server command after '--' or --url")
    cur, _ = _connect(args)
    res = diff_snapshots(base, cur)

    if args.update:
        with open(args.lock, "w", encoding="utf-8") as fh:
            fh.write(cur.to_json())
    if args.sarif:
        _write_sarif(args.sarif, diff=res, lockfile=args.lock)
    if args.json:
        _emit_json(res.to_dict())
    elif not args.quiet:
        print(r.diff_report(res))
        print()
        if res.changes:
            print(
                r.bold(
                    f"drift: {res.counts[BREAKING]} breaking, "
                    f"{res.counts[RISKY]} risky, {res.counts[NEW]} new, "
                    f"{res.counts[SAFE]} safe"
                )
            )
            print(f"fingerprint {base.fingerprint} -> {cur.fingerprint}")
        else:
            print(r.ok(f"match: surface unchanged ({cur.fingerprint})"))
        if args.update:
            print(f"(lockfile updated: {args.lock})")

    if res.at_least(args.fail_on):
        return EXIT_DRIFT
    return EXIT_OK


def _cmd_risk(args: argparse.Namespace, r: Renderer) -> int:
    snap = _load(args.lockfile)
    rep = risk_scan(snap, fail_on_capability=args.deny_capability or None)
    if args.sarif:
        _write_sarif(args.sarif, risk=rep, lockfile=args.lockfile)
    if args.json:
        _emit_json(rep.to_dict())
    elif not args.quiet:
        print(r.snapshot_summary(snap))
        print()
        print(r.risk_report(rep))
    if args.fail_on and _risk_at_least(rep, args.fail_on):
        return EXIT_DRIFT
    return EXIT_OK


def _risk_at_least(rep, threshold: str) -> bool:
    """True if any finding is at or above ``threshold`` in severity."""
    limit = RISK_SEVERITIES.index(threshold)
    return any(RISK_SEVERITIES.index(f.severity) <= limit for f in rep.findings)


def _cmd_inspect(args: argparse.Namespace, r: Renderer) -> int:
    snap = _load(args.lockfile)
    if args.json:
        _emit_json(snap.to_dict())
        return EXIT_OK
    print(r.snapshot_summary(snap))
    if snap.tools:
        print(r.rule("tools"))
        print(r.tool_table(snap, show_schema=args.schema))
    if snap.prompts:
        print(r.rule("prompts"))
        for p in snap.prompts:
            arglist = ", ".join(
                f"{a['name']}{'' if a['required'] else '?'}" for a in p.arguments
            )
            print(f"  {p.name}({arglist})")
            if p.description:
                print(r.dim(f"      {p.description.strip()[:120]}"))
    if snap.resources or snap.resource_templates:
        print(r.rule("resources"))
        for res in snap.resources:
            print(f"  {res.uri}  {res.mime_type or ''}".rstrip())
        for res in snap.resource_templates:
            print(f"  [template] {res.uri}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
