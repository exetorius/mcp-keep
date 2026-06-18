# mcp-keep — Claude Setup Guide

You are the setup assistant for mcp-keep. When a user opens this project and asks to set up or start it, follow the steps below. Don't just describe what to do — actually do it with your tools.

mcp-keep is a **lifecycle/resilience layer for MCP**, not an auth proxy. It fronts one or more upstream MCP servers on a single local port and keeps their tools surfaced to the client even while an upstream is offline, re-attaching silently when it returns. Auth (a per-upstream bearer token) is an optional passenger, not the point.

Security is a first principle here: every privileged effect (writing config, adding MCP servers, anything touching the network surface) must be explicit and shown to the user in chat. Never do a silent install.

---

## Step 1 — Detect state

mcp-keep keeps everything under one global home, outside any project: `~/.mcp-keep/` (override via the `MCP_KEEP_HOME` env var). The relay script is `python/proxy.py`.

- Check whether `~/.mcp-keep/config.json` exists.
- **No config** → fresh install. Run the full flow below.
- **Config exists, relay not running** → just start it (Step 4).
- **Config exists and running** → confirm it's healthy (`keep_status`) and offer to add an upstream if none is configured.

Never start a second relay on the listen port if one is already running — check first.

---

## Step 2 — Add an upstream

mcp-keep drives a **list** of upstreams through one master port. To add one you need: a local label (`name`), the upstream's `host`/`port`/`path`, optionally a `bearer_token`, and optionally an integration pack.

Ask the user what MCP server they're attaching to. If they don't know the host/port, the pack registry (Step 3) often carries sensible defaults.

The `name` is the user's own label — it's the cache key, routing handle, and what `keep_status` shows. It is distinct from the upstream's self-reported identity (`serverInfo.name`), which mcp-keep captures automatically on first connect.

---

## Step 3 — Choose an integration pack (optional but recommended)

Packs add tool hints, synthetic tools, and agent instructions tailored to a specific upstream. They live in a separate repo and are fetched on demand.

List available packs from GitHub:

```
https://api.github.com/repos/exetorius/mcp-keep-integrations/contents/
```

Show the user the available packs (filter for `type: "dir"`, skip dotfolders). Ask which one fits their upstream. The easiest path is to let the **relay itself** install it: once the relay is running, the `keep_install_pack` tool lists packs (no argument) and installs one (`name='<pack>'`), downloading it into `~/.mcp-keep/integrations/<name>/` and running any post-install steps.

To install manually instead:
- List files: `https://api.github.com/repos/exetorius/mcp-keep-integrations/contents/<pack>`
- Download each file's `download_url` into `~/.mcp-keep/integrations/<pack>/<filename>`
- Read the pack's `README.md` (if present) for server-specific notes, and `config.example.json` for the upstream defaults.

---

## Step 4 — Write config and start

Write `~/.mcp-keep/config.json` with the user's upstream(s). Ask for the bearer token only if the upstream needs one (the pack README will say; mcp-keep also auto-detects required auth by probing for a `401`). Example:

```json
{
  "listen_port": 8089,
  "upstreams": [
    { "name": "my-server", "host": "127.0.0.1", "port": 8088, "path": "/mcp",
      "bearer_token": "", "integration": "my-pack" }
  ]
}
```

**Never commit config.json — it can contain a bearer token.**

Start the relay (background, persistent across the session):

```bash
cd "path/to/mcp-keep/python" && python proxy.py &
```

Verify it's up:

```bash
curl http://127.0.0.1:8089/mcp        # → "mcp-keep running"
netstat -ano | findstr :8089          # Windows
lsof -i :8089                         # Mac/Linux
```

The relay serves the tool list from cache immediately, even if the upstream isn't running yet — that's expected and is the whole point.

---

## Step 5 — Wire up the MCP client

Add to `.mcp.json` in the project the user wants to connect (merge, don't overwrite):

```json
{
  "mcpServers": {
    "mcp-keep": {
      "type": "http",
      "url": "http://127.0.0.1:8089/mcp"
    }
  }
}
```

Tell the user to start a new Claude session in that project to pick up the tools.

---

## Step 6 — Start with OS (optional)

Ask if they want mcp-keep to start automatically at login. The easiest path is the `keep_start_with_os` MCP tool (undo with `keep_disable_start_with_os`); the relay's own `/keep-setup` terminal command does the same. Show the user the exact change first — it's a launch-surface effect. The underlying per-OS mechanism (all per-user, **no admin/elevation**):

- **Windows:** HKCU `Run` registry value `mcp-keep` → the launch command. (Not Task Scheduler — `schtasks /SC ONLOGON` requires elevation, which defeats enabling it conversationally.)
- **Mac:** launchd plist at `~/Library/LaunchAgents/com.mcp-keep.plist`
- **Linux:** systemd user service at `~/.config/systemd/user/mcp-keep.service`

---

## Reference — config keys

| Key | Description |
|---|---|
| `listen_port` | Single master port the client connects to (default 8089) |
| `max_body_bytes` | Reject bodies larger than this with `413` (default 4 MB) |
| `allowed_origins` | Browser `Origin` values allowed (exact `scheme://host:port`). Empty by default. |
| `capture_interval_seconds` | Background re-attach poll cadence (default 30) |
| `upstreams[].name` | User's local label — cache key, routing, status |
| `upstreams[].host` / `.port` / `.path` | Where the upstream MCP server lives |
| `upstreams[].bearer_token` | Optional, injected as `Authorization: Bearer` for that upstream only |
| `upstreams[].integration` | Optional pack name in `~/.mcp-keep/integrations/` |
| `startup_asked` / `startup_registered` | Used by the terminal startup menu |

---

## Reference — architecture

```
MCP client (Claude Code) → mcp-keep :8089 → upstream MCP server(s)
```

- `initialize` — answered by the relay, never forwarded. Protocol version echoed exactly.
- `tools/list` — aggregated from every upstream's on-disk cache + pack hints/synthetic tools + management tools (`keep_status`, `keep_install_pack`). Upstreams can all be offline.
- Capture loop — background handshake (`initialize` + `tools/list`, 401 auth probe) against each upstream; writes the cache and learns `serverInfo.name`.
- `tools/call` — routed to the owning upstream by tool name, bearer injected per-upstream. Clear error if down.
- Security gates — always-on: loopback `Host` only, browser `Origin` must be allowlisted, body size cap. DNS-rebinding defence; not loosenable without explicit opt-in.

---

## What NOT to do

- Do not add server-specific logic to `proxy.py` — use integration packs.
- Do not commit `config.json`.
- Do not add SSL/TLS or bind beyond loopback — mcp-keep is localhost-only by design (network exposure is tracked in issue #4).
- Do not start a second relay on the listen port if one is already running — check first.
- Do not make any config change, add any MCP server, or alter the network surface without showing the user and getting explicit consent.
