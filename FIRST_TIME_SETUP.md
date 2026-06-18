# mcp-keep — First-Time Setup

**If a human asked you (an AI assistant) to "set up keep" or "read the first-time setup file and set it up," this file is your instructions. Do the steps — don't just describe them. Show the human every privileged change before you make it; never do a silent install.**

---

## What mcp-keep is

mcp-keep is a **lifecycle/resilience layer for MCP**. It fronts one or more upstream MCP servers on a single local port (default `8089`) and keeps their tools surfaced to this client **even while an upstream is offline**, re-attaching silently when it returns. It is not an auth proxy — a per-upstream bearer token is an optional extra, not the point.

It does nothing on its own: it needs an AI MCP client (you) connected to it. Your job here is to get that connection made, then let mcp-keep guide the rest.

---

## Step 1 — Start the relay (detached, so it outlives this chat)

In the folder this file came in, find the mcp-keep program:

- A standalone binary: `mcp-keep` (macOS/Linux) or `mcp-keep.exe` (Windows), **or**
- the script `python/proxy.py` (run with `python proxy.py`) if this is a source checkout.

**Launch it detached.** This matters: mcp-keep is a long-running server, so if you (the AI) start it as an ordinary foreground command, your client may treat it as a *managed background task* and **kill it the moment this session ends** — the relay won't survive a restart. Start it so it's orphaned from your session instead:

- **Windows:** `start "" mcp-keep.exe`  (or PowerShell: `Start-Process .\mcp-keep.exe`)
- **macOS / Linux binary:** `./mcp-keep &`  (or `nohup ./mcp-keep >/dev/null 2>&1 &`)
- **Source checkout:** `python proxy.py &`

It will:
- create its home at `~/.mcp-keep/` (override with the `MCP_KEEP_HOME` env var),
- listen on `http://127.0.0.1:8089/mcp`,
- run fine with **zero upstreams configured** — that's expected on first run.

Confirm it's up — **poll, don't fixed-sleep-then-give-up:**

```
curl http://127.0.0.1:8089/mcp        # → "mcp-keep running"
```

The **first** launch of a freshly downloaded binary can take several seconds to start listening (the OS may scan the new executable, and a bundled binary unpacks its runtime on first run). So **retry the probe for ~15–20s** (e.g. every 0.5s) until it returns `mcp-keep running`. **Do not conclude "it didn't start" after a single probe, and do not relaunch it** — relaunching while the first instance is still coming up leaves you with two processes racing for the port. A slow first start is expected, not a failure.

If port 8089 is genuinely taken by something else, check whether mcp-keep is already running before starting another one — never start a second relay on the same port.

> **Want it always running** (across reboots, with no relaunch)? That's the durable path — set up Start-with-OS. The relay's own `/keep-setup` terminal command registers it (Windows scheduled task / macOS launchd / Linux systemd user service). A detached launch survives until the machine is rebooted; Start-with-OS survives that too.

---

## Step 2 — Wire THIS client to the relay (the one manual bootstrap)

This is the step that can only happen outside chat: your client needs a pointer to the relay before it can see mcp-keep's tools. Add an MCP server entry pointing at `http://127.0.0.1:8089/mcp`, using **whatever config mechanism your client uses**. Show the human the change first.

- **Claude Code** — merge into the project's `.mcp.json` (don't overwrite it):

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

- **Other clients (Claude Desktop, Cursor, etc.)** — add an HTTP/remote MCP server pointing at the same URL, in that client's own config file/format. Use the method documented for your client.

**First-time bootstrap tip — add the upstream *before* this reload.** On a brand-new install there's no connected client yet, so `keep_add_upstream` (an MCP tool) isn't callable — this is exactly the "fall back to editing `config.json`" case (see Reference below). Ask the human now for their upstream's `host`/`port`/`path` (and bearer if needed) and write it into `~/.mcp-keep/config.json` *before* you reload. Why: the client handshakes its tool list **once per session**, so a single reload here surfaces both the `keep_*` tools **and** the fronted upstream's tools together. If you skip this and add the upstream afterward via `keep_add_upstream`, you'll need a **second reload** before the fronted tools appear. (For best results have the upstream reachable so it captures first — on a fresh home there's no cache until the first successful capture.) Use `keep_add_upstream` for adding *further* upstreams later, to an already-connected client.

Then tell the human to **reload their AI client** so it picks up mcp-keep — in Claude Code, run `/mcp` and reconnect the `mcp-keep` server (a full restart also works).

**This reload is required, not optional.** If the client was already running when you started the relay, its first attempt to reach mcp-keep failed (the relay wasn't up yet) and it will **not** re-handshake on its own mid-session — so `keep_*` won't appear as callable tools until you reload. Don't work around this with raw HTTP/`curl`; just reload, then drive everything through the tools. It's a one-time bootstrap step: with **Start-with-OS** (Step 1) the relay is always up *before* any session starts, so the tools surface with no reload at all.

---

## Step 3 — Connect and finish setup in chat

After the reload, mcp-keep's tools are available to you. From here, **everything happens through the relay's own tools — you should not hand-edit config files** (the one exception was the bootstrap upstream in Step 2, before any tool was callable).

**If you already wrote the upstream into `config.json` during Step 2's bootstrap tip:** just call **`keep_status`** to confirm it attached and see the cached tool count — you're done.

Otherwise, add it now through the tools:

1. Call **`keep_welcome`** — it returns guided onboarding (only appears while no upstream is configured).
2. Ask the human which MCP server they want to attach, and for its **host / port / path** (e.g. `127.0.0.1:8088/mcp`). If they don't know, call **`keep_install_pack`** with no arguments to list integration packs — these often carry sensible host/port defaults for known servers.
3. Ask whether it needs auth (a bearer token). If unsure, add it without one — mcp-keep auto-detects required auth by probing for a `401`.
4. Confirm the details back to the human, then call **`keep_add_upstream`** with `name` (their label) plus `host` / `port` / `path` (and `bearer_token` / `integration` if relevant). Note: tools fronted by a *newly* added upstream may need one more client reload to surface (the tool list handshakes once per session).
5. Call **`keep_status`** to confirm it attached and see the cached tool count.

That's it. mcp-keep will now keep that server's tools surfaced to you even when it's offline.

---

## Reference — config shape (for the rare case you must edit by hand)

Prefer `keep_add_upstream`. Only fall back to editing `~/.mcp-keep/config.json` directly if a tool isn't available. **Never commit this file — it can contain a bearer token.**

```json
{
  "listen_port": 8089,
  "upstreams": [
    { "name": "my-server", "host": "127.0.0.1", "port": 8088, "path": "/mcp",
      "bearer_token": "", "integration": "" }
  ]
}
```

| Upstream key | Meaning |
|---|---|
| `name` | The human's own label — cache key, routing handle, what `keep_status` shows |
| `host` / `port` / `path` | Where the upstream MCP server lives |
| `bearer_token` | Optional; injected as `Authorization: Bearer` for that upstream only |
| `integration` | Optional pack name in `~/.mcp-keep/integrations/` |

---

## Rules

- Show the human every privileged change (config writes, adding a server, wiring the client) before doing it. No silent installs.
- Don't start a second relay on the listen port if one is already running.
- mcp-keep is loopback-only by design — don't try to bind it beyond `127.0.0.1`.
