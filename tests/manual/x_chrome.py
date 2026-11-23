"""Headed Chrome acceptance against recorded X answers. Uses a new temporary profile every run."""
import io
import json
import os
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack, closing, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from rsrc import chrome, cli
from rsrc.channels.x import auth, browser, connection

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


def main():
    csrf, session = (secrets.token_hex(24) for _ in range(2))
    routes = []
    messages = []
    processes = []
    queries = []
    faults = {"mode": None}

    class Fixture(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            path = urlsplit(self.path).path
            routes.append(path)
            status, mime, body = 200, "text/plain", b"ok"
            cookies = []
            if path == "/robots.txt":
                cookies = [f"ct0={csrf}; Path=/", f"probe={session}; HttpOnly; Path=/"]
                if faults["mode"] == "auth_failed":
                    cookies[0] = "ct0=; Max-Age=0; Path=/"
            elif path.startswith("/i/api/graphql/"):
                assert self.headers["x-csrf-token"] == csrf
                assert f"probe={session}" in self.headers["Cookie"]
                mime = "application/json"
                if path.endswith("/SearchTimeline"):
                    query = parse_qs(urlsplit(self.path).query)["variables"][0]
                    queries.append(json.loads(query)["rawQuery"])
                    body = (FIXTURES / "x_graphql_latest.json").read_bytes()
                else:
                    value = (json.loads((FIXTURES / "x_user_tweets.json").read_text())
                             if path.endswith("/UserTweets") else {"rest_id": "123", "core": {"screen_name": "sama"}})
                    body = json.dumps({"data": {"user": {"result": value}}}).encode()
                if faults["mode"] == "blocked":
                    status, mime, body = 403, "text/html", b"<html>challenge</html>"
                if faults["mode"] == "rate_limited":
                    status = 429
            self.send_response(status)
            for cookie in cookies:
                self.send_header("Set-Cookie", cookie)
            self.send_header("Content-Type", mime)
            self.end_headers()
            self.wfile.write(body)

        do_POST = do_GET

    receive = chrome.WebSocket.receive
    program = browser.page_program
    popen = subprocess.Popen

    def capture(ws, deadline):
        message = receive(ws, deadline)
        messages.append(message)
        assert csrf not in message and session not in message
        return message

    def start(args, **kwargs):
        proc = popen(args, **kwargs)
        processes.append(proc)
        return proc

    def stop():
        for proc in processes:
            if proc.poll() is None:
                proc.terminate()
            proc.wait(timeout=10)

    with tempfile.TemporaryDirectory() as root, ThreadingHTTPServer(("127.0.0.1", 0), Fixture) as server, ExitStack() as stack:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        stack.callback(server.shutdown)
        stack.callback(stop)
        origin = f"http://127.0.0.1:{server.server_port}"
        stack.enter_context(patch.object(connection, "ORIGIN", origin))
        stack.enter_context(patch.object(browser, "page_program", lambda p: program(p).replace("https://x.com", origin)))
        stack.enter_context(patch.object(chrome.WebSocket, "receive", capture))
        stack.enter_context(patch.object(subprocess, "Popen", start))
        environ = {"RSRC_X_ENV": root + "/config/x.env", "XDG_DATA_HOME": root, "XDG_CACHE_HOME": root}
        def run(argv, fault=None):
            faults["mode"] = fault
            messages.clear()
            routes.clear()
            queries.clear()
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(out):
                code = cli.main(argv.split(), cli.Context(environ=environ))
            assert csrf not in out.getvalue() and session not in out.getvalue()
            doc = json.loads(out.getvalue())
            assert code == int(bool(fault)) and doc["ok"] == (not fault), (code, doc["error"])
            if argv.startswith("x search from:"):
                assert queries == [argv.split()[2]]
                assert doc["items"][0]["author"] == argv.split()[2][5:]
            if fault:
                assert doc["error"]["code"] == fault
            print(argv, len(messages), "CDP messages;", len(doc["items"]), "items;", routes,
                  doc["error"] or doc["meta"]["notes"])
            if messages:
                replies = [json.loads(m) for m in messages]
                assert replies[-1]["result"] == {"success": True}
                target = next(obj["result"]["targetId"] for obj in replies if "targetId" in obj.get("result", {}))
                endpoint, _ = auth.chrome_settings(environ)
                with closing(chrome.WebSocket(chrome.discover(endpoint))) as ws:
                    for seq in range(20):
                        ws.send(json.dumps({"id": seq, "method": "Target.getTargetInfo",
                                            "params": {"targetId": target}}).encode())
                        reply = json.loads(ws.receive(time.monotonic() + 3))
                        if reply.get("error", {}).get("message") == "No target with given id found":
                            break
                        time.sleep(.05)
                    else:
                        raise AssertionError("command tab still exists")
                counted = sum(p == "/robots.txt" or p.startswith("/i/api/graphql/") for p in routes)
                assert doc["meta"]["attempts"][0]["requests"] == counted
            return doc

        run("x connect")
        endpoint, _ = auth.chrome_settings(environ)
        run("check x")
        for argv in ["x search compiler --limit 1", "x search parser --top --limit 1",
                     "x search from:kylelittle --limit 1", "x timeline sama --limit 2"]:
            doc = run(argv)
            assert doc["items"] and doc["backend"] == "x-chrome"
        run("x connect --cdp " + endpoint)
        # Exercise the installed console script in fresh processes as well. Only these children
        # import the temporary startup module; the installed package has no fixture-origin setting.
        Path(root, "sitecustomize.py").write_text(
            "from rsrc.channels.x import browser, connection\n"
            f"connection.ORIGIN = {origin!r}\n"
            "page = browser.page_program\n"
            "browser.page_program = lambda p: page(p).replace('https://x.com', connection.ORIGIN)\n"
        )
        child_env = {**os.environ, **environ, "RSRC_X_SEARCH": "", "PYTHONPATH": root}
        binary = Path(sys.executable).with_name("rsrc")
        for argv in [["x", "connect", "--cdp", endpoint], ["check", "x"],
                     ["x", "search", "compiler", "--limit", "1"],
                     ["x", "search", "parser", "--top", "--limit", "1"],
                     ["x", "search", "from:kylelittle", "--limit", "1"],
                     ["x", "timeline", "sama", "--limit", "2"]]:
            # Bypass the launch tracker: it owns Chrome processes only.
            with popen([binary, *argv], env=child_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True) as proc:
                stdout, stderr = proc.communicate(timeout=30)
                assert proc.returncode == 0 and json.loads(stdout)["ok"]
                assert all(v not in stdout + stderr for v in (csrf, session))
        print("Installed console script: six commands passed")
        for fault in ("auth_failed", "blocked", "rate_limited"):
            for argv in ("x search compiler --limit 1", "x timeline sama --limit 2"):
                run(argv, fault)
        run("x connect")
        stop()
        assert connection.STARTED in run("x search restart --limit 1")["meta"]["notes"]
        print("Chrome version:", next(json.loads(m)["result"]["product"] for m in messages if '"product"' in m))
    assert all(proc.poll() is not None for proc in processes)


if __name__ == "__main__":
    main()
