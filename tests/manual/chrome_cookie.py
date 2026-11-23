"""Run against an explicitly opened test Chrome. Only the loopback fixture receives traffic."""
import json
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from rsrc.chrome import Tab, WebSocket, discover
from contextlib import closing


def record(endpoint, fixture):
    marker = secrets.token_hex(32)
    seen = []
    received = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            if self.path == "/robots.txt":
                self.send_header("Set-Cookie", f"probe={marker}; HttpOnly; SameSite=Strict; Path=/")
            if self.path == "/check":
                seen.append(self.headers.get("Cookie") == "probe=" + marker)
                self.send_header("Set-Cookie", "probe=; HttpOnly; Max-Age=0; Path=/")
            self.end_headers()
            self.wfile.write(b"ok")

    read = WebSocket.receive

    def capture(ws, deadline):
        message = read(ws, deadline)
        received.append(message)
        return message

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(WebSocket, "receive", capture):
                with Tab(endpoint) as tab:
                    tab.navigate(f"http://127.0.0.1:{server.server_port}/robots.txt")
                    result = tab.evaluate("fetch('/check').then(r => r.ok)")
                    assert result is True and seen == [True]
                target = next(obj["result"]["targetId"] for obj in map(json.loads, received)
                              if "targetId" in obj.get("result", {}))
                with closing(WebSocket(discover(endpoint))) as check:
                    check.send(json.dumps({"id": 1, "method": "Target.getTargetInfo",
                                           "params": {"targetId": target}}).encode())
                    reply = json.loads(check.receive(time.monotonic() + 3))
                    assert reply.get("error", {}).get("message") == "No target with given id found"
        finally:
            server.shutdown()
            thread.join()
    assert received and all(marker not in m for m in received), "Synthetic cookie reached protocol client"
    messages = [json.loads(m) for m in received]
    # Keep response shapes and event ordering. Map ephemeral protocol handles to fixture handles.
    aliases = {}
    def normalize(obj):
        if isinstance(obj, list):
            return [normalize(i) for i in obj]
        if isinstance(obj, dict):
            return {k: (aliases.setdefault(v, f"handle-{len(aliases)}") if k in
                    {"sessionId", "targetId", "frameId", "loaderId", "browserContextId"} else normalize(v))
                    for k, v in obj.items()}
        return obj
    Path(fixture).write_text(json.dumps(normalize(messages), indent=2) + "\n")
    print(f"{len(received)} CDP messages, server received cookie, zero occurrences in CDP")


if __name__ == "__main__":
    record(*sys.argv[1:])
