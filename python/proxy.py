#!/usr/bin/env python3
"""
mcp-keep
========
A lifecycle/resilience layer for MCP. It fronts one or more upstream MCP
servers on a single local port and keeps their tools surfaced to the client
even while an upstream is offline or not yet started — then silently
re-attaches when it returns.

Core ideas (the moat):
  - cache-when-down : the captured tool list is served from disk, so the
                      client always sees the tools even if the backend is dead.
  - attach-not-spawn: keep attaches to a backend it does NOT control (an
                      editor/engine/app the user runs themselves).

Everything lives under a single global home (~/.mcp-keep), outside any project.
Projects only carry a one-line .mcp.json pointer at the master port.

Terminal commands (type while running):
  /keep-status  — current config, upstream health, cached tool counts
  /keep-packs   — browse and install integration packs
  /keep-setup   — startup-with-OS preference
  /keep-reload  — reload config + integrations without restart
  /keep-quit    — stop
"""

import hashlib
import json
import os
import pathlib
import platform
import queue
import shutil
import subprocess
import sys
import time
import threading
import urllib.request
import urllib.error
import socket
import socketserver
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths — single global home, outside any project
# ---------------------------------------------------------------------------

KEEP_HOME        = pathlib.Path(os.environ.get("MCP_KEEP_HOME", pathlib.Path.home() / ".mcp-keep"))
CONFIG_PATH      = KEEP_HOME / "config.json"
INTEGRATIONS_DIR = KEEP_HOME / "integrations"
REGISTRY_PATH    = KEEP_HOME / "registry.json"

PACKS_REPO   = "exetorius/mcp-keep-integrations"
PACKS_BRANCH = "main"

SERVER_NAME = "mcp-keep"
VERSION     = "1.9.0"

# MCP protocol revision (the spec versions revisions by date, not semver).
# Pinned to the oldest stable revision for maximum upstream interop; the only
# methods we use (initialize, tools/list) are unchanged across revisions.
MCP_PROTOCOL_VERSION = "2024-11-05"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
#
# When packaged windowless (PyInstaller --windowed / --noconsole, issue #8),
# sys.stdout is None and any bare print() would crash the relay. So the log
# file is the load-bearing output channel: log() always writes to keep.log and
# only *mirrors* to stdout when a real console exists (the dev path). Guarded
# with a lock because capture threads + request handlers log concurrently.

LOG_PATH = KEEP_HOME / "keep.log"
_log_lock = threading.Lock()

def init_log() -> None:
    """Truncate keep.log at startup and write a session header. Called once
    from main() after KEEP_HOME exists."""
    try:
        with _log_lock:
            with open(LOG_PATH, "w", encoding="utf-8") as fh:
                ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                fh.write(f"=== mcp-keep {VERSION} session started {ts} ===\n")
    except OSError:
        pass  # logging must never take the relay down

def _interactive_console() -> bool:
    """True only when a human is at a real terminal we can both read and write.

    Gates the setup menu and command loop, which call input() and would hang or
    crash with no console. We require BOTH stdin and stdout to be ttys because a
    PyInstaller --windowed build (issue #8) makes sys.stdin.isatty() unreliable —
    it can report a tty when there is no console at all — so checking stdin alone
    would let the interactive menu run in a windowless process. stdout is None (or
    a redirected non-tty) in exactly those cases, so it's the reliable tell."""
    try:
        return bool(sys.stdin and sys.stdin.isatty()
                    and sys.stdout and sys.stdout.isatty())
    except (ValueError, OSError, AttributeError):
        return False

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        with _log_lock:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except OSError:
        pass  # never crash on a logging failure
    # Mirror to the console for the dev path; absent/closed under --windowed.
    if sys.stdout is not None:
        try:
            print(line, flush=True)
        except (ValueError, OSError):
            pass

# ---------------------------------------------------------------------------
# Known-packs registry — shipped defaults, matched against an upstream's
# self-reported serverInfo.name. Carries default host/port for onboarding.
# An optional registry.json in KEEP_HOME overrides/extends these.
# ---------------------------------------------------------------------------

DEFAULT_REGISTRY: dict[str, dict] = {
    # "unreal": {"host": "127.0.0.1", "port": 8088, "path": "/mcp",
    #            "integration": "unreal", "auth": "optional"},
}

def load_registry() -> dict:
    reg = dict(DEFAULT_REGISTRY)
    if REGISTRY_PATH.exists():
        try:
            reg.update(json.loads(REGISTRY_PATH.read_text(encoding="utf-8")))
        except Exception as e:
            log(f"WARNING: could not read registry.json: {e}")
    return reg

# ---------------------------------------------------------------------------
# Configuration — a list of upstreams, driven through one master port
# ---------------------------------------------------------------------------

CONFIG_DEFAULTS = {
    "listen_port":     8089,
    "max_body_bytes":  4 * 1024 * 1024,   # 4 MB body cap (brick 16)
    "allowed_origins": [],                # browser Origins allowed (brick 14); empty by default
    "capture_interval_seconds": 30,       # fast poll cadence while an upstream is DOWN (re-attach)
    "online_heartbeat_seconds": 300,      # cheap liveness check cadence while ONLINE (#40); no tools/list pull
    "pack_update_check_seconds": 21600,   # how often to refresh each installed pack's latest version (#65); 6h
    "upstreams":       [],
    "startup_asked":      False,
    "startup_registered": False,
}

UPSTREAM_DEFAULTS = {
    "name":         "",
    "host":         "127.0.0.1",
    "port":         8088,
    "path":         "/mcp",
    "bearer_token": "",
    "integration":  "",
}

def _normalise_upstream(u: dict) -> dict:
    return {**UPSTREAM_DEFAULTS, **u}

def load_config() -> dict:
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return dict(CONFIG_DEFAULTS)
    except Exception as e:
        log(f"WARNING: could not read config.json: {e}")
        return dict(CONFIG_DEFAULTS)
    cfg = {**CONFIG_DEFAULTS, **raw}
    cfg["upstreams"] = [_normalise_upstream(u) for u in cfg.get("upstreams", [])]
    return cfg

def save_config(cfg: dict) -> None:
    KEEP_HOME.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

# config.json hot-reload (#47): a hand-edit must take effect on a running relay
# without a restart. The windowless build has no console, so the old /keep-reload
# terminal command is unreachable — the capture loop watches the file's mtime and
# the keep_reload tool offers an explicit client-driven trigger. Both funnel
# through reload_config() so config, registry, and cache stay consistent.
_last_config_mtime = 0.0

def _config_mtime() -> float:
    try:
        return CONFIG_PATH.stat().st_mtime
    except OSError:
        return 0.0

def _sync_config_mtime() -> None:
    """Mark the on-disk config as 'already loaded' so a write we just made
    ourselves (e.g. keep_add_upstream) doesn't trigger a redundant hot-reload."""
    global _last_config_mtime
    _last_config_mtime = _config_mtime()

def reload_config(reason: str = "") -> int:
    """Re-read config.json + registry and rebuild routing/cache from them.
    Shared by /keep-reload, the keep_reload tool, and the capture-loop hot-reload.
    Returns the count of configured (named) upstreams."""
    STATE.cfg = load_config()
    STATE.registry = load_registry()
    STATE.rebuild_from_cache()
    _sync_config_mtime()
    n = sum(1 for u in STATE.cfg["upstreams"] if u.get("name"))
    if reason:
        log(f"config reloaded ({reason}) — {n} upstream(s) configured")
    return n

def upstream_url(u: dict) -> str:
    return f"http://{u['host']}:{u['port']}{u['path']}"

# ---------------------------------------------------------------------------
# Integration packs — per upstream: hints, synthetic tools, instructions
# ---------------------------------------------------------------------------

# A pack is "installed" only if it has real content files. We can't use mere
# directory existence (#61): an upstream's capture cache lives at
# integrations/<name>/.cache/, so when the upstream name equals the pack name
# (the normal case, e.g. vibeue) capturing the upstream creates the directory
# even though no pack is installed — which would falsely read as present.
_PACK_FILES = ("hints.json", "synthetic_tools.json", "instructions.md")

def pack_installed(name: str) -> bool:
    """True if an integration pack named `name` has content files on disk (#61)."""
    if not name:
        return False
    base = INTEGRATIONS_DIR / name
    return any((base / f).exists() for f in _PACK_FILES)

def pack_version(name: str) -> str | None:
    """Installed pack version from its pack.json, or None if unversioned/missing (#65)."""
    try:
        p = INTEGRATIONS_DIR / name / "pack.json"
        if p.exists():
            return str(json.loads(p.read_text(encoding="utf-8")).get("version") or "").strip() or None
    except Exception:
        pass
    return None

def remote_pack_version(name: str) -> str | None:
    """Latest pack version from the integrations repo's pack.json on `main` (#65)."""
    try:
        return str(json.loads(_gh_raw(f"{name}/pack.json")).get("version") or "").strip() or None
    except Exception:
        return None

