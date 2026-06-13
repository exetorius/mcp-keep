#!/usr/bin/env python3
"""
mcp-auth-relay
==============
Lightweight MCP relay that sits between your MCP client and a local MCP server.

What it does:
  - Listens on a local port (default 8089)
  - Injects a bearer token into every upstream request (client never sees or sends it)
  - Serves tools/list from a manifest file even when the upstream server is offline
  - Appends integration-supplied hints to tool descriptions at the proxy layer
  - Answers initialize directly (no upstream needed)

Setup:
  1. Copy config.example.json to config.json and fill in your values
  2. python proxy.py
  3. Point your MCP client at http://127.0.0.1:8089/mcp

Commands (type while running):
  /packs   — browse and install integration packs
  /status  — show current config and integration state
  /reload  — reload config and integration without restart
  /quit    — stop the relay
"""

import json
import os
import pathlib
import sys
import time
import threading
import urllib.request
import urllib.error
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_CONFIG_PATH = pathlib.Path(__file__).parent / "config.json"
_INTEGRATIONS_DIR = pathlib.Path(__file__).parent.parent / "integrations"
_PACKS_REPO = "exetorius/mcp-auth-relay-integrations"
_PACKS_BRANCH = "main"

def _load_config() -> dict:
    defaults = {
        "bearer_token":   "",
        "proxy_port":     8089,
        "upstream_host":  "127.0.0.1",
        "upstream_port":  8088,
        "manifest_path":  "",
        "integration":    "",
        "server_name":    "mcp-auth-relay",
        "instructions":   "",
    }
    try:
        with open(_CONFIG_PATH) as f:
            cfg = json.load(f)
            return {**defaults, **cfg}
    except FileNotFoundError:
        return defaults
    except Exception as e:
        print(f"[relay] WARNING: could not read config.json: {e}", flush=True)
        return defaults

_CFG = _load_config()

PROXY_PORT    = int(_CFG["proxy_port"])
UPSTREAM_URL  = f"http://{_CFG['upstream_host']}:{_CFG['upstream_port']}/mcp"
UPSTREAM_PORT = int(_CFG["upstream_port"])
UPSTREAM_HOST = _CFG["upstream_host"]
BEARER_TOKEN  = _CFG["bearer_token"]
SERVER_NAME   = _CFG["server_name"]
INSTRUCTIONS  = _CFG["instructions"]

MANIFEST_PATH = pathlib.Path(os.path.expandvars(_CFG["manifest_path"])) if _CFG["manifest_path"] else None

# ---------------------------------------------------------------------------
# Integration pack
# ---------------------------------------------------------------------------

_INTEGRATION_DIR: pathlib.Path | None = None
_TOOL_HINTS: dict[str, str] = {}
_SYNTHETIC_TOOLS: list[dict] = []

def _load_integration(name: str) -> str:
    """Load a named integration pack. Returns a status line for display."""
    global _INTEGRATION_DIR, _TOOL_HINTS, _SYNTHETIC_TOOLS, INSTRUCTIONS

    _INTEGRATION_DIR = None
    _TOOL_HINTS.clear()
    _SYNTHETIC_TOOLS.clear()

    if not name:
        return ""

    candidate = _INTEGRATIONS_DIR / name
    if not candidate.exists():
        return f"Integration pack '{name}' not found at {candidate}"

    _INTEGRATION_DIR = candidate
    parts = []

    hints_path = candidate / "hints.json"
    if hints_path.exists():
        try:
            with open(hints_path) as f:
                _TOOL_HINTS.update(json.load(f))
            parts.append(f"{len(_TOOL_HINTS)} hints")
        except Exception as e:
            parts.append(f"hints ERROR: {e}")

    synth_path = candidate / "synthetic_tools.json"
    if synth_path.exists():
        try:
            with open(synth_path) as f:
                _SYNTHETIC_TOOLS.extend(json.load(f))
            parts.append(f"{len(_SYNTHETIC_TOOLS)} synthetic tools")
        except Exception as e:
            parts.append(f"synthetic_tools ERROR: {e}")

    instr_path = candidate / "instructions.md"
    if instr_path.exists() and not INSTRUCTIONS:
        try:
            INSTRUCTIONS = instr_path.read_text(encoding="utf-8")
            parts.append(f"instructions ({len(INSTRUCTIONS)} bytes)")
        except Exception as e:
            parts.append(f"instructions ERROR: {e}")

    return f"Integration '{name}' loaded — " + ", ".join(parts) if parts else f"Integration '{name}' loaded (empty pack)"

