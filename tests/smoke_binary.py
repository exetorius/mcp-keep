#!/usr/bin/env python3
"""
smoke_binary.py — boot a *packaged* mcp-keep binary and prove it serves.

Run from CI after PyInstaller builds the one-file binary:

    python tests/smoke_binary.py dist/mcp-keep        # or dist/mcp-keep.exe

Unlike integration_test.py (which runs proxy.py under the interpreter), this
exercises the frozen artifact end-to-end: it confirms the bundle is complete
(no missing stdlib module), the relay actually listens, and tools/list returns
the always-on management tools. It does NOT re-test the moat — that's covered
by the source-level integration test.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import time
import urllib.request

PORT = int(os.environ.get("RELAY_PORT", "8089"))
BASE = f"http://127.0.0.1:{PORT}"

# Empty ProxyHandler: some CI runner images (notably macOS) set http_proxy and
# urllib honors it, which would route our 127.0.0.1 calls through a proxy and
# hang. Talk to the relay directly.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: smoke_binary.py <path-to-binary>", flush=True)
        return 2
    binary = pathlib.Path(sys.argv[1]).resolve()
    if not binary.exists():
        print(f"binary not found: {binary}", flush=True)
        return 2

    home = tempfile.mkdtemp(prefix="keep-smoke-")
    env = dict(os.environ, MCP_KEEP_HOME=home)
    log_path = pathlib.Path(home, "relay.log")
    log_file = open(log_path, "w", encoding="utf-8")

    # stdin=DEVNULL → is_tty False → relay skips the setup menu / command loop
    # and goes straight to serve_forever (no blocking input()).
    relay = subprocess.Popen([str(binary)], env=env, stdin=subprocess.DEVNULL,
                             stdout=log_file, stderr=subprocess.STDOUT)

    def dump_log(msg):
        log_file.flush()
        print(msg, flush=True)
        print(log_path.read_text(encoding="utf-8", errors="replace"), flush=True)

    try:
        # Readiness poll — generous window; Apple-Silicon runners are slow to
        # even start a frozen process.
        started = time.time()
        deadline = started + 90
        ready = False
        while time.time() < deadline:
            if relay.poll() is not None:
                dump_log(f"binary exited early with code {relay.returncode}")
                return 1
            try:
                with OPENER.open(f"{BASE}/mcp", timeout=3) as r:
                    if r.status == 200:
                        ready = True
                        break
            except Exception:
                time.sleep(0.5)
        if not ready:
            dump_log("binary did not become ready within 90s")
            return 1
        print(f"(binary ready after {time.time() - started:.1f}s)", flush=True)

        # tools/list must return the always-on management tools — proves the
        # bundle can answer MCP, not just open a socket.
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list",
                           "params": {}}).encode()
        req = urllib.request.Request(f"{BASE}/mcp", data=body,
                                     headers={"Content-Type": "application/json"})
        with OPENER.open(req, timeout=15) as r:
            names = [t["name"] for t in json.loads(r.read())["result"]["tools"]]
        print("tools/list ->", names, flush=True)
        for required in ("keep_status", "keep_add_upstream"):
            if required not in names:
                dump_log(f"missing management tool: {required}")
                return 1

        print("SMOKE OK", flush=True)
        return 0
    finally:
        relay.terminate()
        try:
            relay.wait(timeout=10)
        except Exception:
            relay.kill()


if __name__ == "__main__":
    sys.exit(main())
