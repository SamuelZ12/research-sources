import http.client
import json
import urllib.request
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock

import pytest

from rsrc.channels.x import browser
from rsrc.http import TIMEOUT_S, urllib_transport

FIXTURES = Path(__file__).parent / "fixtures"
HANDLE = "sama"


def recorded_page():
    return json.loads((FIXTURES / "x_user_tweets.json").read_text())


def expected_tweets():
    entries = recorded_page()["timeline"]["timeline"]["instructions"][0]["entries"]
    wrapper = entries[0]["content"]["itemContent"]["tweet_results"]["result"]
    original = wrapper["legacy"]["retweeted_status_result"]["result"]
    return [{"tweet": original, "reposted_by": HANDLE}, *[
        {"tweet": item["item"]["itemContent"]["tweet_results"]["result"]}
        for item in entries[1]["content"]["items"]
    ]]


@pytest.mark.parametrize("backend", ["x-browser", "x-graphql"])
def test_login_selection_and_recorded_reposts(run_x, monkeypatch, backend):
    expected = expected_tweets()
    profile = {"rest_id": "123", "core": {"screen_name": HANDLE}}
    replies = [profile, recorded_page()]
    def transport(method, url, headers, body):
        assert method == "GET" and "cookie" in headers
        return 200, json.dumps({"data": {"user": {"result": replies.pop(0)}}}).encode()
    def execute(script):
        if script.startswith("const INPUT"):
            data = {"account": profile, "tweets": expected, "requests": 3}
            return ["RSRC_X " + json.dumps(data)]
        assert "task.finish" in script
        return []
    monkeypatch.setattr(browser, "execute", execute)
    config = {"RSRC_X_SEARCH": "x-browser"} if backend == "x-browser" else {"AUTH_TOKEN": "test", "CT0": "test"}
    code, doc, _ = run_x(["timeline", HANDLE], transport, config)
    assert code == 0 and doc["backend"] == backend
    assert doc["meta"]["attempts"][0]["requests"] == (3 if backend == "x-browser" else 2)
    account, *posts = doc["items"]
    assert account["id"] == profile["rest_id"]
    assert account["author"] == HANDLE
    assert [p["id"] for p in posts] == [e["tweet"]["rest_id"] for e in expected]
    assert posts[0]["raw"]["reposted_by"] == HANDLE and posts[0]["author"] != HANDLE
    assert all(p["author"] == HANDLE for p in posts[1:])


def test_browser_rate_limit_stops_before_fallback(run_x, monkeypatch):
    monkeypatch.setattr(browser, "execute", lambda _: ['RSRC_X {"error":"rate_limited","requests":2}'])
    def unexpected(*args):
        pytest.fail("public fallback following rate limit")
    code, doc, _ = run_x(["timeline", HANDLE], unexpected, {"RSRC_X_SEARCH": "x-browser"})
    assert code == 1 and doc["error"]["code"] == "rate_limited"
    assert doc["meta"]["attempts"][0]["requests"] == 2


def test_missing_browser_uses_public_timeline(run_x, monkeypatch):
    def missing(_):
        raise browser.CookieError("ego-browser unavailable")
    monkeypatch.setattr(browser, "execute", missing)
    fixture = (FIXTURES / "x_fx_timeline.json").read_bytes()
    code, doc, _ = run_x(["timeline", "mitchellh", "--limit", "2"], lambda *_: (200, fixture),
                         {"RSRC_X_SEARCH": "x-browser"})
    assert code == 0 and doc["backend"] == "fxtwitter"
    assert [(a["backend"], a["requests"]) for a in doc["meta"]["attempts"]] == [("x-browser", 0), ("fxtwitter", 1)]


def test_too_small_browser_cap_never_navigates(run_x, monkeypatch):
    monkeypatch.setattr(browser, "execute", lambda _: pytest.fail("browser started"))
    code, doc, _ = run_x(["timeline", HANDLE, "--max-requests", "2", "--backend", "x-browser"], None)
    assert code != 0 and doc["meta"]["attempts"][0]["requests"] == 0


def test_direct_timeline_keeps_verified_posts_when_capped(run_x):
    page = recorded_page()
    entries = page["timeline"]["timeline"]["instructions"][0]["entries"]
    entries.append({"content": {"cursorType": "Bottom", "value": "next"}})
    replies = [{"rest_id": "123", "core": {"screen_name": HANDLE}}, page]
    def transport(*args):
        return 200, json.dumps({"data": {"user": {"result": replies.pop(0)}}}).encode()
    code, doc, _ = run_x(["timeline", HANDLE, "--max-requests", "2"], transport,
                         {"AUTH_TOKEN": "test", "CT0": "test"})
    assert code == 0 and len(doc["items"]) == 1 + len(expected_tweets())
    assert doc["meta"]["truncated"] == "request_cap"
    assert doc["meta"]["attempts"][0]["requests"] == 2


@pytest.mark.parametrize("status,error", [(200, None), (401, "auth_failed"), (403, "auth_failed"),
                                          (429, "rate_limited"), (302, "blocked")])
def test_direct_timeline_uses_one_connection_and_stops_on_refusal(run_x, monkeypatch, status, error):
    profile = {"rest_id": "123", "core": {"screen_name": HANDLE}}
    responses = []
    for code, result in [(200, profile), (status, recorded_page())]:
        response = BytesIO(json.dumps({"data": {"user": {"result": result}}}).encode())
        response.status = code
        response.headers = {"Location": "https://example.org/"} if code == 302 else {}
        responses.append(response)
    connect = Mock()
    connect.return_value.getresponse.side_effect = responses
    monkeypatch.setattr(http.client, "HTTPSConnection", connect)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    config = {"AUTH_TOKEN": "test", "CT0": "test", "RSRC_X_SEARCH": "x-graphql,fxtwitter"}
    code, doc, _ = run_x(["timeline", HANDLE], urllib_transport, config)
    connect.assert_called_once_with("x.com", timeout=TIMEOUT_S)
    connect.return_value.close.assert_called_once_with()
    assert connect.return_value.request.call_count == doc["meta"]["attempts"][0]["requests"] == 2
    assert len(doc["meta"]["attempts"]) == 1
    if error:
        assert code == 1 and not doc["ok"] and doc["error"]["code"] == error
    else:
        assert code == 0 and [p["id"] for p in doc["items"][1:]] == [
            e["tweet"]["rest_id"] for e in expected_tweets()]


@pytest.mark.parametrize("status,body,error", [
    (200, b" <html>challenge</html>", "blocked"),
    (503, b"<html>challenge</html>", "blocked"),
    (200, b'{"errors":[{"code":326}]}', "blocked"),
    (200, b'{"errors":[{"code":88}]}', "rate_limited"),
    (200, b'{"errors":[{"code":89}]}', "auth_failed"),
])
def test_timeline_warning_stops_before_fallback(run_x, status, body, error):
    profile = {"rest_id": "123", "core": {"screen_name": HANDLE}}
    responses = iter([(200, json.dumps({"data": {"user": {"result": profile}}}).encode()),
                      (status, body)])
    config = {"AUTH_TOKEN": "test", "CT0": "test", "RSRC_X_SEARCH": "x-graphql,fxtwitter"}
    code, doc, _ = run_x(["timeline", HANDLE], lambda *_: next(responses), config)
    assert code == 1 and doc["error"]["code"] == error
    assert [(a["backend"], a["requests"]) for a in doc["meta"]["attempts"]] == [("x-graphql", 2)]
