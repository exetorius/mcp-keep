# mcp-auth-relay

A lightweight relay that sits between your MCP client and a local MCP server, injecting bearer token authentication so your client never has to handle it.

```
MCP client (Claude Code) → mcp-auth-relay :8089 → upstream MCP server :8088
```

## Why

Some MCP servers require a bearer token for every request. Most MCP clients (including Claude Code) don't have a clean way to inject per-server auth headers. This relay handles it transparently — you configure the token once in `config.json` and forget about it.

It also serves `tools/list` from a local manifest file, so your client sees the full tool list even when the upstream server hasn't started yet.

## Quick Start

**Python** (no dependencies beyond stdlib):
```bash
git clone https://github.com/exetorius/mcp-auth-relay
cd mcp-auth-relay/python
cp ../config.example.json config.json
# edit config.json — set bearer_token and upstream_port
python proxy.py
```

**C++** (CMake, fetches httplib + nlohmann/json automatically):
```bash
git clone https://github.com/exetorius/mcp-auth-relay
cd mcp-auth-relay/cpp
cmake -B build -S . -DCMAKE_BUILD_TYPE=Release
cmake --build build --config Release
cp ../config.example.json bin/config.json
# edit bin/config.json — set bearer_token and upstream_port
bin/mcp-auth-relay      # Linux/Mac
bin/mcp-auth-relay.exe  # Windows
```

Point your MCP client at `http://127.0.0.1:8089/mcp`.

## Configuration

Copy `config.example.json` to `config.json` (next to the script or binary) and fill in your values:

```json
{
  "bearer_token":  "your-secret-token",
  "proxy_port":    8089,
  "upstream_host": "127.0.0.1",
  "upstream_port": 8088,
  "manifest_path": "/path/to/tools-manifest.json",
  "integration":   "",
  "server_name":   "mcp-auth-relay",
  "instructions":  ""
}
```

| Key | Description |
|-----|-------------|
| `bearer_token` | Injected as `Authorization: Bearer <token>` on every upstream request |
| `proxy_port` | Port the relay listens on (default 8089) |
| `upstream_host` | Upstream MCP server host (default 127.0.0.1) |
| `upstream_port` | Upstream MCP server port (default 8088) |
| `manifest_path` | Path to a JSON array of tool definitions — served at `tools/list` even when upstream is offline. On Windows, `%APPDATA%` is expanded. |
| `integration` | Name of an integration pack to load from the `integrations/` folder |
| `server_name` | Name reported in MCP `initialize` response |
| `instructions` | Agent instructions injected into `initialize` serverInfo. If blank, loaded from integration pack. |

`config.json` is gitignored — never commit it.

## Claude Code Setup

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

## Integration Packs

Integration packs add server-specific behaviour without touching the relay code — tool hints, synthetic tools, and agent instructions for a specific upstream server.

Install a pack while the relay is running (Python only):
```
/packs
```

This fetches the available pack list from [mcp-auth-relay-integrations](https://github.com/exetorius/mcp-auth-relay-integrations), lets you pick one, and downloads it into `integrations/<name>/`. Your `config.json` is updated automatically.

Other commands:
```
/status  — show current config and integration state
/reload  — reload config and integration without restarting
/quit    — stop the relay
```

## How It Works

- `initialize` — answered directly by the relay. Protocol version is echoed back exactly as sent.
- `tools/list` — served from the local manifest file + any synthetic tools from the integration pack. Upstream not required.
- `tools/call` and everything else — forwarded to upstream with the bearer token injected.
- SSE heartbeat — a persistent GET `/mcp` stream sends a comment every 15 seconds to prevent MCP clients from reconnecting in a tight loop.
- New connection per call — avoids stale keep-alive socket errors between tool calls.

## Contributing

PRs are welcome. Please target the `contrib` branch — not `main`. Main is locked to the maintainers.

## Repository Layout

```
python/proxy.py       — Python relay (single file, stdlib only)
cpp/
  CMakeLists.txt      — build config, fetches deps via FetchContent
  src/Main.cpp        — C++ relay (single file)
  build/              — gitignored CMake build dir
bin/                  — gitignored compiled binary output
integrations/         — gitignored, populated by /packs or manual clone
config.example.json   — template, safe to commit
config.json           — gitignored, contains secrets
```
