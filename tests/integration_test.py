#!/usr/bin/env python3
"""End-to-end integration test for mcp-keep.

Spins up a fake upstream MCP server + the real relay, then exercises the whole
moat over HTTP: AI-native onboarding tools, re-attach/capture, cache-when-down,
tool-call routing, offline detection, and clean errors when an upstream is down.

Self-contained (stdlib only), headless, and CI-friendly: it captures the relay's
output and prints it on failure, and exits non-zero if any check fails.

Run locally:   python tests/integration_test.py
Env overrides: RELAY_PORT (default 8089), UP_PORT (default 9555)
"""
import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

RELAY_PORT = int(os.environ.get("RELAY_PORT", "8089"))
UP_PORT    = int(os.environ.get("UP_PORT", "9555"))
REPO_ROOT  = pathlib.Path(__file__).resolve().parent.parent
PROXY      = REPO_ROOT / "python" / "proxy.py"


# --- fake upstream MCP server: initialize + tools/list + tools/call ----------
class _Upstream(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        req = json.loads(self.rfile.read(n) or b"{}")
        method, rid = req.get("method"), req.get("id")
        if method == "initialize":
            result = {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}},
                      "serverInfo": {"name": "fake-upstream", "version": "0.1"}}
        elif method == "tools/list":
            result = {"tools": [{"name": "fake_echo", "description": "echo",
                                 "inputSchema": {"type": "object", "properties": {}}}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "ECHO OK from upstream"}]}
        else:
            result = {}
        body = json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def main() -> int:
    home = tempfile.mkdtemp(prefix="keep-itest-")
    # Pre-seed config so capture polls quickly (capture_loop enforces a 5s floor).
    pathlib.Path(home, "config.json").write_text(json.dumps(
        {"listen_port": RELAY_PORT, "capture_interval_seconds": 5, "upstreams": []}))

    upstream = _Threaded(("127.0.0.1", UP_PORT), _Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()

    log_path = pathlib.Path(home, "relay.log")
    log_file = open(log_path, "w", encoding="utf-8")
    env = dict(os.environ, MCP_KEEP_HOME=home)
    relay = subprocess.Popen([sys.executable, str(PROXY)], env=env,
                             stdin=subprocess.DEVNULL, stdout=log_file, stderr=subprocess.STDOUT)

    # Talk to the relay directly — never via a proxy. Some CI runner images (notably
    # macOS) set http_proxy/HTTP_PROXY, and urllib honors it by default, which would
    # route our 127.0.0.1 calls through a proxy and hang. An empty ProxyHandler
    # disables that for this opener.
    base = f"http://127.0.0.1:{RELAY_PORT}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def rpc(method, params=None, rid=1, timeout=15):
        body = json.dumps({"jsonrpc": "2.0", "id": rid,
                           "method": method, "params": params or {}}).encode()
        req = urllib.request.Request(f"{base}/mcp", data=body,
                                     headers={"Content-Type": "application/json"})
        with opener.open(req, timeout=timeout) as r:
            return json.loads(r.read())

    # Readiness poll — wait for the relay to actually serve before asserting,
    # instead of a fixed sleep. Generous window because CI runners (notably
    # macOS / Apple Silicon) can be slow to even start the process.
    started = time.time()
    deadline = started + 90
    ready = False
    while time.time() < deadline:
        try:
            with opener.open(f"{base}/mcp", timeout=3) as r:
                if r.status == 200:
                    ready = True
                    break
        except Exception:
            time.sleep(0.5)
    if not ready:
        log_file.flush()
        print("relay did not become ready within 90s", flush=True)
        print(log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
        relay.terminate()
        return 1
    print(f"(relay ready after {time.time() - started:.1f}s)", flush=True)

    def tool_names():
        return [t["name"] for t in rpc("tools/list")["result"]["tools"]]

    fails = []

    def check(label, cond):
        print(("PASS" if cond else "FAIL"), "-", label, flush=True)
        if not cond:
            fails.append(label)

    try:
        names = tool_names()
        check("first-run shows keep_welcome", "keep_welcome" in names)
        check("first-run shows keep_add_upstream", "keep_add_upstream" in names)

        rpc("tools/call", {"name": "keep_add_upstream",
                           "arguments": {"name": "fake", "host": "127.0.0.1",
                                         "port": UP_PORT, "path": "/"}}, 2)

        deadline, surfaced = time.time() + 15, False
        while time.time() < deadline:
            if "fake_echo" in tool_names():
                surfaced = True
                break
            time.sleep(1)
        check("RE-ATTACH: 'fake_echo' surfaces after add", surfaced)
        check("keep_welcome disappears once configured", "keep_welcome" not in tool_names())

        call = rpc("tools/call", {"name": "fake_echo", "arguments": {}}, 3)
        text = call.get("result", {}).get("content", [{}])[0].get("text", "")
        check("ROUTING: tools/call fake_echo reaches upstream", "ECHO OK" in text)

        # Truly take the upstream down (close the socket so the port is refused).
        upstream.shutdown()
        upstream.server_close()
        time.sleep(8)  # > one capture poll, so 'online' flips to False

        check("CACHE-WHEN-DOWN: fake_echo still listed while offline",
              "fake_echo" in tool_names())
        status = rpc("tools/call", {"name": "keep_status", "arguments": {}}, 4)
        status_text = status["result"]["content"][0]["text"]
        check("keep_status reports OFFLINE / serving cache", "OFFLINE" in status_text)
        down = rpc("tools/call", {"name": "fake_echo", "arguments": {}}, 5)
        check("clear error when calling tool of a down upstream",
              "not running" in json.dumps(down) or "error" in down)
    except Exception as e:  # noqa: BLE001 - surface anything as a failure
        check(f"unexpected exception: {e!r}", False)
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=5)
        except Exception:
            relay.kill()
        log_file.close()

    if fails:
        print("\n--- relay log ---", flush=True)
        print(log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
        print(f"\nRESULT: {len(fails)} FAILED: {fails}", flush=True)
        return 1
    print("\nRESULT: ALL PASSED", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
