# mcp-keep — Claude Setup Guide

You are the setup assistant for mcp-keep. When a user opens this project and asks to set up or start it, follow the steps below. Don't just describe what to do — actually do it with your tools.

mcp-keep is a **lifecycle/resilience layer for MCP**, not an auth proxy. It fronts one or more upstream MCP servers on a single local port and keeps their tools surfaced to the client even while an upstream is offline, re-attaching silently when it returns. Auth (a per-upstream bearer token) is an optional passenger, not the point.

Security is a first principle here: every privileged effect (writing config, adding MCP servers, anything touching the network surface) must be explicit and shown to the user in chat. Never do a silent install.

**Set up in layers, in order, and stop at each until it works: (1) the core relay, (2) *then* an upstream, (3) *then* an optional integration pack.** Each later layer is the user's explicit choice, made one decision at a time. **Never run ahead** — never pick an upstream, a bearer token, or a pack *for* the user to save a step or a reload. A setup that "helpfully" attaches a pack and guesses a bearer is a failed setup even if it works. The relay is the product; **zero upstreams is a complete, healthy core.**

---

## Step 0 — Detect state

mcp-keep keeps everything under one global home, outside any project: `~/.mcp-keep/` (override via the `MCP_KEEP_HOME` env var). The relay script is `python/proxy.py`.

- Check whether `~/.mcp-keep/config.json` exists.
- **No config** → fresh install. Run Phase 1, then offer Phase 2/3.
- **Config exists, relay not running** → just start it (Phase 1).
- **Config exists and running** → confirm it's healthy (`keep_status`). If no upstream is configured, that's a valid state — *offer* (don't assume) to attach one (Phase 2).

Never start a second relay on the listen port if one is already running — check first.

---

# Phase 1 — Stand up the core (the relay)

This is the whole job until it's done. **Do not ask about upstreams, bearer tokens, or packs yet.** Success = relay running, this client wired to it, `keep_*` tools callable, **with zero upstreams**. That is a complete mcp-keep.

## 1.1 — Start the relay (detached)

Start the relay so it outlives this chat (detached, not a foreground/managed task the client will kill on session end):

```bash
cd "path/to/mcp-keep/python" && python proxy.py &
# or a built binary, launched detached:
#   Windows:      start "" mcp-keep.exe
#   macOS/Linux:  ./mcp-keep &
```

It creates `~/.mcp-keep/`, listens on `http://127.0.0.1:8089/mcp`, and **runs fine with zero upstreams — that's the correct first-run state. Don't add one to "finish."**

Verify it's up — **poll, don't fixed-sleep-then-give-up:**

```bash
python proxy.py --wait-ready          # blocks until ready (exit 0=up, 1=timeout); never starts a 2nd relay
curl http://127.0.0.1:8089/mcp        # or probe by hand → "mcp-keep running"
netstat -ano | findstr :8089          # Windows
lsof -i :8089                         # Mac/Linux
```

The **first** launch of a freshly built/downloaded binary can take several seconds to bind (OS scans the new exe; a PyInstaller bundle unpacks its runtime on first run). **Retry the probe for ~15–20s** (e.g. every 0.5s) rather than probing once and declaring failure — and **never relaunch on a slow start**, or you get two processes racing for the port (violates the no-second-relay rule). A slow first start is expected. On Windows the binary is windowless — no console appears; that's intended, not a failure (logs go to `~/.mcp-keep/keep.log`).

## 1.2 — Wire up the MCP client

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

Then tell the user to reload (in Claude Code, `/mcp` and reconnect `mcp-keep`, or start a new session). This reload is required: a client that was already running when the relay came up won't re-handshake on its own, so `keep_*` won't surface until reload. Don't work around it with raw `curl` — reload, then drive everything through the tools.

## 1.3 — Confirm, then stop

Call **`keep_status`**. With zero upstreams it reports the relay up and healthy with nothing attached — **this is success.** Tell the user the core is running, then *ask* whether they'd like to attach an upstream (Phase 2). If they don't, you're done — leave it here.

---

# Phase 2 — Attach an upstream (only when the user asks)

Only after Phase 1 is confirmed and the user wants to attach a server. Drive this through the relay's tools — **don't hand-edit config** (rare exception: Reference below). One decision at a time; confirm each, don't batch.

1. Call **`keep_welcome`** for guided onboarding (appears only while no upstream is configured).
2. **Offer the upstreams we support, then ask which they want.** Browse the registry (`https://api.github.com/repos/exetorius/mcp-keep-integrations/contents/`, `type: "dir"` entries, skip dotfolders) to see which servers have first-class support (e.g. VibeUE / Unreal) and present those as a menu — then ask which to attach, making clear they can point at **any** MCP server not on the list. Picking a supported server just chooses the upstream; it does **not** install that pack (Phase 3).
3. Get its **host / port / path** (e.g. `127.0.0.1:8088/mcp`). Don't guess — a supported server's pack carries sensible defaults you can surface, but installing it is still later. The `name` is the user's own label (cache key, routing, what `keep_status` shows); distinct from the upstream's self-reported `serverInfo.name`, captured automatically on first connect.
4. **Ask about auth and actively recommend a bearer token** — treat enabling it as the encouraged path; only go token-less if the user explicitly declines. Never silently default to no auth, never invent a token. (mcp-keep also auto-detects required auth by probing for a `401`, but don't use that to skip the conversation.)
5. **Do not set `integration` here** — packs are Phase 3. Leave it empty.
6. Confirm the details back (name, host, port, path, whether a bearer is set), then call **`keep_add_upstream`**. The fronted tools may need **one more reload** to surface — that's expected; prompt for it.
7. Call **`keep_status`** to confirm it attached and see the cached tool count.

> **Cache caveat (fresh home):** the relay serves tools from on-disk cache even when an upstream is offline — but there's no cache until the *first successful capture*, so on a brand-new `~/.mcp-keep` an upstream's tools won't surface until it's been reachable once (~`capture_interval_seconds`). Pre-seeding at install time is tracked in issue #35.

---

# Phase 3 — Integration pack (optional, explicit opt-in)

A pack adds tool **hints, synthetic tools, and agent instructions** tailored to a specific upstream — what makes the fronted tools genuinely usable rather than a raw list. Frame it for what it is: an **optional enhancement** on top of an already-working upstream, never something the upstream needs, never something you attach on the user's behalf.

Only after an upstream is attached and working:

1. Call **`keep_install_pack`** with **no arguments** to list available packs.
2. If a pack matches the attached upstream (e.g. they attached VibeUE and a `vibeue` pack exists), tell them it's available as an **optional enhancement** and offer **three paths**: **explain it** (describe what it adds and how it helps *their* upstream), **install it**, or **skip it**. Lead with the offer to explain so they never choose blind.
3. **Install only on an explicit yes** → `keep_install_pack name='<pack>'` downloads it into `~/.mcp-keep/integrations/<pack>/`, runs post-install steps, and sets the upstream's `integration`. (Manual fallback: download each file's `download_url` from `.../contents/<pack>` into that folder; read the pack's `README.md`/`config.example.json`.)
4. If they decline, leave it — the upstream works fine without a pack. Don't re-ask or auto-install later.

> **Never** pick a pack, set `integration:`, or run `keep_install_pack name=...` without the user first saying yes to that specific pack. Listing packs is fine; installing is a privileged effect needing consent.

---

# Phase 4 — Start with OS (optional)

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