def load_pack(name: str) -> dict:
    """Load a pack's hints / synthetic tools / instructions. Safe if missing."""
    pack = {"hints": {}, "synthetic_tools": [], "instructions": ""}
    if not name:
        return pack
    base = INTEGRATIONS_DIR / name
    if not pack_installed(name):
        # #58/#61: an upstream references a pack that isn't installed (no content
        # files — a bare .cache dir doesn't count). Don't swallow it silently —
        # log so it's diagnosable; keep_status surfaces it too.
        log(f"integration '{name}' is set but the pack is not installed "
            f"({base}) — run keep_install_pack name='{name}' to install it.")
        return pack
    try:
        hp = base / "hints.json"
        if hp.exists():
            pack["hints"] = json.loads(hp.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"pack '{name}' hints error: {e}")
    try:
        sp = base / "synthetic_tools.json"
        if sp.exists():
            pack["synthetic_tools"] = json.loads(sp.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"pack '{name}' synthetic_tools error: {e}")
    try:
        ip = base / "instructions.md"
        if ip.exists():
            pack["instructions"] = ip.read_text(encoding="utf-8")
    except Exception as e:
        log(f"pack '{name}' instructions error: {e}")
    return pack

def apply_hints(tools: list, hints: dict) -> list:
    out = []
    for tool in tools:
        hint = hints.get(tool.get("name", ""))
        if hint:
            tool = {**tool, "description": tool.get("description", "") + hint}
        out.append(tool)
    return out

# ---------------------------------------------------------------------------
# Manifest cache — per upstream, persisted on disk. THIS is cache-when-down:
# tools are served from here regardless of whether the upstream is reachable.
# ---------------------------------------------------------------------------

def cache_path(upstream_name: str) -> pathlib.Path:
    return INTEGRATIONS_DIR / upstream_name / ".cache" / "manifest.json"

def load_cached_manifest(upstream_name: str) -> dict:
    """Returns {'serverInfo': {...}, 'tools': [...]} or empty shell."""
    p = cache_path(upstream_name)
    if not p.exists():
        return {"serverInfo": {}, "tools": []}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        log(f"cache read error for '{upstream_name}': {e}")
        return {"serverInfo": {}, "tools": []}

def save_cached_manifest(upstream_name: str, server_info: dict, tools: list) -> None:
    p = cache_path(upstream_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "serverInfo": server_info,
        "tools": tools,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def seed_cache_if_absent(upstream_name: str, pack_name: str) -> bool:
    """Bootstrap the tool cache from a pack's shipped seed (#35).

    Closes the first-ever-run gap: a brand-new user who installs a pack but
    hasn't launched the upstream even once would otherwise see 0 tools until
    the first capture. If the pack ships a `cache.seed.json` and the upstream
    has no cache yet, copy it into the cache path so tools surface before the
    upstream has *ever* been reachable.

    Never clobbers a live-captured manifest — seeds only when none exists. The
    seed is marked `"seeded": true` (and carries no `captured_at`) so it isn't
    mistaken for a real capture; the capture loop overwrites it via
    save_cached_manifest on the first successful connect, so staleness is
    self-correcting.
    """
    if not pack_name:
        return False
    cp = cache_path(upstream_name)
    if cp.exists():
        return False                       # never clobber an existing (real or seeded) cache
    seed = INTEGRATIONS_DIR / pack_name / "cache.seed.json"
    if not seed.exists():
        return False
    try:
        data = json.loads(seed.read_text(encoding="utf-8"))
        cp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "serverInfo": data.get("serverInfo", {}),
            "tools": data.get("tools", []),
            "captured_at": None,
            "seeded": True,
        }
        cp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        log(f"seeded cache for '{upstream_name}' from pack '{pack_name}' "
            f"({len(payload['tools'])} tools) — pre-connect bootstrap (#35)")
        return True
    except Exception as e:
        log(f"cache seed for '{upstream_name}' from pack '{pack_name}' failed: {e}")
        return False

# ---------------------------------------------------------------------------
# Runtime state — built from cache on startup, refreshed by capture loop
# ---------------------------------------------------------------------------

class State:
    def __init__(self):
        self.lock        = threading.Lock()
        self.cfg         = load_config()
        self.registry    = load_registry()
        # per-upstream: {"manifest": {...}, "pack": {...}, "online": bool, "auth_required": bool}
        self.upstreams: dict[str, dict] = {}
        self.routing:   dict[str, str]  = {}   # tool name -> upstream name
        # #65 pack-update detection: {pack_name: {"latest": str|None, "checked_at": ts}}
        self.pack_latest: dict[str, dict] = {}
        # #6 live tool-list refresh: open SSE client streams (each a Queue we push
        # notifications into) + a signature of the last-broadcast aggregate surface.
        self.listeners: set = set()
        self.tools_sig: str = ""
        self.rebuild_from_cache()

    def rebuild_from_cache(self):
        with self.lock:
            self.upstreams.clear()
            self.routing.clear()
            for u in self.cfg["upstreams"]:
                name = u["name"]
                if not name:
                    continue
                pack_name = u.get("integration", "")
                seed_cache_if_absent(name, pack_name)   # #35: pre-connect bootstrap, no-op if cache exists
                manifest = load_cached_manifest(name)
                pack     = load_pack(pack_name)
                self.upstreams[name] = {
                    "config": u, "manifest": manifest, "pack": pack,
                    "online": False, "auth_required": False,
                    # #40 cadence/observability: when the live tool surface was last
                    # PROVEN fresh (a real tools/list pull this session), a hash of it
                    # to skip churn when unchanged, and the last cheap liveness ping.
                    "captured_at": None, "tools_hash": "", "last_liveness": 0.0,
                }
                self._index_tools(name, manifest.get("tools", []), pack)

    def _index_tools(self, upstream_name: str, tools: list, pack: dict):
        for t in tools:
            tn = t.get("name")
            if tn:
                self.routing.setdefault(tn, upstream_name)
        for t in pack.get("synthetic_tools", []):
            tn = t.get("name")
            if tn:
                self.routing.setdefault(tn, upstream_name)

    def aggregate_tools(self) -> list:
        """All tools across all upstreams (cache + hints + synthetic) + management tools."""
        out = []
        with self.lock:
            for name, st in self.upstreams.items():
                tools = st["manifest"].get("tools", [])
                out.extend(apply_hints(tools, st["pack"].get("hints", {})))
                out.extend(st["pack"].get("synthetic_tools", []))
        out.extend(management_tools(self))
        return out

    def aggregate_instructions(self) -> str:
        parts = []
        with self.lock:
            for st in self.upstreams.values():
                instr = st["pack"].get("instructions", "")
                if instr:
                    parts.append(instr)
        return "\n\n".join(parts)

    def upstream_for_tool(self, tool_name: str):
        with self.lock:
            name = self.routing.get(tool_name)
            if name is None and len(self.cfg["upstreams"]) == 1:
                name = self.cfg["upstreams"][0]["name"]   # lenient single-upstream fallback
            if name is None:
                return None
            return self.upstreams.get(name, {}).get("config")

STATE: "State" = None  # set in main / on demand

# Management tools whose effect changes the aggregate tool surface — used to push
# a live refresh immediately after they run (#6), instead of waiting for the loop.
_MUTATING_TOOLS = {"keep_add_upstream", "keep_remove_upstream",
                   "keep_install_pack", "keep_remove_pack", "keep_reload"}

def maybe_notify_tools_changed():
    """Push notifications/tools/list_changed to open SSE clients when the aggregate
    tool surface actually changed since the last broadcast (#6). The relay already
    advertises capabilities.tools.listChanged; this is what delivers it, so a client
    refreshes its tool list live instead of needing a /mcp reconnect. Cheap, idempotent
    signature compare — safe to call liberally. Must NOT be called while holding
    STATE.lock (aggregate_tools acquires it)."""
    if STATE is None:
        return
    try:
        names = sorted(t.get("name", "") for t in STATE.aggregate_tools())
    except Exception:
        return
    sig = hashlib.sha256("\n".join(names).encode()).hexdigest()
    with STATE.lock:
        if sig == STATE.tools_sig:
            return
        STATE.tools_sig = sig
        listeners = list(STATE.listeners)
    frame = ("event: message\ndata: "
             + json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"})
             + "\n\n").encode()
    for q in listeners:
        try:
            q.put_nowait(frame)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Upstream capture — initialize handshake + tools/list, learn identity.
# Runs in the background; updates the on-disk cache when reachable.
# ---------------------------------------------------------------------------

