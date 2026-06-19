# mcp-keep — First-Time Setup

**If a human asked you (an AI assistant) to "set up keep" or "read the first-time setup file and set it up," this file is your instructions. Do the steps — don't just describe them. Show the human every privileged change before you make it; never do a silent install.**

> **Read this first — the one rule that governs everything below.** Set up in **layers, in order, and stop at each layer until it works**: (1) the **core relay**, (2) *then* an **upstream**, (3) *then* an optional **integration pack**. Each later layer is the human's explicit choice, made **one decision at a time**. You must **never** run ahead — never pick an upstream, a bearer token, or a pack *for* the human to save a step or a reload. The whole point of mcp-keep's first principle is that every privileged effect is explicit and consented. A setup that "helpfully" attaches a pack and guesses a bearer on the human's behalf is a **failed** setup, even if it works.

---

## What mcp-keep is

mcp-keep is a **lifecycle/resilience layer for MCP**. It fronts one or more upstream MCP servers on a single local port (default `8089`) and keeps their tools surfaced to this client **even while an upstream is offline**, re-attaching silently when it returns. It is not an auth proxy — a per-upstream bearer token is an optional extra, not the point.

The **relay is the product**. It runs perfectly well with **zero upstreams** — that's the core, and standing it up is the entire goal of Phase 1. Upstreams and packs come *after*, only if the human wants them.

---

# Phase 1 — Stand up the core (the relay)

**This is the whole job until it's done.** Do not ask about upstreams, bearer tokens, or integration packs yet. The success condition for this phase is: the relay is running, this client is wired to it, and the `keep_*` tools are callable — with **no upstreams configured**. That is a complete, healthy mcp-keep.

## Step 1.1 — Start the relay (detached, so it outlives this chat)

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
- run fine with **zero upstreams configured** — that's the expected, correct first-run state. Do not add one to "finish" setup.

> **No window appears — that's intended.** On Windows `mcp-keep.exe` is **windowless**: launching it pops no console, and there is no window to leave open. This is the steady state — mcp-keep is meant to run out of sight and be driven by you (the AI) over MCP. So **don't take the absence of a window as "it didn't start"** — confirm it the right way (below). Its log goes to `~/.mcp-keep/keep.log` instead of a console; `tail` that file to see what it's doing. (macOS/Linux have no console-popup to begin with.)

Confirm it's up — **poll, don't fixed-sleep-then-give-up:**

```
mcp-keep --wait-ready                  # blocks until ready; exit 0 = up, 1 = timeout
curl http://127.0.0.1:8089/mcp         # or probe by hand → "mcp-keep running"
```

`mcp-keep --wait-ready` does the poll-with-retry for you (it only probes — it never starts a second relay), so prefer it over a hand-rolled sleep loop. The **first** launch of a freshly downloaded binary can take several seconds to start listening (the OS may scan the new executable, and a bundled binary unpacks its runtime on first run). So **retry the probe for ~15–20s** (e.g. every 0.5s) until it returns `mcp-keep running`. **Do not conclude "it didn't start" after a single probe, and do not relaunch it** — relaunching while the first instance is still coming up leaves you with two processes racing for the port. A slow first start is expected, not a failure.

If port 8089 is genuinely taken by something else, check whether mcp-keep is already running before starting another one — never start a second relay on the same port.

> **Want it always running** (across reboots, with no relaunch)? That's the durable path — set up Start-with-OS. The `keep_start_with_os` tool (or the relay's own `/keep-setup` terminal command) registers it, with **no admin/elevation** (Windows HKCU `Run` registry value / macOS launchd / Linux systemd user service). A detached launch survives until the machine is rebooted; Start-with-OS survives that too. This is an offer, not a default — show the human the exact change first.

## Step 1.2 — Wire THIS client to the relay (the one manual bootstrap)

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

Then tell the human to **reload their AI client** so it picks up mcp-keep — in Claude Code, run `/mcp` and reconnect the `mcp-keep` server (a full restart also works).

