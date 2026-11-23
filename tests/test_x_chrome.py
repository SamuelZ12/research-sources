import json
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from rsrc import chrome, cli
from rsrc.channels.x import auth, browser, connection

ADDRESS = "http://127.0.0.1:9333"


@pytest.mark.parametrize(("kind", "message"), [
    ("missing", "Google Chrome is not installed. Install Google Chrome, then run rsrc x connect."),
    ("unreachable", chrome.UNREACHABLE), ("wrong", chrome.WRONG_BROWSER),
    ("remote", "Chrome needs a loopback endpoint with an explicit port."),
])
def test_connect_failure(run_x, monkeypatch, kind, message):
    monkeypatch.setattr(connection, "executable", lambda: None)
    conn = Mock()
    conn.getresponse.return_value.status = 200
    conn.getresponse.return_value.read.return_value = b'{"Browser":"Firefox/1"}'
    if kind == "unreachable":
        conn.request.side_effect = ConnectionRefusedError
    monkeypatch.setattr(chrome.http.client, "HTTPConnection", lambda *a, **kw: conn)
    endpoint = "http://example.org:9333" if kind == "remote" else ADDRESS
    code, doc, _ = run_x(["connect", *([] if kind == "missing" else ["--cdp", endpoint])], None)
    assert code and not doc["ok"] and doc["error"]["message"] == message
    assert not auth.chrome_settings_path({}).exists()


@pytest.mark.parametrize("action", ["search", "timeline"])
@pytest.mark.parametrize("fault", ["auth_failed", "blocked", "rate_limited"])
def test_page_failure_stops_default_backend(run_x, monkeypatch, action, fault):
    connection.save({}, ADDRESS, False)
    tab = MagicMock()
    tab.evaluate.return_value = {"error": fault, "requests": 1, "dispatched": False}
    factory = MagicMock()
    factory.return_value.__enter__.return_value = tab
    monkeypatch.setattr(chrome, "Tab", factory)
    code, doc, _ = run_x([action, "sama"], None)
    assert code and doc["error"] == {"code": fault, "message": browser.ChromeSearcher.messages[fault]}
    assert not doc["ok"] and doc["backend"] == "x-chrome"
    tab.__exit__.assert_called_once()
    tab.navigate.assert_called_once_with("https://x.com/robots.txt")


@pytest.mark.parametrize("managed", [True, False])
def test_connect_settings_and_check_without_launch(run_x, monkeypatch, capsys, managed):
    monkeypatch.setattr(connection, "managed_browser", lambda *a, **kw: (ADDRESS, True))
    monkeypatch.setattr(chrome, "discover", lambda endpoint: None)
    monkeypatch.setattr(connection, "active", lambda _: ADDRESS)
    code, doc, _ = run_x(["connect", *([] if managed else ["--cdp", ADDRESS])], None)
    assert code == 0 and doc["ok"]
    assert json.loads(auth.chrome_settings_path({}).read_text()) == {
        "backend": "x-chrome", "managed_endpoint" if managed else "endpoint": ADDRESS}
    assert cli.main(["check", "x"], cli.Context(environ={})) == 0
    raw = json.loads(capsys.readouterr().out)["items"][0]["raw"]
    assert raw["x_chrome"] == {"configured": True, "reachable": True}
    monkeypatch.setattr(connection, "active", lambda _: None)
    monkeypatch.setattr(chrome, "discover", Mock(side_effect=chrome.ChromeError(chrome.UNREACHABLE)))
    assert cli.main(["check", "x"], cli.Context(environ={})) != 0
    assert not json.loads(capsys.readouterr().out)["items"][0]["raw"]["x_chrome"]["reachable"]


def test_managed_launch_and_reuse(monkeypatch):
    active = Mock(side_effect=[None, ADDRESS, ADDRESS])
    monkeypatch.setattr(connection, "active", active)
    monkeypatch.setattr(connection, "executable", lambda: "google-chrome")
    launch = Mock()
    monkeypatch.setattr(connection.subprocess, "Popen", launch)
    assert connection.managed_browser({}, signin=True) == (ADDRESS, True)
    assert connection.managed_browser({}) == (ADDRESS, False)
    args, = launch.call_args.args
    assert launch.call_count == 1 and args[-1] == "https://x.com"
    assert f"--user-data-dir={Path.home()}/.local/share/rsrc/chrome-profile" in args
    assert "--remote-debugging-port=0" in args and "--remote-debugging-address=127.0.0.1" in args


def test_check_reports_unconfigured_chrome(capsys):
    assert cli.main(["check", "x"], cli.Context(environ={})) == 0
    raw = json.loads(capsys.readouterr().out)["items"][0]["raw"]
    assert raw["x_chrome"] == {"configured": False, "reachable": None}


@pytest.mark.parametrize("argv", [["search", "news"], ["timeline", "sama"]])
def test_closed_external_browser_never_starts_chrome(run_x, monkeypatch, argv):
    connection.save({}, ADDRESS, False)
    monkeypatch.setattr(connection, "managed_browser", lambda *a: pytest.fail("launched external profile"))
    monkeypatch.setattr(chrome, "discover", Mock(side_effect=chrome.ChromeError(chrome.UNREACHABLE)))
    code, doc, _ = run_x(argv, None)
    assert code == 2 and not doc["ok"] and doc["backend"] == "x-chrome"
    assert doc["error"]["message"] == chrome.UNREACHABLE
    assert all(a["requests"] == 0 for a in doc["meta"]["attempts"])


def test_interrupted_search_closes_tab(run_x, monkeypatch):
    connection.save({}, ADDRESS, False)
    tab = MagicMock()
    tab.evaluate.side_effect = KeyboardInterrupt
    manager = MagicMock()
    manager.__enter__.return_value = tab
    monkeypatch.setattr(chrome, "Tab", lambda _: manager)
    with pytest.raises(KeyboardInterrupt):
        run_x(["search", "news"], None)
    tab.__exit__.assert_called_once()
