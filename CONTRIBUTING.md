# Contributing to mcp-keep

Thanks for helping out. `mcp-keep` is a small, single-file relay with a security-first design — contributions are very welcome, and the bar is "keep it simple, keep it safe."

## Project shape

```
python/proxy.py        — the relay (single file, stdlib only — no dependencies)
config.example.json    — config template, safe to commit
tests/integration_test.py — end-to-end test (onboarding + the moat) against a fake upstream
tests/smoke_binary.py  — boots a PyInstaller-built binary and asserts it serves
.github/workflows/     — ci.yml (tests) + release.yml (packaged binaries)
CLAUDE.md              — setup-assistant guide for Claude Code (clone path)
FIRST_TIME_SETUP.md    — AI bootstrap guide that ships with the binary
```

Everything the relay does at runtime lives under a single global home, `~/.mcp-keep/` (override with the `MCP_KEEP_HOME` environment variable). Nothing is written into the repo or a project.

## Run from source

Python 3.10+ — **standard library only, no dependencies.**

```bash
git clone https://github.com/exetorius/mcp-keep
cd mcp-keep/python
python proxy.py --serve
```

> `--serve` is required to actually run the relay (#56). A bare `python proxy.py` (or any unknown arg) prints a "your MCP client starts me" notice and exits without binding a port — by design, so a stray invocation never spawns a hidden relay.

Use an isolated home while developing so you never touch a real config — and so a
dev relay can't collide with a running prod one (#22). The easiest way is `--dev`,
which uses `~/.mcp-keep-dev` and port **8090** instead of `~/.mcp-keep` / `:8089`:

```bash
python proxy.py --serve --dev
```

`--dev` is equivalent to `MCP_KEEP_DEV=1`. An explicit `MCP_KEEP_HOME=/path` still
overrides the home if you want a throwaway dir (e.g. `MCP_KEEP_HOME=/tmp/keep-dev`).

## Tests

Both tests are stdlib-only and headless — no network, no fixtures to install.

```bash
# End-to-end: onboarding tools + the moat (cache-when-down, re-attach, routing)
python tests/integration_test.py

# Optional: build a binary and smoke-test the frozen artifact
pip install pyinstaller
# Windows: builds two binaries (mcp-keep-relay.exe + mcp-keep-watchdog.exe) sharing _internal/
pyinstaller mcp-keep.spec
python tests/smoke_binary.py "dist/mcp-keep/mcp-keep-relay.exe"
# macOS/Linux: single console binary
pyinstaller --onedir --name mcp-keep --console python/proxy.py
python tests/smoke_binary.py "dist/mcp-keep/mcp-keep"
```

CI runs `py_compile` + `integration_test.py` on Windows, macOS, and Linux for every push/PR to `main` and `contrib`. The release workflow additionally builds and smoke-tests the binaries.

## Branch & PR flow

**Do not push to `main` directly.** The flow is:

1. Branch from `main` (e.g. `feat/…`, `fix/…`, `ci/…`, `docs/…`).
2. Open a PR into **`contrib`**.
3. CI runs across all three OSes; merge once green.
4. `contrib` is promoted to `main` via its own PR once changes are validated together.

Pack contributions don't go here at all — they live in the separate [mcp-keep-integrations](https://github.com/exetorius/mcp-keep-integrations) repo, fetched on demand by the relay.

## Design guardrails

These are deliberate; please keep them in mind (and raise an issue first if you want to challenge one):

- **No server-specific logic in `proxy.py`.** Anything tailored to a particular upstream belongs in an integration pack, not the core relay.
- **Loopback only.** `mcp-keep` binds to `127.0.0.1` and is not loosenable to a bare hostname/LAN bind without a real opt-in flow ([issue #4](https://github.com/exetorius/mcp-keep/issues/4)). No SSL/TLS — it's localhost by design.
- **Security gates stay always-on.** Loopback `Host` check, `Origin` allowlist, and body-size cap are zero-config and not removable. They defend against DNS-rebinding.
- **No silent privileged effects.** Anything that writes config, adds an upstream, or touches the network surface must be explicit and visible to the user.
- **Stdlib only.** The relay has no third-party runtime dependencies — please keep it that way so the binary stays a single self-contained file.

## Filing issues

Spotted an improvement or a sharp edge in passing? File an issue — we'd rather track it than lose it. The repo uses `security`, `reliability`, and `design` labels.