def _post_mcp(url: str, payload: dict, bearer: str, session_id: str = ""):
    """POST a JSON-RPC message to an MCP HTTP server. Returns (status, headers, obj)."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-MCP-Keep": "true",
    }
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    if session_id:
        headers["mcp-session-id"] = session_id
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            ctype = resp.headers.get("Content-Type", "")
            sid = resp.headers.get("mcp-session-id", session_id)
            if "text/event-stream" in ctype:
                # #24: read the SSE stream incrementally and stop at the first
                # complete event. A Streamable-HTTP upstream may hold the stream
                # open for server-initiated messages, so resp.read() (wait for EOF)
                # could block until the timeout. _read_sse_first_event returns as
                # soon as one event's payload parses, then we close the connection.
                obj = _read_sse_first_event(resp)
            else:
                obj = _parse_mcp_body(resp.read(), ctype)
            return resp.status, {"mcp-session-id": sid, "content-type": ctype}, obj
    except urllib.error.HTTPError as e:
        return e.code, {}, None

# Bound how much of an SSE stream we'll read while hunting the first event, so a
# misbehaving keep-alive stream can't make us read unboundedly (#24).
_SSE_READ_CAP = 4 * 1024 * 1024

def _read_sse_first_event(resp):
    """Return the first SSE event that parses as JSON, reading line-by-line so we
    never wait for the stream to EOF (#24). Accumulates `data:` fields and parses
    the joined payload when a blank line completes an event; ignores comments
    (`: keep-alive`) and other SSE fields."""
    data_lines: list[str] = []
    total = 0
    for raw_line in resp:                      # readline-based: incremental, not EOF
        total += len(raw_line)
        if total > _SSE_READ_CAP:
            break
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        if line.startswith("data:"):
            value = line[5:]
            if value.startswith(" "):
                value = value[1:]
            data_lines.append(value)
        elif line == "":                       # blank line terminates an event
            if data_lines:
                try:
                    return json.loads("\n".join(data_lines))
                except json.JSONDecodeError:
                    data_lines = []
    if data_lines:                             # stream ended mid/after a data block
        try:
            return json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            return None
    return None

def _parse_mcp_body(raw: bytes, content_type: str):
    """Handle both plain JSON and SSE (text/event-stream) responses.

    SSE events may carry their payload across multiple `data:` lines (a server
    is free to pretty-print JSON one physical line per `data:` field). Per the
    SSE spec those lines are concatenated with newlines to form the event data,
    so we must accumulate them and parse the joined payload — not each line
    individually. We return the first event that parses as JSON. (See issue #23.)
    """
    text = raw.decode("utf-8", errors="replace").strip()
    if "text/event-stream" in content_type:
        data_lines: list[str] = []
        for line in text.splitlines():
            if line.startswith("data:"):
                # Strip exactly one leading space after the colon (SSE spec).
                value = line[5:]
                if value.startswith(" "):
                    value = value[1:]
                data_lines.append(value)
            elif not line.strip():            # blank line terminates an event
                if data_lines:
                    try:
                        return json.loads("\n".join(data_lines))
                    except json.JSONDecodeError:
                        data_lines = []
        if data_lines:                        # last event, no trailing blank line
            try:
                return json.loads("\n".join(data_lines))
            except json.JSONDecodeError:
                return None
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None

def capture_upstream(u: dict) -> bool:
    """Connect, handshake, capture tools/list -> cache. Returns True if captured."""
    url = upstream_url(u)
    name = u["name"]
    bearer = u.get("bearer_token", "")
    auth_required = False

    init_payload = {
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcp-keep", "version": VERSION},
        },
    }

    status, hdrs, obj = _post_mcp(url, init_payload, "")
    if status == 401:                       # auth probe (brick 7)
        auth_required = True
        if not bearer:
            _mark(name, online=False, auth_required=True)
            return False
        status, hdrs, obj = _post_mcp(url, init_payload, bearer)

    if status >= 400 or obj is None:
        _mark(name, online=False, auth_required=auth_required)
        return False

    session_id  = hdrs.get("mcp-session-id", "")
    server_info = (obj.get("result") or {}).get("serverInfo", {})

    # politely complete the handshake
    _post_mcp(url, {"jsonrpc": "2.0", "method": "notifications/initialized"},
              bearer if auth_required else "", session_id)

    status, hdrs, obj = _post_mcp(
        url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        bearer if auth_required else "", session_id)
    if status >= 400 or obj is None:
        _mark(name, online=False, auth_required=auth_required)
        return False

    tools = (obj.get("result") or {}).get("tools", [])
    new_hash = _tools_hash(tools)
    now = time.time()

    with STATE.lock:
        st = STATE.upstreams.get(name)
        unchanged = st is not None and st.get("tools_hash") == new_hash and bool(tools)
        if st is not None:
            st["manifest"] = {"serverInfo": server_info, "tools": tools}
            st["online"] = True
            st["auth_required"] = auth_required
            st["tools_hash"] = new_hash
            st["captured_at"] = now      # last time the surface was PROVEN fresh
            st["last_liveness"] = now
            STATE._index_tools(name, tools, st["pack"])

    # Skip churn (#40): only rewrite the on-disk cache when the tool surface
    # actually changed — an identical re-capture is a no-op on disk.
    if not unchanged:
        save_cached_manifest(name, server_info, tools)

    identity = server_info.get("name", "?")
    log(f"captured '{name}' (identity='{identity}', {len(tools)} tools, "
        f"auth={'required' if auth_required else 'none'}"
        f"{', unchanged' if unchanged else ''})")
    return True

def _tools_hash(tools: list) -> str:
    """Stable hash of a tool surface, to detect unchanged captures (#40)."""
    blob = json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()

def _ago(ts: float) -> str:
    """Human relative time for keep_status freshness (#40)."""
    secs = max(0, int(time.time() - ts))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"

def liveness_ok(u: dict) -> bool:
    """Cheap transport health check (#40): can we open a TCP connection to the
    upstream? Proves liveness WITHOUT a full initialize + tools/list handshake,
    so a healthy upstream isn't spammed with a 21 KB catalog pull every cycle."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(2.0)
    try:
        return s.connect_ex((u["host"], int(u["port"]))) == 0
    except OSError:
        return False
    finally:
        s.close()

def _mark(name: str, online: bool, auth_required: bool):
    with STATE.lock:
        st = STATE.upstreams.get(name)
        if st is not None:
            st["online"] = online
            st["auth_required"] = auth_required
            if not online:
                st["last_liveness"] = 0.0   # force a fresh full capture on next poll

def _safe_capture(u: dict):
    """Capture one upstream, swallowing the expected offline/transient errors."""
    if not u.get("name"):
        return
    try:
        capture_upstream(u)
    except (urllib.error.URLError, socket.timeout, OSError):
        _mark(u["name"], online=False, auth_required=False)
    except Exception as e:
        log(f"capture error for '{u['name']}': {e}")

def check_pack_updates(force: bool = False):
    """Refresh the cached 'latest version' for installed packs referenced by an
    upstream (#65). Network-light: one raw pack.json GET per due pack, self-
    throttled to pack_update_check_seconds unless force=True. Cached in
    STATE.pack_latest so keep_status reads it instantly with no per-call network."""
    interval = max(60, int(STATE.cfg.get("pack_update_check_seconds", 21600)))
    now = time.time()
    packs = {u.get("integration") for u in STATE.cfg["upstreams"]
             if pack_installed(u.get("integration"))}
    for pack in packs:
        cur = STATE.pack_latest.get(pack)
        if not force and cur and (now - cur.get("checked_at", 0.0) < interval):
            continue
        latest = remote_pack_version(pack)   # network — outside the lock
        with STATE.lock:
            prev = STATE.pack_latest.get(pack, {})
            STATE.pack_latest[pack] = {
                # keep the last known version if this fetch failed, but still
                # record checked_at so we don't retry harder than the interval.
                "latest": latest if latest is not None else prev.get("latest"),
                "checked_at": now,
            }

def capture_loop():
    """Background re-attach with an INVERTED cadence (#40):

    - DOWN / not-yet-captured upstream  -> poll FAST (capture_interval_seconds):
      full initialize + tools/list to (re)attach. No log-spam cost — it's down.
    - ONLINE + captured upstream        -> stay quiet: a cheap TCP liveness check
      only every online_heartbeat_seconds, and NEVER a tools/list pull. The cache
      is the source of truth while healthy; freshness is re-proven only on the
      next (re)attach or an explicit signal. A lost liveness ping flips it back
      to DOWN, which resumes fast polling.

    Also hot-reloads config.json when it changes on disk (#47), within one tick."""
    _sync_config_mtime()
    while True:
        mt = _config_mtime()
        if mt and mt != _last_config_mtime:
            reload_config("config.json changed on disk")
        now = time.time()
        heartbeat = max(30, int(STATE.cfg.get("online_heartbeat_seconds", 300)))
        for u in list(STATE.cfg["upstreams"]):
            name = u.get("name")
            if not name:
                continue
            st = STATE.upstreams.get(name)
            if st and st.get("online"):
                # Healthy: cheap liveness only, on the slow heartbeat — no handshake.
                if now - st.get("last_liveness", 0.0) >= heartbeat:
                    if liveness_ok(u):
                        with STATE.lock:
                            st["last_liveness"] = now
                    else:
                        log(f"liveness lost for '{name}' — marking down, will re-capture")
                        _mark(name, online=False, auth_required=st.get("auth_required", False))
            else:
                # Down or never captured this session: poll hard to (re)attach.
                _safe_capture(u)
        check_pack_updates()           # #65: self-throttled pack-version refresh
        maybe_notify_tools_changed()   # #6: push a refresh if the surface changed this tick
        time.sleep(max(5, int(STATE.cfg.get("capture_interval_seconds", 30))))

# ---------------------------------------------------------------------------
# tools/call forwarding — route to the owning upstream, inject its bearer
# ---------------------------------------------------------------------------

def forward_call(u: dict, body_bytes: bytes, client_headers: dict) -> tuple[bool, bytes]:
    url = upstream_url(u)
    fwd = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "X-MCP-Keep": "true",
        "Connection": "close",
    }
    if u.get("bearer_token"):
        fwd["Authorization"] = f"Bearer {u['bearer_token']}"
    for k in ("mcp-protocol-version", "mcp-session-id"):
        if k in client_headers:
            fwd[k] = client_headers[k]

    last = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, data=body_bytes, headers=fwd, method="POST")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return True, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            log(f"upstream '{u['name']}' HTTP {e.code}: {body[:200]}")
            return False, e.headers.get("Content-Type", ""), body
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            last = exc
            if attempt == 0:
                log(f"'{u['name']}' connection error, retrying: {exc}")
    log(f"'{u['name']}' unreachable: {last}")
    return False, "", b""

def error_result(req_id, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": req_id,
            "result": {"content": [{"type": "text", "text": text}], "isError": True}}

# ---------------------------------------------------------------------------
# Management tools (chat is the UI) — exposed to the client conditionally
# ---------------------------------------------------------------------------

# Tools mcp-keep answers itself (never forwarded to an upstream). The tools/call
# dispatcher routes any name in this set to handle_management_call().
MANAGEMENT_TOOL_NAMES = {
    "keep_status", "keep_install_pack", "keep_remove_pack",
    "keep_add_upstream", "keep_remove_upstream", "keep_welcome",
    "keep_start_with_os", "keep_disable_start_with_os", "keep_reload",
}

def _missing_packs(state: "State") -> list[str]:
    """Integration names referenced by an upstream but not installed on disk (#58)."""
    return [u["integration"] for u in state.cfg["upstreams"]
            if u.get("integration") and not pack_installed(u["integration"])]

def management_tools(state: "State") -> list[dict]:
    tools = []

    # First-run onboarding — state-gated: surfaced ONLY while no upstream is
    # configured, and drops off the tool list once one is added. This is how a
    # bare binary onboards itself over MCP with no repo / CLAUDE.md present.
    # (Vanishing/appearing is pushed live via notifications/tools/list_changed to
    # any open SSE client — #6 — so a compliant client re-fetches without a reload.)
    if not state.cfg["upstreams"]:
        tools.append({
            "name": "keep_welcome",
            "description": ("Onboarding guidance for mcp-keep when no upstream is configured yet. "
                            "Call this first: it returns step-by-step instructions for setting the "
                            "user up. Disappears once an upstream exists."),
            "inputSchema": {"type": "object", "properties": {}, "required": []},
        })

    tools.append({
        "name": "keep_status",
        "description": ("Show mcp-keep status: configured upstreams, whether each is "
                        "currently reachable, how many tools are cached for each, and "
                        "whether any installed integration pack has an update available. "
                        "Pass check_updates=true to force a live re-check of pack versions "
                        "now instead of using the cached result."),
        "inputSchema": {
            "type": "object",
            "properties": {"check_updates": {"type": "boolean",
                           "description": "Force a live check of each installed pack's latest version (default: use cached)."}},
            "required": [],
        },
    })

    # Always available: register a new upstream MCP server over the protocol —
    # no config-file editing or filesystem access required.
    tools.append({
        "name": "keep_add_upstream",
        "description": ("Register a new upstream MCP server with mcp-keep, so its tools are "
                        "aggregated and kept surfaced even while it is offline. Only 'name' is "
                        "required (the user's own label). Confirm the host/port/path with the "
                        "user before calling — this writes config and adds a network upstream."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "name":         {"type": "string",
                                 "description": "Your local label for this upstream — cache key, routing handle, what keep_status shows."},
                "host":         {"type": "string",
                                 "description": "Upstream host. Default 127.0.0.1."},
                "port":         {"type": "integer",
                                 "description": "Upstream port. Default 8088."},
                "path":         {"type": "string",
                                 "description": "Upstream MCP path. Default /mcp."},
                "bearer_token": {"type": "string",
                                 "description": "Optional. Injected as 'Authorization: Bearer' for this upstream only."},
                "integration":  {"type": "string",
                                 "description": "Optional pack name to attach (see keep_install_pack)."},
            },
            "required": ["name"],
        },
    })

    # Removal counterpart to keep_add_upstream (#36) — surfaced only while there
    # is at least one upstream to remove, so it never clutters a zero-upstream core.
    if state.cfg["upstreams"]:
        tools.append({
            "name": "keep_remove_upstream",
            "description": ("Remove an upstream MCP server from mcp-keep by its label. Drops it from "
                            "config and stops fronting its tools. Confirm with the user before calling "
                            "— this writes config and removes a network upstream. The on-disk tool "
                            "cache is left in place, so re-adding the same name restores it instantly."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "The label of the upstream to remove (as shown in keep_status)."},
                },
                "required": ["name"],
            },
        })

    # Apply a hand-edited config.json without a restart. The capture loop also
    # hot-reloads on file change within one interval; this is the explicit, instant
    # trigger that replaces the console-only /keep-reload for the windowless build.
    tools.append({
        "name": "keep_reload",
        "description": ("Re-read ~/.mcp-keep/config.json and integration packs, applying any "
                        "hand-edits to a running relay without a restart. Use after editing the "
                        "config file directly. Returns the refreshed upstream status."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    })

    # Offer pack install whenever an upstream has no integration set, OR an
    # upstream references a pack that isn't installed on disk (#58 — without the
    # second clause an `integration` pointing at a missing pack hides the very
    # tool needed to install it: a silent catch-22).
    if (any(not u.get("integration") for u in state.cfg["upstreams"])
            or not state.cfg["upstreams"]
            or _missing_packs(state)):
        tools.append({
            "name": "keep_install_pack",
            "description": ("Install (or update) an integration pack. Call with no arguments to "
                            "list available packs, or name='<pack>' to install one. Downloads the "
                            "pack AND attaches it to an upstream: auto-attaches when unambiguous "
                            "(one upstream, or one whose name matches the pack), or pass "
                            "upstream='<name>' to attach explicitly. Packs add tool hints, "
                            "synthetic tools, and agent instructions for that upstream."),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": "Pack to install/update. Omit to list available packs."},
                    "upstream": {"type": "string",
                                 "description": "Upstream to attach the pack to (sets its integration). Optional — auto-attaches when unambiguous."},
                },
                "required": [],
            },
        })

    # Removal counterpart to keep_install_pack (#59) — surfaced only when there's
    # a pack to remove: a real pack installed on disk (content files, not a bare
    # .cache dir — #61), or an upstream still referencing one (so it can also clear
    # a dangling integration that points at a missing pack).
    _installed = [p.name for p in INTEGRATIONS_DIR.iterdir()
                  if p.is_dir() and pack_installed(p.name)] if INTEGRATIONS_DIR.exists() else []
    if _installed or any(u.get("integration") for u in state.cfg["upstreams"]):
        tools.append({
            "name": "keep_remove_pack",
            "description": ("Remove an integration pack: detaches it from any upstream that "
                            "references it (clears their 'integration') AND deletes it from "
                            "~/.mcp-keep/integrations/. Mirror of keep_install_pack. Confirm with "
                            "the user before calling — it writes config and deletes files. Re-install "
                            "later with keep_install_pack if needed."),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string",
                               "description": "The pack name to remove (as shown by keep_status / keep_install_pack)."}},
                "required": ["name"],
            },
        })

    # Start-with-OS controls. Deliberately NOT state-gated on startup_registered:
    # the tool list is handshaked once per client session, so gating would leave
    # the opposite tool unsurfaced after a mid-session flip — forcing a /mcp
    # reload just to undo what you just did (issue #32). Both are always present
    # and idempotent; the live steering toward Start-with-OS lives in keep_status
    # text instead, which the relay regenerates on every call.
    tools.append({
        "name": "keep_start_with_os",
        "description": ("Register mcp-keep to start automatically at login, so the relay is "
                        "already up before any client session begins (the only zero-reload path). "
                        "This changes the OS launch surface — a scheduled task (Windows), launchd "
                        "agent (macOS), or systemd user service (Linux). Show the user the exact "
                        "change and get explicit consent BEFORE calling. Idempotent."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    })
    tools.append({
        "name": "keep_disable_start_with_os",
        "description": ("Undo keep_start_with_os: remove mcp-keep from OS login startup. "
                        "Idempotent — safe to call even if start-with-OS was never enabled."),
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    })
    return tools

def _attach_pack_after_install(pack_name: str, want: str) -> str:
    """Link a freshly-installed pack to an upstream (#67) — download alone doesn't.

    Explicit `want` (upstream name) overrides. Otherwise auto-attach only when
    unambiguous: a single upstream, or one whose name matches the pack — and never
    silently overwrite an upstream already using a *different* pack. Returns a
    human-readable status note; writes + persists config when it attaches."""
    cfg = load_config()
    ups = cfg["upstreams"]
    names = ", ".join(u.get("name", "?") for u in ups) or "(none)"
    if want:
        target = next((u for u in ups if u.get("name") == want), None)
        if target is None:
            return (f"NOTE: no upstream named '{want}' to attach to (have: {names}). "
                    "Files installed but not attached.")
    elif not ups:
        return ("No upstream is configured yet — add one and attach this pack with "
                f"keep_add_upstream (set integration='{pack_name}').")
    elif len(ups) == 1:
        target = ups[0]
    else:
        target = next((u for u in ups if u.get("name") == pack_name), None)
        if target is None:
            return ("Multiple upstreams exist and none matches the pack name, so it was NOT "
                    f"auto-attached. Re-run keep_install_pack name='{pack_name}' "
                    f"upstream='<one of: {names}>' to attach it.")
    cur = target.get("integration") or ""
    if cur == pack_name:
        return f"Already attached to upstream '{target['name']}'."
    if not want and cur:
        return (f"NOTE: upstream '{target['name']}' already uses pack '{cur}', so it was NOT "
                f"changed. Re-run keep_install_pack name='{pack_name}' upstream='{target['name']}' "
                "to switch it.")
    target["integration"] = pack_name
    save_config(cfg)
    _sync_config_mtime()   # our own write — don't let the hot-reload re-fire on it
    return f"Attached to upstream '{target['name']}' (integration='{pack_name}')."

def handle_management_call(req_id, tool_name: str, args: dict):
    def ok(text):
        return {"jsonrpc": "2.0", "id": req_id,
                "result": {"content": [{"type": "text", "text": text}]}}

    if tool_name == "keep_welcome":
        return ok(
            "Welcome to mcp-keep — a lifecycle/resilience layer that fronts your MCP "
            "server(s) on one local port and keeps their tools surfaced even while an "
            "upstream is offline, re-attaching silently when it returns.\n\n"
            "No upstream is configured yet. To set one up:\n"
            "  1. Ask the user which MCP server they want to attach, and for its host, "
            "port, and path (e.g. 127.0.0.1:8088/mcp). If they don't know, keep_install_pack "
            "lists integration packs that often carry sensible defaults.\n"
            "  2. Ask whether it needs auth (a bearer token). If unsure, you can add it "
            "without one — mcp-keep auto-detects required auth by probing for a 401.\n"
            "  3. Confirm the details back to the user, then call keep_add_upstream with "
            "name (their label) plus host/port/path (+ bearer_token / integration if any).\n"
            "  4. Call keep_status to confirm it attached and see its cached tool count.\n\n"
            "Every privileged step (adding an upstream, installing a pack) must be shown to "
            "the user and done with their consent — never silently.")

    if tool_name == "keep_add_upstream":
        a = args or {}
        name = str(a.get("name") or "").strip()
        if not name:
            return error_result(req_id,
                "keep_add_upstream needs a 'name' — the user's label for this upstream.")
        # Reload fresh from disk so we don't clobber a concurrent change.
        cfg = load_config()
        if any(u.get("name") == name for u in cfg["upstreams"]):
            return error_result(req_id,
                f"An upstream named '{name}' already exists. Choose a different name.")
        try:
            port = int(a.get("port") or UPSTREAM_DEFAULTS["port"])
        except (TypeError, ValueError):
            return error_result(req_id, f"port must be an integer, got {a.get('port')!r}.")
        new_u = _normalise_upstream({
            "name":         name,
            "host":         str(a.get("host") or UPSTREAM_DEFAULTS["host"]),
            "port":         port,
            "path":         str(a.get("path") or UPSTREAM_DEFAULTS["path"]),
            "bearer_token": str(a.get("bearer_token") or ""),
            "integration":  str(a.get("integration") or ""),
        })
        cfg["upstreams"].append(new_u)
        save_config(cfg)
        STATE.cfg = cfg
        STATE.rebuild_from_cache()
        _sync_config_mtime()   # our own write — don't let the hot-reload re-fire on it
        # Attach immediately rather than waiting for the next capture poll.
        threading.Thread(target=_safe_capture, args=(new_u,), daemon=True).start()
        extra = ""
        if new_u["bearer_token"]:
            extra += ", auth=bearer"
        if new_u["integration"]:
            extra += f", pack='{new_u['integration']}'"
        return ok(f"Added upstream '{name}' -> {upstream_url(new_u)}{extra}. "
                  "Capturing its tools now — call keep_status in a moment to confirm.")

    if tool_name == "keep_remove_upstream":
        a = args or {}
        name = str(a.get("name") or "").strip()
        if not name:
            return error_result(req_id,
                "keep_remove_upstream needs a 'name' — the label of the upstream to remove "
                "(as shown in keep_status).")
        # Reload fresh from disk so we don't clobber a concurrent change.
        cfg = load_config()
        before = len(cfg["upstreams"])
        cfg["upstreams"] = [u for u in cfg["upstreams"] if u.get("name") != name]
        if len(cfg["upstreams"]) == before:
            existing = ", ".join(u.get("name", "?") for u in cfg["upstreams"]) or "(none)"
            return error_result(req_id,
                f"No upstream named '{name}' to remove. Configured upstreams: {existing}.")
        save_config(cfg)
        STATE.cfg = cfg
        STATE.rebuild_from_cache()
        _sync_config_mtime()   # our own write — don't let the hot-reload re-fire on it
        return ok(f"Removed upstream '{name}'. {len(cfg['upstreams'])} upstream(s) remain. "
                  "Its tool cache is kept, so re-adding the same name restores it instantly. "
                  "Call keep_status to confirm.")

    if tool_name == "keep_remove_pack":
        a = args or {}
        name = str(a.get("name") or "").strip()
        if not name:
            return error_result(req_id,
                "keep_remove_pack needs a 'name' — the pack to remove (as shown by "
                "keep_status / keep_install_pack).")
        # Reload fresh from disk so we don't clobber a concurrent change.
        cfg = load_config()
        detached = [u["name"] for u in cfg["upstreams"] if u.get("integration") == name]
        base = INTEGRATIONS_DIR / name
        had_files = base.exists()
        if not detached and not had_files:
            return error_result(req_id,
                f"Nothing to remove for pack '{name}': no upstream references it and it "
                "is not installed in ~/.mcp-keep/integrations/.")
        for u in cfg["upstreams"]:
            if u.get("integration") == name:
                u["integration"] = ""
        if had_files:
            shutil.rmtree(base, ignore_errors=True)
        save_config(cfg)
        STATE.cfg = cfg
        STATE.rebuild_from_cache()
        _sync_config_mtime()   # our own write — don't let the hot-reload re-fire on it
        did = []
        if detached:
            did.append(f"detached from {', '.join(detached)}")
        did.append("deleted from disk" if had_files else "was not installed on disk")
        return ok(f"Removed pack '{name}' ({'; '.join(did)}). "
                  "Re-install later with keep_install_pack if needed. Call keep_status to confirm.")

    if tool_name == "keep_reload":
        n = reload_config("keep_reload tool")
        return ok(f"Reloaded config + integration packs — {n} upstream(s) configured. "
                  "Call keep_status to see each upstream's current health and cached tool count.")

    if tool_name == "keep_status":
        if (args or {}).get("check_updates"):
            # Explicit live re-check (#65): the user's escape hatch from a stale
            # cache — fetch each installed pack's latest version right now.
            check_pack_updates(force=True)
        lines = [f"mcp-keep {VERSION} — listening on 127.0.0.1:{STATE.cfg['listen_port']}"]
        if not STATE.cfg["upstreams"]:
            lines.append("No upstreams configured yet. Call keep_add_upstream to register one "
                         "(or keep_welcome for guided setup).")
        cfg_bearer = {u["name"]: bool(u.get("bearer_token"))
                      for u in STATE.cfg["upstreams"]}
        needs_token = []
        with STATE.lock:
            for name, st in STATE.upstreams.items():
                tools = st["manifest"].get("tools", [])
                ident = st["manifest"].get("serverInfo", {}).get("name", "?")
                state_str = "online" if st["online"] else "OFFLINE (serving cache)"
                if st["auth_required"] and not cfg_bearer.get(name):
                    auth_str = "REQUIRED but no bearer set"
                    needs_token.append(name)
                elif st["auth_required"]:
                    auth_str = "required (bearer set)"
                elif cfg_bearer.get(name):
                    # #68: a bearer IS configured; we just haven't probed a 401 this
                    # session (e.g. upstream offline). Don't render that as "none".
                    auth_str = "none detected (bearer configured)"
                else:
                    auth_str = "none"
                fresh = (f"verified fresh {_ago(st['captured_at'])}"
                         if st.get("captured_at")
                         else "from cache, not verified this session")
                lines.append(f"  • {name}: {state_str}, identity='{ident}', "
                             f"{len(tools)} cached tools ({fresh}), auth={auth_str}")
        if needs_token:
            who = ", ".join(needs_token)
            lines.append(
                f"\nAuth: {who} rejected the connection with 401 and has no bearer token "
                "configured — it will stay offline until you set one. Add it via "
                "keep_add_upstream (re-run with the same name plus bearer_token) or edit "
                "config.json. A bearer is recommended for any upstream's security.")
        # #58: an integration set but the pack absent from disk would otherwise be
        # silent (load_pack returns empty). Surface it with the recovery path.
        for u in STATE.cfg["upstreams"]:
            pk = u.get("integration")
            if pk and not pack_installed(pk):
                lines.append(
                    f"\nPack: upstream '{u['name']}' has integration '{pk}' set but that "
                    "pack is not installed in ~/.mcp-keep/integrations/ — its hints and "
                    f"synthetic tools won't load. Run keep_install_pack name='{pk}' to install "
                    f"it, or keep_remove_pack name='{pk}' to detach it.")
        # #65: pack update detection — installed version vs the cached latest from
        # the integrations repo (refreshed in the background every
        # pack_update_check_seconds; pass check_updates=true to force a live check).
        seen_packs = set()
        for u in STATE.cfg["upstreams"]:
            pk = u.get("integration")
            if not pk or pk in seen_packs or not pack_installed(pk):
                continue
            seen_packs.add(pk)
            installed = pack_version(pk)
            info = STATE.pack_latest.get(pk) or {}
            latest = info.get("latest")
            age = _ago(info["checked_at"]) if info.get("checked_at") else None
            if latest and latest != installed:
                lines.append(
                    f"\nPack update available: '{pk}' {installed or 'unversioned'} → {latest} "
                    f"(latest checked {age}). Run keep_install_pack name='{pk}' to update; "
                    "or keep_status check_updates=true to re-check now.")
            elif latest:
                lines.append(f"\nPack '{pk}': up to date ({installed}, latest checked {age}).")
            elif age is None:
                lines.append(
                    f"\nPack '{pk}': installed {installed or 'unversioned'}; latest not checked "
                    "yet — keep_status check_updates=true to check now.")
        # Self-quieting steer toward start-with-OS (issue #31). Shown only while
        # NOT registered for OS startup — the instant keep_start_with_os flips the
        # flag, this regenerates without the note (state lives in one place, no
        # per-session cadence the long-lived relay couldn't track anyway).
        # Phrased conditionally ("if ... aren't showing up") so it never false-
        # alarms a working native session; the user who actually needs it is the
        # one reading raw keep_status output because their tools didn't surface.
        if not STATE.cfg.get("startup_registered"):
            lines.append(
                "\nNote: if mcp-keep's tools aren't showing up as tools in this chat, the "
                "relay wasn't running when this session started. Restart the chat (or run "
                "/mcp) to surface them — a client connects its tools once, at session start, "
                "so this is how MCP works, not an mcp-keep bug. To avoid it for good, enable "
                "start-with-OS (keep_start_with_os) so the relay is always up first.")
        return ok("\n".join(lines))

    if tool_name == "keep_install_pack":
        a = args or {}
        pack_name = a.get("name", "")
        if not pack_name:
            try:
                packs = list_available_packs()
                return ok("Available packs:\n" + "\n".join(f"  - {p}" for p in packs) +
                          "\n\nCall keep_install_pack with name='<pack>' to install.")
            except Exception as e:
                return error_result(req_id, f"Could not reach GitHub: {e}")
        success, msg = download_pack(pack_name)
        if not success:
            return error_result(req_id, f"Download failed: {msg}")
        post = run_post_install(INTEGRATIONS_DIR / pack_name)
        # #67: downloading alone doesn't link the pack — attach it to an upstream.
        attach_msg = _attach_pack_after_install(pack_name, str(a.get("upstream") or "").strip())
        STATE.cfg = load_config()
        STATE.rebuild_from_cache()
        text = msg
        if attach_msg:
            text += f"\n\n{attach_msg}"
        if post:
            text += f"\n\n{post}"
        return ok(text)

    if tool_name == "keep_start_with_os":
        # Reload fresh so we persist alongside any concurrent change.
        cfg = load_config()
        if cfg.get("startup_registered"):
            return ok("Start-with-OS is already enabled — nothing to do. "
                      "Use keep_disable_start_with_os to turn it off.")
        success, msg = register_startup()
        if not success:
            return error_result(req_id, f"Could not enable start-with-OS: {msg}")
        cfg["startup_registered"] = True
        cfg["startup_asked"] = True
        save_config(cfg)
        STATE.cfg = cfg
        return ok(f"{msg} mcp-keep will be up before your next session — no client "
                  "reload needed from now on.")

    if tool_name == "keep_disable_start_with_os":
        cfg = load_config()
        if not cfg.get("startup_registered"):
            return ok("Start-with-OS isn't enabled — nothing to do.")
        success, msg = unregister_startup()
        if not success:
            return error_result(req_id, f"Could not disable start-with-OS: {msg}")
        cfg["startup_registered"] = False
        save_config(cfg)
        STATE.cfg = cfg
        return ok(f"{msg} mcp-keep will no longer start at login.")

    return error_result(req_id, f"Unknown management tool '{tool_name}'.")

# ---------------------------------------------------------------------------
# Pack download (salvaged, repointed to mcp-keep-integrations)
# ---------------------------------------------------------------------------

def _gh_raw(path: str) -> str:
    url = f"https://raw.githubusercontent.com/{PACKS_REPO}/{PACKS_BRANCH}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "mcp-keep"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()

def _gh_api(path: str):
    url = f"https://api.github.com/repos/{PACKS_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "mcp-keep", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def list_available_packs() -> list[str]:
    return [e["name"] for e in _gh_api("") if e["type"] == "dir" and not e["name"].startswith(".")]

def download_pack(name: str) -> tuple[bool, str]:
    dest = INTEGRATIONS_DIR / name
    dest.mkdir(parents=True, exist_ok=True)
    try:
        files = _gh_api(name)
        got, remote_names = [], set()
        for f in files:
            if f["type"] != "file":
                continue
            (dest / f["name"]).write_text(_gh_raw(f"{name}/{f['name']}"), encoding="utf-8")
            remote_names.add(f["name"]); got.append(f["name"])
        # #65: prune local pack files no longer in the remote pack, so deletions
        # propagate on update. Only top-level files; never the .cache/ dir or
        # dotfiles (the upstream capture cache lives at <pack>/.cache/).
        pruned = []
        for child in dest.iterdir():
            if child.is_file() and not child.name.startswith(".") and child.name not in remote_names:
                child.unlink(); pruned.append(child.name)
        # We just synced to latest — record it so keep_status reflects it at once.
        with STATE.lock:
            STATE.pack_latest[name] = {"latest": pack_version(name), "checked_at": time.time()}
        msg = f"Downloaded pack '{name}' ({len(got)} files): {', '.join(got)}"
        if pruned:
            msg += f"; pruned {len(pruned)} stale: {', '.join(pruned)}"
        ver = pack_version(name)
        if ver:
            msg += f". Version {ver}."
        return True, msg
    except Exception as e:
        return False, str(e)

def run_post_install(pack_path: pathlib.Path) -> str:
    """Describe a pack's post_install.json mcp_server steps WITHOUT applying them.

    A pack may suggest a companion MCP server. We deliberately do NOT write it to
    the client's config: adding an MCP server (often outward-facing) is a privileged
    effect that must be shown and consented per-step, not done silently as a side
    effect of a pack install. So we return a proposal for the assistant to surface;
    the user adds it only on an explicit, separate yes.
    """
    path = pack_path / "post_install.json"
    if not path.exists():
        return ""
    try:
        steps = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return f"Warning: could not read post_install.json: {e}"
    results = []
    for step in steps:
        if step.get("type") == "mcp_server":
            results.append(_describe_companion_server(step))
    return "\n".join(r for r in results if r)

def _describe_companion_server(step: dict) -> str:
    """Surface (do not apply) a suggested companion MCP server from a pack."""
    server_name   = step["server_name"]
    server_config = step["server_config"]
    target = pathlib.Path(step.get("target", "~/.claude/.mcp.json")).expanduser()
    # NOTE: client reads "mcpServers" (not "servers"). Snippet uses the correct key.
    snippet = json.dumps({"mcpServers": {server_name: server_config}}, indent=2)
    url = server_config.get("url") or server_config.get("command") or "(see config)"
    return (
        f"This pack suggests an OPTIONAL companion MCP server, '{server_name}' "
        f"({url}). It was NOT added — adding an MCP server (this one is outward-facing) "
        "is a separate privileged step. Show the user what it is and add it ONLY on an "
        f"explicit yes, by merging this into {target} (create the file if absent):\n"
        f"{snippet}\nThen the client must reload (/mcp) or restart to pick it up. "
        "If they decline, the pack works fine without it.")

# ---------------------------------------------------------------------------
# Security gates — always-on, zero-config (bricks 14, 15, 16)
# ---------------------------------------------------------------------------

def check_origin(headers: dict) -> bool:
    """Reject a present Origin not in the allowlist. Missing Origin = trusted client."""
    origin = headers.get("origin")
    if origin is None:
        return True
    return origin in STATE.cfg.get("allowed_origins", [])

def check_host(headers: dict) -> bool:
    """Reject any Host that isn't loopback (DNS-rebinding defence)."""
    host = (headers.get("host") or "").strip()
    if not host:
        return False
    hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return hostname in ("127.0.0.1", "localhost", "[::1]", "::1")

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class KeepHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # silence default logging
        pass

    # -- security on every request --------------------------------------
    def _gate(self) -> bool:
        h = {k.lower(): v for k, v in self.headers.items()}
        if not check_host(h):
            self._text(403, "forbidden: host not allowed"); return False
        if not check_origin(h):
            self._text(403, "forbidden: origin not allowed"); return False
        return True

    def do_GET(self):
        if not self._gate():
            return
        if "text/event-stream" not in self.headers.get("Accept", ""):
            self._text(200, "mcp-keep running"); return
        # SSE stream: register as a listener so we can push tools/list_changed (#6),
        # with a heartbeat on idle to keep the client from reconnecting in a tight loop.
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        q: queue.Queue = queue.Queue()
        with STATE.lock:
            STATE.listeners.add(q)
        try:
            while True:
                try:
                    frame = q.get(timeout=15)        # pushed notification
                except queue.Empty:
                    frame = b": heartbeat\n\n"        # idle keep-alive
                self.wfile.write(frame)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            with STATE.lock:
                STATE.listeners.discard(q)

    def do_POST(self):
        if not self._gate():
            return
        if self.path != "/mcp":
            self._text(404, "not found"); return

        length = int(self.headers.get("Content-Length", 0))
        cap = int(STATE.cfg.get("max_body_bytes", CONFIG_DEFAULTS["max_body_bytes"]))
        if length > cap:
            self._text(413, f"payload too large (max {cap} bytes)"); return

        raw = self.rfile.read(length)
        client_headers = {k.lower(): v for k, v in self.headers.items()}

        try:
            rpc = json.loads(raw)
        except json.JSONDecodeError:
            self._json(400, {"error": "invalid JSON"}); return

        method = rpc.get("method", "")
        req_id = rpc.get("id")

        if method == "initialize":
            ver = (rpc.get("params") or {}).get("protocolVersion", MCP_PROTOCOL_VERSION)
            instr = STATE.aggregate_instructions() or (
                "mcp-keep active. Tools stay surfaced even while an upstream is offline.")
            self._result(req_id, {
                "protocolVersion": ver,
                "capabilities": {"tools": {"listChanged": True}},
                "serverInfo": {"name": SERVER_NAME, "version": VERSION},
                "instructions": instr,
            })
            log(f"initialize (protocol {ver})")
            return

        if method == "notifications/initialized":
            self.send_response(202); self.end_headers(); return

        if method == "tools/list":
            tools = STATE.aggregate_tools()
            self._result(req_id, {"tools": tools})
            log(f"tools/list -> {len(tools)} tools")
            return

        if method == "tools/call":
            tool_name = (rpc.get("params") or {}).get("name", "")
            if tool_name in MANAGEMENT_TOOL_NAMES:
                args = (rpc.get("params") or {}).get("arguments", {})
                self._raw(handle_management_call(req_id, tool_name, args))
                log(f"{tool_name} -> handled by keep")
                if tool_name in _MUTATING_TOOLS:
                    # #6: a config-mutating tool changed the surface — push a live
                    # refresh now rather than waiting for the next capture tick.
                    maybe_notify_tools_changed()
                return
            u = STATE.upstream_for_tool(tool_name)
            if u is None:
                self._raw(error_result(req_id,
                    f"No upstream knows the tool '{tool_name}'."))
                return
            success, ctype, resp = forward_call(u, raw, client_headers)
            if success:
                self._forward_response(resp, ctype)
                log(f"tools/call '{tool_name}' -> '{u['name']}'")
            else:
                msg = resp.decode(errors="replace").strip() if resp else ""
                if not msg:
                    # Transport failure on a real call = the upstream is down.
                    # Mark it lazily (#40) so the capture loop resumes fast polling
                    # for re-attach — this is how a user actually notices ("UE crashed"),
                    # without us proactively handshaking a healthy upstream every cycle.
                    _mark(u["name"], online=False,
                          auth_required=STATE.upstreams.get(u["name"], {}).get("auth_required", False))
                detail = (f"Upstream '{u['name']}' rejected the call: {msg}"
                          if msg else
                          f"Upstream '{u['name']}' is not running. Start it, then retry '{tool_name}'.")
                self._raw(error_result(req_id, detail))
                log(f"tools/call '{tool_name}' -> '{u['name']}' FAILED")
            return

        # any other method: best-effort forward to the single upstream, else empty
        if len(STATE.cfg["upstreams"]) == 1:
            success, ctype, resp = forward_call(STATE.cfg["upstreams"][0], raw, client_headers)
            if success:
                self._forward_response(resp, ctype); return
        self._result(req_id, {})

    # -- response helpers -----------------------------------------------
    def _result(self, req_id, result):
        self._raw({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _raw(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _forward_response(self, body: bytes, content_type: str):
        """Relay an upstream response to the client. Upstreams may answer in SSE
        (text/event-stream) — re-emit as clean JSON so the client always gets a
        consistent content-type (see issue #26). If the body can't be parsed,
        pass it through faithfully under its real content-type."""
        obj = _parse_mcp_body(body, content_type)
        if obj is not None:
            self._raw(obj)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type or "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status, obj):
        body = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _text(self, status, msg):
        body = msg.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def server_bind(self):
        # Skip HTTPServer.server_bind's socket.getfqdn() reverse-DNS lookup. On a
        # host with no reverse resolver for 127.0.0.1 (e.g. macOS CI runners) that
        # PTR query blocks until it times out — a deterministic ~30s — and stalls
        # startup *after* we log "listening" but *before* the socket accepts. We're
        # loopback-only and server_name is cosmetic, so bind the socket and set the
        # name directly without the lookup.
        socketserver.TCPServer.server_bind(self)
        host, port = self.server_address[:2]
        self.server_name = host
        self.server_port = port

    def handle_error(self, request, client_address):
        if sys.exc_info()[0] in (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        super().handle_error(request, client_address)

# ---------------------------------------------------------------------------
# OS startup registration (salvaged, renamed to mcp-keep)
# ---------------------------------------------------------------------------

_OS = platform.system()

def _launch_args() -> list[str]:
    """Argv that re-launches keep at login.

    When packaged by PyInstaller, the binary *is* the program: sys.executable
    points at the frozen exe and there is no script path to pass (sys.argv[0]
    inside _MEIPASS would be wrong). When running from source, launch the
    interpreter against proxy.py.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--serve"]
    return [sys.executable, str(pathlib.Path(__file__).resolve()), "--serve"]

def _launch_command() -> str:
    return " ".join(f'"{a}"' for a in _launch_args())

# Per-user autostart on Windows. We use the HKCU Run key rather than Task
# Scheduler: schtasks /SC ONLOGON requires an elevated (admin) session, which
# defeats enabling start-with-OS conversationally from a normal chat. The Run
# key is per-user, needs no admin, and is trivially reversible — the same
# no-elevation class as VibeUE's documented shell:startup shortcut, but a single
# registry value instead of a generated .lnk. (Console window at login is the
# #8 windowed-build tradeoff, same as the startup-folder .bat.)
_WIN_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WIN_RUN_VALUE = "mcp-keep"

def register_startup() -> tuple[bool, str]:
    if _OS == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, _WIN_RUN_VALUE, 0, winreg.REG_SZ,
                                  _launch_command())
            return True, "Registered in the HKCU Run key — keep starts at login (per-user, no admin)."
        except OSError as e:
            return False, f"Registry write failed: {e}"
    elif _OS == "Darwin":
        d = pathlib.Path.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        plist = d / "com.mcp-keep.plist"
        plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
    <key>Label</key><string>com.mcp-keep</string>
    <key>ProgramArguments</key><array>
{chr(10).join(f"        <string>{a}</string>" for a in _launch_args())}
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
</dict></plist>
""", encoding="utf-8")
        subprocess.run(["launchctl", "load", str(plist)], capture_output=True)
        return True, "Registered via launchd — keep starts at login."
    else:
        d = pathlib.Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        (d / "mcp-keep.service").write_text(f"""[Unit]
Description=mcp-keep
After=network.target

[Service]
ExecStart={" ".join(_launch_args())}
Restart=on-failure

[Install]
WantedBy=default.target
""", encoding="utf-8")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "mcp-keep"], capture_output=True)
        if r.returncode == 0:
            return True, "Registered via systemd user service — keep starts at login."
        return False, f"systemd failed: {r.stderr.decode().strip()}"

def unregister_startup() -> tuple[bool, str]:
    if _OS == "Windows":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WIN_RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, _WIN_RUN_VALUE)
            return True, "Removed from the HKCU Run key."
        except FileNotFoundError:
            return True, "Was not in the HKCU Run key — nothing to remove."
        except OSError as e:
            return False, f"Registry delete failed: {e}"
    elif _OS == "Darwin":
        plist = pathlib.Path.home() / "Library" / "LaunchAgents" / "com.mcp-keep.plist"
        subprocess.run(["launchctl", "unload", str(plist)], capture_output=True)
        plist.unlink(missing_ok=True)
        return True, "Removed from launchd."
    else:
        subprocess.run(["systemctl", "--user", "disable", "mcp-keep"], capture_output=True)
        (pathlib.Path.home() / ".config" / "systemd" / "user" / "mcp-keep.service").unlink(missing_ok=True)
        return True, "Removed from systemd."

# ---------------------------------------------------------------------------
# Terminal command loop + startup menu
# ---------------------------------------------------------------------------

def cmd_status():
    print(handle_management_call(0, "keep_status", {})["result"]["content"][0]["text"], flush=True)

def cmd_packs():
    print("\n  Fetching packs from GitHub...", flush=True)
    try:
        packs = list_available_packs()
    except Exception as e:
        print(f"  Could not reach GitHub: {e}", flush=True); return
    if not packs:
        print("  No packs found.", flush=True); return
    for i, name in enumerate(packs, 1):
        installed = (INTEGRATIONS_DIR / name).exists()
        print(f"    {i}. {name}{' (installed)' if installed else ''}", flush=True)
    print("    0. Cancel", flush=True)
    try:
        idx = int(input("  Select pack: ").strip())
    except (ValueError, EOFError):
        print("  Cancelled.", flush=True); return
    if idx <= 0 or idx > len(packs):
        print("  Cancelled.", flush=True); return
    name = packs[idx - 1]
    ok, msg = download_pack(name)
    print(f"  {msg}", flush=True)
    if ok:
        post = run_post_install(INTEGRATIONS_DIR / name)
        if post:
            print(f"  {post}", flush=True)
        STATE.cfg = load_config(); STATE.rebuild_from_cache(); _sync_config_mtime()

def cmd_reload():
    n = reload_config("/keep-reload")
    print(f"  Reloaded config + integrations — {n} upstream(s).", flush=True)

def run_setup_menu():
    print("\n  mcp-keep — start with your OS?\n", flush=True)
    if STATE.cfg.get("startup_registered"):
        print("  Currently: ENABLED.  1) Disable   2) Keep enabled", flush=True)
        try:
            choice = input("  Choice [1-2]: ").strip()
        except (EOFError, KeyboardInterrupt):
            choice = "2"
        if choice == "1":
            _, msg = unregister_startup()
            print(f"  {msg}", flush=True)
            STATE.cfg["startup_registered"] = False
        STATE.cfg["startup_asked"] = True
        save_config(STATE.cfg); return

    print("  1) Start with OS (recommended)\n  2) Start manually each time\n  3) Ask next time", flush=True)
    try:
        choice = input("  Choice [1-3]: ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = "3"
    if choice == "1":
        ok, msg = register_startup()
        print(f"  {msg}", flush=True)
        STATE.cfg["startup_registered"] = ok
        STATE.cfg["startup_asked"] = True
    elif choice == "2":
        STATE.cfg["startup_registered"] = False
        STATE.cfg["startup_asked"] = True
    else:
        STATE.cfg["startup_asked"] = False
    save_config(STATE.cfg)

COMMANDS = {
    "/keep-status": cmd_status,
    "/keep-packs":  cmd_packs,
    "/keep-setup":  run_setup_menu,
    "/keep-reload": cmd_reload,
}

def command_loop():
    for line in sys.stdin:
        cmd = line.strip().lower()
        if not cmd:
            continue
        if cmd in ("/keep-quit", "/keep-exit"):
            log("keep stopped."); os._exit(0)
        handler = COMMANDS.get(cmd)
        if handler:
            handler()
        else:
            print(f"  Unknown '{cmd}'. Available: {', '.join(COMMANDS)}, /keep-quit", flush=True)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def wait_ready(timeout: float = 20.0, interval: float = 0.4) -> bool:
    """Poll the local health endpoint until the relay answers, or timeout.

    Console-independent readiness probe (#39): a launcher runs the relay
    detached, then `mcp-keep --wait-ready` blocks here until `GET /mcp`
    returns the running banner — instead of a fixed sleep + single probe that
    false-negatives under the first-launch penalty (AV scan + onedir unpack)
    and tempts a relaunch (which races two processes for the port). Probes
    HTTP, not stdout, so it still works when the relay runs windowless (#8).
    """
    port = int(load_config()["listen_port"])
    url = f"http://127.0.0.1:{port}/mcp"
    deadline = time.monotonic() + timeout
    while True:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:
                if resp.status == 200 and b"mcp-keep running" in resp.read():
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass  # not bound yet — a slow first start is expected, keep polling
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)

def main():
    global STATE
    try:                                  # keep em-dash / bullet output sane on Windows consoles
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    KEEP_HOME.mkdir(parents=True, exist_ok=True)
    INTEGRATIONS_DIR.mkdir(parents=True, exist_ok=True)
    init_log()
    STATE = State()

    port = int(STATE.cfg["listen_port"])
    is_tty = _interactive_console()

    # Report what we loaded from cache (the moat: tools available before any capture)
    total_cached = sum(len(st["manifest"].get("tools", [])) for st in STATE.upstreams.values())
    log(f"mcp-keep {VERSION} — home {KEEP_HOME}")
    log(f"{len(STATE.cfg['upstreams'])} upstream(s) configured, {total_cached} tools served from cache")
    log(f"listening on http://127.0.0.1:{port}/mcp")

    # First-run greeting — this is the "I just downloaded and ran it" moment.
    # mcp-keep does nothing on its own; it is driven by an AI MCP client. Make
    # that obvious instead of sitting silently with an empty tool list.
    # Console-only: under --windowed there is no stdout (would crash) and no
    # window to read it; the windowless sign-of-life lives in the bundle README.
    if is_tty and not STATE.cfg["upstreams"]:
        print(
            "\n"
            "  ──────────────────────────────────────────────────────────────\n"
            "   mcp-keep is running — but it's a tool for an AI assistant.\n"
            "   On its own it does nothing; an AI MCP client has to connect.\n"
            "\n"
            "   You don't need to touch any config files. Just:\n"
            "     1. Open your AI assistant (e.g. Claude).\n"
            "     2. Say: \"Read FIRST_TIME_SETUP.md and set up keep for me.\"\n"
            "        (it's in the same folder as this program)\n"
            "     Your AI will do the rest and walk you through it.\n"
            "\n"
            "   Leave this window open; closing it stops mcp-keep.\n"
            "  ──────────────────────────────────────────────────────────────\n",
            flush=True)

    # Background capture / re-attach. Always run it — even with zero upstreams at
    # boot — so an upstream added later via keep_add_upstream gets attached.
    threading.Thread(target=capture_loop, daemon=True).start()

    # First-run / startup preference
    if is_tty and not STATE.cfg.get("startup_asked", False):
        run_setup_menu()

    if is_tty:
        threading.Thread(target=command_loop, daemon=True).start()

    server = QuietServer(("127.0.0.1", port), KeepHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("keep stopped.")

_USAGE = (
    f"mcp-keep {VERSION}\n"
    "\n"
    "mcp-keep is an AI-driven MCP lifecycle relay - your MCP client (e.g. Claude\n"
    "Code) starts and drives it for you. There's nothing to run by hand here.\n"
    "Open the mcp-keep project in your client and ask it to set up mcp-keep, then\n"
    "manage everything through the keep_* tools (keep_status, keep_add_upstream, ...).\n"
    "\n"
    "  --serve        run the relay (used by your client / start-with-OS; not for humans)\n"
    "  --wait-ready   poll an already-running relay until ready (exit 0=up, 1=timeout)\n"
    "  --version, -v  print version and exit\n"
)

if __name__ == "__main__":
    _args = sys.argv[1:]
    if "--wait-ready" in _args:
        # Readiness probe for launchers (#39): exit 0 once the relay answers,
        # 1 on timeout. Does NOT start a relay — only polls an existing one.
        sys.exit(0 if wait_ready() else 1)
    if "--version" in _args or "-v" in _args:
        print(f"mcp-keep {VERSION}")
        sys.exit(0)
    if "--serve" in _args:
        # The only path that binds a port. Used by the MCP client launch and by
        # start-with-OS registration (_launch_args appends --serve).
        main()
    else:
        # #56: bare/unknown invocation (a human at a terminal) must NOT silently
        # spawn a relay — that orphans a windowless process they can't see. Push
        # them back to the AI-driven flow and exit cleanly without binding.
        print(_USAGE, file=sys.stderr)
        sys.exit(0)
