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
        payload = {"jsonrpc": "2.0", "id": rid, "result": result}
        # Mirror a real Streamable-HTTP server (e.g. Unreal/VibeUE): when the client
        # advertises text/event-stream, answer with SSE and pretty-print the JSON one
        # physical line per `data:` field. This is the multi-line shape that broke
        # capture in issue #23 — keep it in CI so the regression can't return.
        if "text/event-stream" in self.headers.get("Accept", ""):
            pretty = json.dumps(payload, indent=2)
            sse = "".join(f"data: {line}\n" for line in pretty.splitlines()) + "\n"
            body = sse.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(body)
        else:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)


class _Threaded(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def check_windowless(check) -> None:
    """Regression for issue #8: under PyInstaller --windowed, sys.stdout/stderr/stdin
    are None and any bare print() would crash the relay. DEVNULL gives a *valid* handle
    so it can't reproduce this — instead spawn a child that nulls its own std streams,
    then prove the relay still binds, serves, and writes ~/.mcp-keep/keep.log."""
    home = tempfile.mkdtemp(prefix="keep-windowless-")
    port = RELAY_PORT + 100  # isolated from the main test's relay
    pathlib.Path(home, "config.json").write_text(json.dumps(
        {"listen_port": port, "capture_interval_seconds": 5, "upstreams": []}))
    python_dir = str((REPO_ROOT / "python").resolve())
    # Mimic a PyInstaller --windowed process precisely: stdout/stderr are None, and
    # stdin *claims to be a tty* (the quirk that made isatty()-only gating run the
    # interactive setup menu and hang). If the relay still serves, the menu/command
    # loop were correctly skipped despite the lying stdin.
    child = (
        "import sys\n"
        "class _FakeTTY:\n"
        "    def isatty(self): return True\n"
        "    def readline(self, *a): return ''\n"
        "    def read(self, *a): return ''\n"
        "sys.stdout = None; sys.stderr = None; sys.stdin = _FakeTTY()\n"
        f"sys.path.insert(0, {python_dir!r})\n"
        "import proxy\n"
        "proxy.main()\n"
    )
    env = dict(os.environ, MCP_KEEP_HOME=home)
    relay = subprocess.Popen([sys.executable, "-c", child], env=env,
                             stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        deadline, bound = time.time() + 30, False
        while time.time() < deadline:
            if relay.poll() is not None:
                break  # crashed — bound stays False
            try:
                with opener.open(f"http://127.0.0.1:{port}/mcp", timeout=3) as r:
                    if b"mcp-keep running" in r.read():
                        bound = True
                        break
            except Exception:
                time.sleep(0.25)
        check("WINDOWLESS (#8): relay binds + serves with std streams None", bound)
        log = pathlib.Path(home, "keep.log")
        check("WINDOWLESS (#8): keep.log written when no console",
              log.exists() and len(log.read_text(encoding="utf-8").strip()) > 0)
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=5)
        except Exception:
            relay.kill()


def main() -> int:
    home = tempfile.mkdtemp(prefix="keep-itest-")
    # Pre-seed config so capture polls quickly (capture_loop enforces a 5s floor).
    pathlib.Path(home, "config.json").write_text(json.dumps(
        {"listen_port": RELAY_PORT, "capture_interval_seconds": 5, "upstreams": []}))

    # A pack that ships a pre-baked tool cache (#35). An upstream attached to this
    # pack must surface its tools BEFORE it has ever connected — proven below by
    # pointing such an upstream at a dead port and never bringing it online.
    seed_pack = pathlib.Path(home, "integrations", "SeedPack")
    seed_pack.mkdir(parents=True, exist_ok=True)
    (seed_pack / "cache.seed.json").write_text(json.dumps(
        {"serverInfo": {"name": "seed-upstream"},
         "tools": [{"name": "seeded_only_tool", "description": "from pack seed",
                    "inputSchema": {"type": "object", "properties": {}}}]}))

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
        check_windowless(check)

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

        # #35 — pre-baked pack cache: attach an upstream that ships a seed, pointed
        # at a dead port (9 = discard, refused), and never bring it online. Its
        # seeded tool must surface from the pack seed before any successful capture.
        rpc("tools/call", {"name": "keep_add_upstream",
                           "arguments": {"name": "seeded", "host": "127.0.0.1",
                                         "port": 9, "path": "/",
                                         "integration": "SeedPack"}}, 6)
        check("SEED (#35): pack-seeded tool surfaces before first-ever connect",
              "seeded_only_tool" in tool_names())
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
