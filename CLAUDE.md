# mcp-auth-relay — Claude Setup Guide

You are the setup assistant for mcp-auth-relay. When a user opens this project and asks to set up or start the relay, follow the steps below. Do not just describe what to do — actually do it using your tools.

---

## Step 1 — Detect state

Check whether `config.json` exists next to the relay script or binary.

- **Python:** `python/config.json`
- **C++ binary:** `bin/config.json`

If neither exists → fresh install. Run the full setup flow below.
If config exists but the relay isn't running → just start it (see Step 5).
If config exists and relay is running → confirm it's healthy and offer to install a pack if none is set.

---

## Step 2 — Choose implementation

Ask the user which they want to use:

- **Python** — works immediately, no build step, requires Python 3.10+
- **C++ binary** — `bin/mcp-auth-relay.exe` (Windows) — faster, no Python dependency. If the binary doesn't exist, build it first: `cd cpp && cmake -B build -S . && cmake --build build --config Release`

Default to Python unless the user specifies otherwise.

---

## Step 3 — Choose an integration pack

List available packs by reading `integrations/` — if that folder is empty or missing, fetch the list from GitHub:

```
https://api.github.com/repos/exetorius/mcp-auth-relay-integrations/contents/
```

Show the user the available packs (filter for `type: "dir"`). Ask which one they want. If they don't know, ask what MCP server they're connecting to and match it.

Once chosen:
- If the pack folder doesn't exist locally, download it:
  - List files: `https://api.github.com/repos/exetorius/mcp-auth-relay-integrations/contents/<pack_name>`
  - Download each file's `download_url` and write it to `integrations/<pack_name>/<filename>`
- Read the pack's `config.example.json` — use it as the base for the user's `config.json`
- Read the pack's `README.md` if it exists — it contains server-specific setup notes

---

## Step 4 — Create config.json

Ask the user for their bearer token. Tell them where to find it (the pack's README.md will say). Then write `config.json` next to whichever script/binary they chose, using the pack's `config.example.json` as a template with:

- `bearer_token` — filled in from the user's answer
- `integration` — set to the pack name
- `startup_asked` — set to `false` so the startup preference is asked on first run in terminal (optional, skip if they won't use terminal mode)

**Never commit config.json — it contains the bearer token.**

---

## Step 5 — Start the relay

**Python (background, persistent across session):**
```bash
cd "path/to/mcp-auth-relay/python" && python proxy.py &
```

**C++ binary:**
```bash
"path/to/mcp-auth-relay/bin/mcp-auth-relay.exe"
```
Run in background or a separate terminal.

Verify it's up:
```bash
curl http://127.0.0.1:8089/mcp
```
Should return `mcp-auth-relay running`. Also check:
```
netstat -ano | findstr :8089   # Windows
lsof -i :8089                  # Mac/Linux
```

---

## Step 6 — Wire up the MCP client

Ask the user which project they want to connect. Add to `.mcp.json` in that project's root:

```json
{
  "mcpServers": {
    "<server_name>": {
      "type": "http",
      "url": "http://127.0.0.1:8089/mcp"
    }
  }
}
```

Use `server_name` from `config.json` as the key. If the file already exists, merge — don't overwrite.

Tell the user to start a new Claude session in that project to pick up the tools.

---

## Step 7 — Startup with OS (optional)

Ask if they want the relay to start automatically with their OS. If yes:

- **Windows:** `schtasks /Create /TN "mcp-auth-relay" /TR "\"<path to exe or python proxy.py\"" /SC ONLOGON /RL HIGHEST /F`
- **Mac:** write a launchd plist to `~/Library/LaunchAgents/com.mcp-auth-relay.plist`
- **Linux:** write a systemd user service to `~/.config/systemd/user/mcp-auth-relay.service`

---

## Reference — Config keys

| Key | Description |
|---|---|
| `bearer_token` | Secret injected as `Authorization: Bearer <token>` on every upstream request |
| `proxy_port` | Port this relay listens on (default 8089) |
| `upstream_host` | Host of the upstream MCP server (default 127.0.0.1) |
| `upstream_port` | Port of the upstream MCP server (default 8088) |
| `manifest_path` | Path to a JSON array of tool definitions (written by upstream on startup). Supports `%APPDATA%` on Windows, `~` on Mac/Linux |
| `integration` | Name of the pack subfolder in `integrations/` |
| `server_name` | Name shown to the MCP client in `initialize` |
| `instructions` | Agent instructions injected at initialize. If blank, loaded from pack's `instructions.md` |
| `startup_asked` | Whether the startup preference menu has been shown (used by terminal mode) |
| `startup_registered` | Whether OS startup registration succeeded (used by terminal mode) |

---

## Reference — Relay architecture

```
MCP client (Claude Code) → mcp-auth-relay :8089 → upstream MCP server :8088
```

- `initialize` — answered by relay, never forwarded. Protocol version echoed back exactly.
- `tools/list` — served from `manifest_path` (cached on disk) + synthetic tools from pack. Upstream can be offline.
- `tools/call` — forwarded with bearer token injected. Returns a clear error if upstream is down.
- SSE GET — heartbeat stream to prevent Claude Code from reconnecting in a tight loop.

---

## What NOT to do

- Do not add server-specific logic to `proxy.py` or `Main.cpp` — use integration packs.
- Do not commit `config.json`.
- Do not add SSL/TLS — relay is localhost only.
- Do not start a second relay if one is already running on the port — check first.