**This reload is required, not optional.** If the client was already running when you started the relay, its first attempt to reach mcp-keep failed (the relay wasn't up yet) and it will **not** re-handshake on its own mid-session — so `keep_*` won't appear as callable tools until you reload. Don't work around this with raw HTTP/`curl`; just reload, then drive everything through the tools. (With **Start-with-OS** the relay is always up *before* any session starts, so the tools surface with no reload at all.)

## Step 1.3 — Confirm the core is healthy — then stop

After the reload, mcp-keep's tools are available to you. Call **`keep_status`**. With zero upstreams it will report the relay is up and healthy with nothing attached — **this is success.** Tell the human the core is running.

**Phase 1 is now complete.** Do not silently roll into adding an upstream. Instead, tell the human what they have (a running relay, no upstreams yet) and ask whether they'd like to **attach an upstream MCP server** now. If they don't want to, you're done — leave it here.

---

# Phase 2 — Attach an upstream (only when the human asks)

Do this **only after Phase 1 is confirmed working** and the human has said they want to attach a server. From here on, everything happens through the relay's own tools — **do not hand-edit config files** (the rare exception is in the Reference below). Work through these **one decision at a time**, confirming each with the human; do not batch them or fill in answers yourself.

1. Call **`keep_welcome`** — it returns guided onboarding (only appears while no upstream is configured).
2. **Offer the upstreams we support, then ask which they want.** Browse the integration registry (`https://api.github.com/repos/exetorius/mcp-keep-integrations/contents/`, entries with `type: "dir"`, skipping dotfolders) to see which servers we have first-class support for (e.g. VibeUE / Unreal), and present those as a menu — *then* ask which one they want to attach, making clear they can also point at **any** MCP server not on the list. Picking a supported server here is just choosing the upstream; it does **not** install that server's pack (that's Phase 3, a separate yes/no).
3. Get its **host / port / path** (e.g. `127.0.0.1:8088/mcp`). Don't guess these — if they're unsure and they picked a supported server, its pack carries sensible defaults you can surface, but installing the pack is still a later step.
4. **Ask about auth, and actively recommend a bearer token.** A bearer protects the upstream; treat enabling it as the encouraged path. Ask the human if they want to set one and steer toward "yes" — only go token-less if they explicitly decline. **Never silently default to no auth, and never invent a token yourself.** (mcp-keep also auto-detects required auth by probing for a `401`, so if the upstream needs one you'll find out, but don't use that as a reason to skip the conversation.)
5. **Do not set `integration` here.** Packs are Phase 3, chosen explicitly. Leave `integration` empty for now.
6. Confirm the assembled details back to the human (name, host, port, path, whether a bearer is set), then call **`keep_add_upstream`** with `name` (their label) plus `host` / `port` / `path` (and `bearer_token` if they chose one). 
7. The tools fronted by a newly added upstream may need **one more client reload** to surface (the tool list handshakes once per session) — that's expected and fine. Prompt the human to reload if the fronted tools aren't visible.
8. Call **`keep_status`** to confirm it attached and see the cached tool count.

That's it for the upstream. mcp-keep will now keep that server's tools surfaced to you even when it's offline.

---

# Phase 3 — Integration pack (optional, explicit opt-in)

A pack is **not** just a host/port lookup — it ships tool **hints, synthetic tools, and agent instructions** tailored to a specific upstream (e.g. VibeUE / Unreal), which is what makes the fronted tools genuinely usable rather than a raw list. Frame it to the human for what it is: an **optional enhancement** on top of an already-working upstream — never something the upstream needs, and never something you attach on their behalf.

Only after an upstream is attached and working:

1. Call **`keep_install_pack`** with **no arguments** to list the available packs.
2. If a pack matches the upstream the human attached (e.g. they attached VibeUE and a `vibeue` pack exists), tell them it's available as an **optional enhancement** and offer it as a clear choice with **three paths**:
   - **explain it** — describe what the pack adds (the hints / synthetic tools / agent instructions, and concretely how it improves working with *their* upstream) so they can decide informed;
   - **install it** — go ahead; or
   - **skip it** — the upstream works fine as-is.
   Lead with the offer to explain, so they're never forced to choose blind.
3. **Install only on an explicit yes.** On consent, call `keep_install_pack name='<pack>'` to download and install it into `~/.mcp-keep/integrations/<pack>/` and run any post-install steps. This also sets the upstream's `integration` to that pack.
4. If they decline, leave it — the upstream works fine without a pack. Don't re-ask or auto-install later.

> **Never** pick a pack, set `integration:`, or run `keep_install_pack name=...` without the human first saying yes to that specific pack. Browsing/listing packs is fine; installing one is a privileged effect that needs consent.

---

## Reference — config shape (for the rare case you must edit by hand)

Prefer the `keep_*` tools. Only fall back to editing `~/.mcp-keep/config.json` directly if a tool isn't available (e.g. a pre-launch edit before any client is connected). A hand-edit to a *running* relay is picked up automatically within one capture interval (the relay watches the file's mtime), or immediately if you call **`keep_reload`** — no restart needed. **Never commit this file — it can contain a bearer token.**

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

- **Layers, in order:** core relay → (optional) upstream → (optional) pack. Don't run ahead of the human into a later layer.
- **One decision at a time.** Ask, confirm, then act. Never batch-decide host/port/bearer/pack to save a reload.
- Show the human every privileged change (config writes, adding a server, installing a pack, wiring the client, Start-with-OS) before doing it. No silent installs.
- **Never auto-pick a pack** or set `integration:` — packs are an explicit, consented yes/no.
- **Recommend a bearer token** for any upstream; only go token-less if the human declines. Never invent one or silently skip auth.
- Don't start a second relay on the listen port if one is already running.
- mcp-keep is loopback-only by design — don't try to bind it beyond `127.0.0.1`.