_integration_status = _load_integration(_CFG.get("integration", ""))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)

# ---------------------------------------------------------------------------
# Pack installer
# ---------------------------------------------------------------------------

def _github_raw(path: str) -> str:
    url = f"https://raw.githubusercontent.com/{_PACKS_REPO}/{_PACKS_BRANCH}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "mcp-auth-relay"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return r.read().decode()

def _github_api(path: str) -> list | dict:
    url = f"https://api.github.com/repos/{_PACKS_REPO}/contents/{path}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "mcp-auth-relay",
        "Accept": "application/vnd.github+json",
    })
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())

def _list_available_packs() -> list[str]:
    entries = _github_api("")
    return [e["name"] for e in entries if e["type"] == "dir" and not e["name"].startswith(".")]

def _download_pack(name: str) -> tuple[bool, str]:
    dest = _INTEGRATIONS_DIR / name
    dest.mkdir(parents=True, exist_ok=True)
    try:
        files = _github_api(name)
        downloaded = []
        for f in files:
            if f["type"] != "file":
                continue
            content = _github_raw(f"{name}/{f['name']}")
            (dest / f["name"]).write_text(content, encoding="utf-8")
            downloaded.append(f["name"])
        return True, f"Downloaded {len(downloaded)} files: {', '.join(downloaded)}"
    except Exception as e:
        return False, str(e)

