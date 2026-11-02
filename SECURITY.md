# Security policy

## Scope

**mcplock** — Capability lockfile and drift detection for MCP servers — is a static analysis tool: it reads **MCP server tool manifests, captured over stdio or HTTP, stored as a lockfile** and prints a report.
The security questions that apply to it are the ones that apply to any tool
that parses untrusted input.

Supported: the current release.

## What this tool does with your files

Nothing leaves your machine. There is no network client in the scanning path:
the tool opens the files you point it at, parses them, and writes a report.
It never writes to the files it reads.

## Reporting a vulnerability in mcplock

Use [GitHub private vulnerability reporting](https://github.com/xuange-hu/mcplock/security/advisories)
on the Security tab. If private reporting is unavailable to you, open an issue
marked `security`.

Include:

1. the version (`mcplock --version`);
2. the input that triggers it — the actual file is preferred over a description;
3. what you expected and what happened.

For a parse failure, the smallest input that reproduces it is the whole report.

## What counts as a vulnerability here

- **A drift that is reported as no drift.** The entire promise is that a changed tool set does not pass silently.
- **A runner failure reported as drift.** If the server could not start, that is an infrastructure error, not a capability change; confusing the two makes people ignore the gate.
- **A transport that leaks credentials.** The lockfile can contain server configuration.
- **A path that escapes the directory it was pointed at.**

## What does not count

- A rule firing on input you consider correct. That is a false positive: open
  an issue with the sample and it gets a regression test.
- A rule you want added. Open an issue or a PR.

## Timing

I triage within a week and will confirm receipt. If a fix needs to sit in a
branch for a while, the advisory goes out when the fix lands, not before.
