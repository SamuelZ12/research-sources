import io
import json
import time
from pathlib import Path
from unittest.mock import Mock

import pytest

from rsrc import chrome

FIXTURE = Path(__file__).parent / "fixtures/chrome_cookie.json"


def test_recorded_cookie_exchange(monkeypatch):
    messages = iter(json.loads(FIXTURE.read_text()))
    sent = []
    wire = Mock()
    wire.receive.side_effect = lambda _: json.dumps(next(messages))
    wire.send.side_effect = lambda body: sent.append(json.loads(body))
    monkeypatch.setattr(chrome, "discover", lambda _: None)
    monkeypatch.setattr(chrome, "WebSocket", lambda _: wire)
    with chrome.Tab("test") as tab:
        tab.navigate("http://127.0.0.1/robots.txt")
        assert tab.evaluate("fetch('/check').then(r => r.ok)") is True
    assert [m["error"]["message"] for m in messages] == ["No target with given id found"]
    assert [m["method"] for m in sent] == ["Browser.getVersion", "Target.createTarget", "Target.attachToTarget",
        "Page.navigate", "Runtime.evaluate", "Runtime.evaluate", "Target.closeTarget"]
    assert sent[2]["params"]["targetId"] == sent[-1]["params"]["targetId"] == "handle-0"
    wire.close.assert_called_once()


@pytest.mark.parametrize("endpoint", ["http://example.com:123", "http://127.0.0.2:9333",
    "https://127.0.0.1:9333", "http://localhost", "http://localhost:0", "http://u@localhost:1",
    "http://localhost:1/foo", "http://localhost:1/?next=1", "http://localhost:65536"])
def test_endpoint_rules(endpoint):
    with pytest.raises(chrome.ChromeError, match="loopback endpoint with an explicit port"):
        chrome.address(endpoint, "http")


@pytest.mark.parametrize("endpoint", ["http://localhost:1", "http://127.0.0.1:9333", "http://[::1]:9222/"])
def test_allowed_endpoint(endpoint):
    assert chrome.address(endpoint, "http").port


def test_discovery_never_connects_to_remote_websocket(monkeypatch):
    conn = Mock()
    response = conn.getresponse.return_value
    response.status = 200
    response.read.return_value = b'{"Browser":"Chrome/158","webSocketDebuggerUrl":"ws://evil.test:80/devtools/browser/x"}'
    monkeypatch.setattr(chrome.http.client, "HTTPConnection", lambda *a, **k: conn)
    with pytest.raises(chrome.ChromeError, match="loopback"):
        chrome.discover("http://localhost:9333")
    conn.request.assert_called_once_with("GET", "/json/version")


def ws(data):
    client = chrome.WebSocket.__new__(chrome.WebSocket)
    client.reader = io.BytesIO(data)
    client.sock = Mock()
    return client


def test_fragmented_with_ping():
    client = ws(b"\x01\x03one\x89\x01x\x80\x03two")
    assert client.receive(time.monotonic()+5) == "onetwo"
    assert client.sock.sendall.call_args.args[0][:2] == b"\x8a\x81"


@pytest.mark.parametrize("frame", [b"", b"\x81\x81", b"\xc1\x00", b"\x82\x00", b"\x09\x00",
    b"\x81\x7f" + (chrome.MAX_MESSAGE + 1).to_bytes(8, "big"), b"\x80\x01a"])
def test_invalid_frame(frame):
    with pytest.raises(chrome.ChromeError):
        ws(frame).receive(time.monotonic()+5)


@pytest.mark.parametrize("size", [2, 125, 126, 65535, 65536])
def test_send_masks_each_payload(size):
    client = ws(b"")
    client.send(b"a" * size)
    frame = client.sock.sendall.call_args.args[0]
    n = frame[1] & 127
    width = 2 if n == 126 else 8 if n == 127 else 0
    assert frame[0] == 129 and frame[1] & 128
    assert (int.from_bytes(frame[2:2+width], "big") if width else n) == size
    mask, body = frame[2+width:6+width], frame[6+width:]
    assert bytes(b ^ mask[i % 4] for i, b in enumerate(body)) == b"a" * size


@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_interruption_closes_own_target(monkeypatch, failure):
    tab = chrome.Tab("test")
    commands = []
    def call(method, params=None):
        commands.append((method, params))
        if method == "Browser.getVersion":
            return {"product": "Chrome/158"}
        if method == "Target.createTarget":
            return {"targetId": "owned"}
        if method == "Target.attachToTarget":
            raise failure
        return {"success": True}
    monkeypatch.setattr(tab, "call", call)
    monkeypatch.setattr(chrome, "discover", lambda _: None)
    monkeypatch.setattr(chrome, "WebSocket", Mock())
    with pytest.raises(failure):
        with tab:
            pytest.fail("attach failed")
    assert commands[-1] == ("Target.closeTarget", {"targetId": "owned"})


def test_wrong_endpoint(monkeypatch):
    conn = Mock()
    conn.getresponse.return_value.status = 200
    conn.getresponse.return_value.read.return_value = b'{"Browser":"NotChrome/158"}'
    monkeypatch.setattr(chrome.http.client, "HTTPConnection", lambda *a, **k: conn)
    with pytest.raises(chrome.ChromeError) as error:
        chrome.discover("http://localhost:9333")
    assert str(error.value) == chrome.WRONG_BROWSER


def test_refused_endpoint(monkeypatch):
    conn = Mock()
    conn.request.side_effect = ConnectionRefusedError
    monkeypatch.setattr(chrome.http.client, "HTTPConnection", lambda *a, **k: conn)
    with pytest.raises(chrome.ChromeError) as error:
        chrome.discover("http://localhost:9333")
    assert str(error.value) == chrome.UNREACHABLE


def test_ctrl_c_after_navigation(monkeypatch):
    messages = iter(json.loads(FIXTURE.read_text()))
    commands = []
    wire = Mock()
    wire.receive.side_effect = lambda _: json.dumps(next(messages))
    wire.send.side_effect = lambda body: commands.append(json.loads(body))
    monkeypatch.setattr(chrome, "discover", lambda _: None)
    monkeypatch.setattr(chrome, "WebSocket", lambda _: wire)
    with pytest.raises(KeyboardInterrupt):
        with chrome.Tab("test") as tab:
            tab.navigate("http://127.0.0.1/robots.txt")
            tab.evaluate("fetch('/check').then(r => r.ok)")
            raise KeyboardInterrupt
    assert commands[-1]["method"] == "Target.closeTarget"
    assert commands[-1]["params"] == {"targetId": "handle-0"}
    wire.close.assert_called_once()


def test_cleanup_after_original_socket_fails(monkeypatch):
    tab = chrome.Tab("test")
    tab.address = object()
    tab.ws = Mock()
    second = Mock()
    monkeypatch.setattr(chrome, "WebSocket", lambda address: second)
    calls = Mock(side_effect=[chrome.ChromeError("broken frame"), {"success": True}])
    monkeypatch.setattr(tab, "call", calls)
    tab.close_target("owned")
    assert [c.args for c in calls.call_args_list] == [("Target.closeTarget", {"targetId": "owned"})] * 2
    second.close.assert_called_once()
