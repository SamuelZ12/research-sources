"""A synchronous, loopback-only Chrome transport for one command-owned tab."""
import base64
import hashlib
import http.client
import json
import os
import re
import signal
import socket
import struct
import threading
import time
from contextlib import ExitStack, contextmanager
from urllib.parse import urlsplit

from rsrc.http import HttpError


MAX_MESSAGE = 8 * 1024 * 1024
UNREACHABLE = "Cannot reach Chrome. Start Chrome and check the debugging address."
WRONG_BROWSER = "The endpoint did not report a Chrome debugging browser."


class ChromeError(HttpError):
    """A local debugging connection or command failed."""


def address(url, scheme):
    try:
        parts = urlsplit(url)
        if (parts.scheme == scheme and parts.hostname in ("127.0.0.1", "::1", "localhost")
                and parts.port and not (parts.username or parts.password or "@" in parts.netloc
                or parts.query or parts.fragment) and url.isascii() and all(32 < ord(c) < 127 for c in url)):
            if ((scheme == "http" and parts.path in ("", "/")) or
                    (scheme == "ws" and re.fullmatch(r"/devtools/browser(?:/[\w-]+)?", parts.path))):
                return parts
    except ValueError:
        pass
    raise ChromeError("Chrome needs a loopback endpoint with an explicit port.")


def host(parts):
    return "127.0.0.1" if parts.hostname == "localhost" else parts.hostname


def discover(endpoint):
    parts = address(endpoint, "http")
    conn = http.client.HTTPConnection(host(parts), parts.port, timeout=3)
    try:
        conn.request("GET", "/json/version")
        response = conn.getresponse()
        body = response.read(16385)
        if response.status == 200 and len(body) <= 16384:
            doc = json.loads(body)
            if isinstance(doc, dict) and str(doc.get("Browser")).startswith("Chrome/"):
                ws = doc.get("webSocketDebuggerUrl")
                if isinstance(ws, str):
                    return address(ws, "ws")
    except OSError:
        raise ChromeError(UNREACHABLE) from None
    except (ValueError, http.client.HTTPException):
        pass
    finally:
        conn.close()
    raise ChromeError(WRONG_BROWSER)


class WebSocket:
    def __init__(self, parts):
        try:
            self.sock = socket.create_connection((host(parts), parts.port), timeout=10)
        except OSError:
            raise ChromeError(UNREACHABLE) from None
        self.reader = self.sock.makefile("rb")
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            self.sock.sendall((f"GET {parts.path} HTTP/1.1\r\nHost: {parts.netloc}\r\n"
                               "Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\n"
                               f"Sec-WebSocket-Key: {key}\r\n\r\n").encode("ascii"))
            status = self.reader.readline(1025)
            if status.split()[1:2] != [b"101"]:
                raise ChromeError(WRONG_BROWSER)
            headers = {}
            size = 0
            while size < 8192:
                line = self.reader.readline(8193 - size)
                if line == b"\r\n":
                    break
                k, sep, v = line.partition(b":")
                if not sep:
                    raise ChromeError(WRONG_BROWSER)
                headers[k.strip().lower()] = v.strip()
                size += len(line)
            else:
                raise ChromeError(WRONG_BROWSER)
            expected = hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
            if (headers.get(b"sec-websocket-accept") != base64.b64encode(expected)
                    or headers.get(b"upgrade", b"").lower() != b"websocket"
                    or b"upgrade" not in headers.get(b"connection", b"").lower().split(b", ")):
                raise ChromeError(WRONG_BROWSER)
        except BaseException:
            self.close()
            raise

    def close(self):
        self.reader.close()
        self.sock.close()

    def send(self, data, opcode=1):
        if len(data) > MAX_MESSAGE:
            raise ChromeError("Chrome message exceeded the size limit.")
        n = len(data)
        length = bytes([128 + n]) if n < 126 else (
            b"\xfe" + struct.pack(">H", n) if n <= 65535 else b"\xff" + struct.pack(">Q", n))
        mask = os.urandom(4)
        self.sock.sendall(bytes([128 + opcode]) + length + mask +
                          bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def read(self, n, deadline):
        data = bytearray()
        while len(data) < n:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ChromeError("Chrome timed out sending a debugging message.")
            self.sock.settimeout(left)
            chunk = self.reader.read1(min(65536, n - len(data)))
            if not chunk:
                raise ChromeError("Chrome closed the debugging connection.")
            data.extend(chunk)
        return bytes(data)

    def receive(self, deadline):
        message = bytearray()
        started = False
        for _ in range(2048):
            flags, n = self.read(2, deadline)
            op = flags & 15
            if flags & 112 or n & 128 or (op >= 8 and (not flags & 128 or n > 125)):
                break
            if n >= 126:
                n = int.from_bytes(self.read(2 if n == 126 else 8, deadline), "big")
            if n + len(message) > MAX_MESSAGE:
                break
            data = self.read(n, deadline)
            if op == 9:
                self.send(data, 10)
            elif op == 10:
                continue
            elif op == (0 if started else 1):
                started = True
                message.extend(data)
                if flags & 128:
                    return message.decode("utf-8")
            else:
                break
        raise ChromeError("Chrome returned an invalid or oversized WebSocket message.")


@contextmanager
def finish_creation():
    # Defer Ctrl-C until we have the target ID and registered its cleanup.
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending = []
    old = signal.signal(signal.SIGINT, lambda *args: pending.append(True))
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, old)
        if pending:
            signal.raise_signal(signal.SIGINT)


