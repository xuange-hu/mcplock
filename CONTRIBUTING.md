# Contributing

**mcplock** — Capability lockfile and drift detection for MCP servers — is small on purpose: hand-written parsers, deterministic rules, no dependencies. Contributions that keep it that way are welcome.

## Setup

```bash
git clone https://github.com/xuange-hu/mcplock
cd mcplock
pip install -e ".[mcp]" pytest ruff
pytest                 # 199 tests
ruff check src tests
```

No `pip install` is needed to run the tests after a clone — the root
`conftest.py` puts `src/` on `sys.path`.

## Rules and where they live

`src/mcplock/diff.py` (drift) and `src/mcplock/risk.py` (what each change means).

## Adding a rule

1. **The classification** in `src/mcplock/risk.py` — say what the change means, not just that something changed.
2. **The exit-code semantics**: 0 clean, 1 drift, 2 the run could not be trusted. Getting these wrong is how a gate gets bypassed.
3. **An end-to-end test** with a stub server, not a unit test of the diff function alone.

Then update the rule table and the test-count badge in `README.md`. CI warns
when the badge and pytest disagree.

## Rules of the road

- **Zero dependencies in `src/` outside `mcp_server.py`.** `tests/test_packaging.py`
  fails on an optional import leaking into a required path.
- **No network access.** Not in the scanner, not in the tests. Tests that need
  a file write it to `tmp_path`.
- **Every finding carries a location and its source text.** A report that says
  "something is wrong" without saying where gets closed and forgotten.
- **`absent` and `empty` are different.** Decide what the explicit empty form
  means before adding a rule that fires on a missing field.
- **Line numbers come from the parser.** Do not compute them in the rule.

## Commit and PR

- One logical change per commit.
- CI runs on 3.10–3.13 across Ubuntu and Windows. A test that passes only on
  your machine will be caught there, so run `pytest` before pushing.
