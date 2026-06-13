# mcp-auth-relay — Claude Context

Generic MCP relay that sits between your MCP client and a local MCP server.
Injects a bearer token, serves tools/list from a manifest, and loads integration packs.

---

## What this is

Two implementations of the same relay — use whichever fits your deployment:

| File | Runtime | Use when |
|---|---|---|
| `python/proxy.py` | Python 3.10+ | Dev/testing, any OS |
| `cpp/src/Main.cpp` | C++17, CMake | Production, no Python dependency |

Architecture:
```
MCP client (Claude Code) → mcp-auth-relay (port 8089) → upstream MCP server (port 8088)
```

The relay is transparent — the client never sees or sends the bearer token.

---

## Config (`config.json`)

Lives next to the script/binary. Copy `config.example.json` to get started.

```json
{
  "bearer_token":  "your-secret-token",
  "proxy_port":    8089,
  "upstream_host": "127.0.0.1",
  "upstream_port": 8088,
  "manifest_path": "C:/path/to/tools-manifest.json",
  "integration":   "vibeue",
  "server_name":   "mcp-auth-relay",
  "instructions":  ""
}
```

- `manifest_path` — JSON array of MCP tool definitions written by the upstream server. Served at tools/list even when upstream is offline.
- `integration` — name of a subfolder in `integrations/` (see Integration packs below).
- `instructions` — agent instructions injected into `initialize` serverInfo. If blank, loaded from integration pack's `instructions.md`.

**config.json is gitignored** — it contains secrets. Never commit it.

---

## Running

**Python:**
```bash
cd python
python proxy.py
```

**C++ (build first):**
```bash
cd cpp
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
# Binary lands in bin/
bin/mcp-auth-relay.exe   # Windows
bin/mcp-auth-relay       # Linux/Mac
```

C++ binary reads `config.json` from the same directory as the executable, not `cpp/`. Copy or symlink accordingly.

**Verify it's up:**
```bash
curl http://127.0.0.1:8089/mcp
# → "mcp-auth-relay running"
```

---

## Integration packs

A pack is a folder under `integrations/<name>/` that customises the relay for a specific upstream server:

```
integrations/
  vibeue/
    config.example.json    ← pre-filled config for this integration
    hints.json             ← {"tool_name": " — hint appended to description"}
    synthetic_tools.json   ← extra tools handled by the proxy, not forwarded
    instructions.md        ← agent-facing routing/setup guide
    README.md              ← human-facing setup steps
```

Set `"integration": "vibeue"` in `config.json` to activate a pack. The relay loads it at startup — no restart needed after editing hints or synthetic tools (restart is needed for instructions since they're sent at initialize time).

Integration packs live in the companion repo: `exetorius/mcp-auth-relay-integrations`.
Clone it alongside this repo, then symlink or copy the pack folder you need into `integrations/`.

---

## MCP client config (Claude Code)

Add to `.mcp.json` in your project root:
```json
{
  "mcpServers": {
    "mcp-auth-relay": {
      "type": "http",
      "url": "http://127.0.0.1:8089/mcp"
    }
  }
}
```

---

## Code structure

```
python/proxy.py          — single-file Python relay
cpp/
  CMakeLists.txt         — fetches httplib + nlohmann/json via FetchContent
  src/Main.cpp           — single-file C++ relay
  build/                 — gitignored CMake build dir
bin/                     — gitignored compiled binary output
integrations/            — gitignored at root level; add specific packs as needed
config.example.json      — template, safe to commit
config.json              — gitignored, contains secrets
```

---

## Key design decisions

- **No HTTPS** — relay only binds to localhost. SSL would add libssl/zlib deps for no benefit.
- **New connection per call** — avoids stale keep-alive sockets between tool calls.
- **SSE heartbeat** — GET /mcp returns an event-stream that keeps Claude Code from reconnecting in a tight loop (log flooding fix).
- **initialize answered by proxy** — upstream never sees it; protocol version is echoed back exactly as sent so Claude Code's version check passes regardless of upstream.
- **tools/list from manifest** — upstream can be offline; the manifest (written by the upstream server on startup) is always served.
- **Synthetic tools** — loaded from integration pack, answered by proxy, never forwarded. Useful for status checks or setup wizards.

---

## What NOT to do

- Do not hardcode any upstream server name, path, or URL into the core relay files.
- Do not add VibeUE-specific logic to `proxy.py` or `Main.cpp` — that belongs in the `integrations/vibeue/` pack.
- Do not add SSL/TLS — not needed for localhost, adds complexity and deps.
- Do not commit `config.json`.