def _save_integration_to_config(name: str) -> None:
    cfg = {}
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH) as f:
                cfg = json.load(f)
        except Exception:
            pass
    cfg["integration"] = name
    with open(_CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def cmd_packs() -> None:
    print("\n  Fetching available packs from GitHub...", flush=True)
    try:
        packs = _list_available_packs()
    except Exception as e:
        print(f"  Could not reach GitHub: {e}", flush=True)
        return

    if not packs:
        print("  No packs found.", flush=True)
        return

    print(f"\n  Available integration packs:\n", flush=True)
    for i, name in enumerate(packs, 1):
        installed = (_INTEGRATIONS_DIR / name).exists()
        tag = " (installed)" if installed else ""
        print(f"    {i}. {name}{tag}", flush=True)
    print(f"    0. Cancel\n", flush=True)

    try:
        choice = input("  Select pack number: ").strip()
        idx = int(choice)
        if idx == 0:
            print("  Cancelled.", flush=True)
            return
        if idx < 1 or idx > len(packs):
            print("  Invalid selection.", flush=True)
            return
    except (ValueError, EOFError):
        print("  Cancelled.", flush=True)
        return

    name = packs[idx - 1]
    print(f"\n  Downloading '{name}' pack...", flush=True)
    ok, msg = _download_pack(name)
    if not ok:
        print(f"  Download failed: {msg}", flush=True)
        return

    print(f"  {msg}", flush=True)
    _save_integration_to_config(name)
    status = _load_integration(name)
    print(f"  {status}", flush=True)
    print(f"  Integration active. config.json updated — token and ports preserved.\n", flush=True)

def cmd_status() -> None:
    print(f"\n  mcp-auth-relay status", flush=True)
    print(f"  Listening:    http://127.0.0.1:{PROXY_PORT}/mcp", flush=True)
    print(f"  Upstream:     {UPSTREAM_URL}", flush=True)
    print(f"  Token:        {'set' if BEARER_TOKEN else 'NOT SET'}", flush=True)
    print(f"  Manifest:     {MANIFEST_PATH or 'not configured'}", flush=True)
    if MANIFEST_PATH:
        tools = load_manifest()
        print(f"  Tools:        {len(tools)} from manifest + {len(_SYNTHETIC_TOOLS)} synthetic", flush=True)
    if _INTEGRATION_DIR:
        print(f"  Integration:  {_INTEGRATION_DIR.name} ({len(_TOOL_HINTS)} hints, {len(_SYNTHETIC_TOOLS)} synthetic tools)", flush=True)
    else:
        print(f"  Integration:  none — type /packs to install one", flush=True)
    print(flush=True)

def cmd_reload() -> None:
    global _CFG, BEARER_TOKEN, INSTRUCTIONS
    _CFG = _load_config()
    BEARER_TOKEN = _CFG["bearer_token"]
    INSTRUCTIONS = _CFG["instructions"]
    status = _load_integration(_CFG.get("integration", ""))
    print(f"  Reloaded config. {status or 'No integration.'}", flush=True)

# ---------------------------------------------------------------------------
# Interactive command loop (stdin, runs in a background thread)
# ---------------------------------------------------------------------------

COMMANDS = {
    "/packs":  cmd_packs,
    "/status": cmd_status,
    "/reload": cmd_reload,
}

def _command_loop() -> None:
    for line in sys.stdin:
        cmd = line.strip().lower()
        if not cmd:
            continue
        if cmd in ("/quit", "/exit", "quit", "exit"):
            log("Relay stopped.")
            os._exit(0)
        handler = COMMANDS.get(cmd)
        if handler:
            handler()
        else:
            print(f"  Unknown command '{cmd}'. Available: {', '.join(COMMANDS)} /quit", flush=True)

# ---------------------------------------------------------------------------
# Manifest + hint helpers
# ---------------------------------------------------------------------------

def load_manifest() -> list:
    if not MANIFEST_PATH or not MANIFEST_PATH.exists():
        return []
    for encoding in ("utf-8-sig", "utf-16", "utf-8"):
        try:
            with open(MANIFEST_PATH, encoding=encoding) as f:
                return json.load(f)
        except (UnicodeDecodeError, UnicodeError):
            continue
        except Exception as e:
            log(f"Warning: could not read manifest ({encoding}): {e}")
            break
    return []


def apply_hints(tools: list) -> list:
    for tool in tools:
        hint = _TOOL_HINTS.get(tool.get("name", ""))
        if hint:
            tool = dict(tool)
            tool["description"] = tool.get("description", "") + hint
        yield tool

# ---------------------------------------------------------------------------
# Upstream forwarding
# ---------------------------------------------------------------------------

def forward_to_upstream(body_bytes: bytes, headers: dict) -> tuple[bool, bytes]:
    forward_headers = {
        "Content-Type":     "application/json",
        "Accept":           "application/json",
        "X-MCP-Auth-Relay": "true",
        "Connection":       "close",
    }
    if BEARER_TOKEN:
        forward_headers["Authorization"] = f"Bearer {BEARER_TOKEN}"
    for key in ("mcp-protocol-version",):
        if key in headers:
            forward_headers[key] = headers[key]

    last_exc = None
    for attempt in range(2):
        try:
            req = urllib.request.Request(
                UPSTREAM_URL, data=body_bytes, headers=forward_headers, method="POST",
            )
            with urllib.request.urlopen(req, timeout=120) as resp:
                return True, resp.read()
        except urllib.error.HTTPError as e:
            body = e.read()
            log(f"Upstream returned HTTP {e.code}: {body[:200]}")
            return False, body
        except (urllib.error.URLError, socket.timeout, OSError) as exc:
            last_exc = exc
            if attempt == 0:
                log(f"Connection error (attempt {attempt + 1}), retrying: {exc}")
    log(f"Upstream unreachable after 2 attempts: {last_exc}")
    return False, b""


def upstream_error_response(req_id, tool_name: str, upstream_message: str = "") -> dict:
    if upstream_message:
        text = (
            f"Upstream server rejected the request: {upstream_message}\n"
            f"Check that bearer_token in config.json matches the upstream server's expected token."
        )
    else:
        text = (
            f"Upstream server is not running.\n"
            f"Please start your MCP server, then retry '{tool_name}'."
        )
    return {
        "jsonrpc": "2.0", "id": req_id,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }

# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class RelayHandler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        pass

    def _send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, MCP-Protocol-Version, mcp-session-id")

    def do_OPTIONS(self):
        self.send_response(200)
        self._send_cors()
        self.end_headers()

    def do_GET(self):
        accept = self.headers.get("Accept", "")
        if "text/event-stream" not in accept:
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self._send_cors()
            self.end_headers()
            self.wfile.write(b"mcp-auth-relay running")
            return

        log("SSE stream opened")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self._send_cors()
        self.end_headers()
        try:
            while True:
                self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
                time.sleep(15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            log("SSE stream closed")

    def do_POST(self):
        if self.path != "/mcp":
            self.send_response(404)
            self.end_headers()
            return

        length = int(self.headers.get("Content-Length", 0))
        raw_body = self.rfile.read(length)
        lower_headers = {k.lower(): v for k, v in self.headers.items()}

        try:
            rpc = json.loads(raw_body)
        except json.JSONDecodeError:
            self._json(400, {"error": "Invalid JSON"})
            return

        method = rpc.get("method", "")
        req_id = rpc.get("id")

        if method == "initialize":
            client_version = (rpc.get("params") or {}).get("protocolVersion", "2024-11-05")
            log(f"initialize (protocol {client_version})")
            self._jsonrpc(req_id, {
                "protocolVersion": client_version,
                "capabilities": {"tools": {}},
                "serverInfo": {
                    "name":    SERVER_NAME,
                    "version": "1.0.0",
                    "instructions": INSTRUCTIONS or (
                        f"MCP relay active. Upstream: {UPSTREAM_URL}. "
                        "Tools are forwarded to the upstream server with auth injected automatically."
                    ),
                },
            })
            return

        if method == "notifications/initialized":
            self.send_response(202)
            self.end_headers()
            return

        if method == "tools/list":
            tools = list(apply_hints(load_manifest())) + _SYNTHETIC_TOOLS
            log(f"tools/list -> {len(tools)} tools ({len(tools) - len(_SYNTHETIC_TOOLS)} manifest + {len(_SYNTHETIC_TOOLS)} synthetic)")
            self._jsonrpc(req_id, {"tools": tools})
            return

        success, response_bytes = forward_to_upstream(raw_body, lower_headers)
        if success:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self._send_cors()
            self.end_headers()
            self.wfile.write(response_bytes)
            log(f"{method} -> upstream")
        else:
            upstream_msg = response_bytes.decode(errors="replace").strip() if response_bytes else ""
            log(f"{method} -> upstream error: {upstream_msg or '(unreachable)'}")
            if method == "tools/call":
                tool_name = (rpc.get("params") or {}).get("name", "unknown")
                self._raw_json(upstream_error_response(req_id, tool_name, upstream_msg))
            else:
                self._jsonrpc(req_id, {})

    def _jsonrpc(self, req_id, result: dict):
        self._raw_json({"jsonrpc": "2.0", "id": req_id, "result": result})

    def _raw_json(self, data: dict):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_cors()
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: dict):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        if exc_type in (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return
        super().handle_error(request, client_address)

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not _CONFIG_PATH.exists():
        log(f"WARNING: config.json not found — copy config.example.json and fill in your values.")
    elif not BEARER_TOKEN:
        log("WARNING: bearer_token is empty — upstream requests will be unauthenticated.")
    else:
        log("Bearer token loaded.")

    if MANIFEST_PATH:
        if MANIFEST_PATH.exists():
            log(f"Manifest: {len(load_manifest())} tools loaded.")
        else:
            log(f"Manifest not found at {MANIFEST_PATH} — tools/list will be empty until the upstream server writes it.")
    else:
        log("No manifest_path configured — tools/list served from upstream only.")

    if _integration_status:
        log(_integration_status)

    log(f"mcp-auth-relay started — listening on http://127.0.0.1:{PROXY_PORT}/mcp")
    log(f"Forwarding to upstream at {UPSTREAM_URL}")

    if not _CFG.get("integration"):
        print(
            f"\n  No integration pack loaded. Type /packs to browse and install packs,\n"
            f"  or /status for current configuration.\n",
            flush=True,
        )

    # Start stdin command loop if running interactively
    if sys.stdin and sys.stdin.isatty():
        threading.Thread(target=_command_loop, daemon=True).start()

    server = QuietThreadingHTTPServer(("127.0.0.1", PROXY_PORT), RelayHandler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("Relay stopped.")