class Tab:
    def __init__(self, endpoint):
        self.endpoint = endpoint
        self.stack = ExitStack()
        self.sequence = 0
        self.session = None

    def __enter__(self):
        try:
            self.address = discover(self.endpoint)
            self.ws = WebSocket(self.address)
            self.stack.callback(self.ws.close)
            version = self.call("Browser.getVersion")
            if not str(version.get("product", "")).startswith("Chrome/"):
                raise ChromeError(WRONG_BROWSER)
            with finish_creation():
                target = self.call("Target.createTarget", {"url": "about:blank", "background": True})["targetId"]
                self.stack.callback(self.close_target, target)
            self.session = self.call("Target.attachToTarget", {"targetId": target, "flatten": True})["sessionId"]
            return self
        except BaseException:
            self.stack.close()
            raise

    def close_target(self, target):
        self.session = None
        try:
            result = self.call("Target.closeTarget", {"targetId": target})
        except ChromeError:
            # A timeout or interrupt may have left a partial frame on the original socket.
            self.ws = WebSocket(self.address)
            try:
                result = self.call("Target.closeTarget", {"targetId": target})
            finally:
                self.ws.close()
        if result.get("success") is not True:
            raise ChromeError("Chrome did not confirm that the command tab closed.")

    def __exit__(self, *args):
        self.stack.close()

    def call(self, method, params=None):
        self.sequence += 1
        request = {"id": self.sequence, "method": method, "params": params or {}}
        if self.session:
            request["sessionId"] = self.session
        try:
            self.ws.send(json.dumps(request).encode())
            deadline = time.monotonic() + 30
            for _ in range(128):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.ws.sock.settimeout(remaining)
                reply = json.loads(self.ws.receive(deadline))
                if reply.get("id") == self.sequence:
                    if isinstance(reply.get("result"), dict):
                        return reply["result"]
                    break
        except (OSError, ValueError, AttributeError):
            pass
        raise ChromeError("Chrome did not complete the debugging command.")

    def navigate(self, url):
        answer = self.call("Page.navigate", {"url": url})
        if "errorText" in answer:
            raise ChromeError("Chrome could not load the requested page.")
        for _ in range(100):
            if self.evaluate("document.readyState === 'complete' && location.href !== 'about:blank'"):
                return
            time.sleep(.05)
        raise ChromeError("Chrome timed out loading the requested page.")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", {"expression": expression, "returnByValue": True,
                                               "awaitPromise": True, "silent": True})
        if "exceptionDetails" in result or "value" not in result.get("result", {}):
            raise ChromeError("Chrome could not read the requested page.")
        return result["result"]["value"]
