"""Exercise the shipped page program against recorded public GraphQL responses offline."""
import json
import subprocess
from pathlib import Path

import pytest

from rsrc.channels.x import browser, search
from rsrc.http import Client

FIXTURES = Path(__file__).parent / "fixtures"
def session_result(monkeypatch, payload):
    scripts = []
    def execute(script):
        scripts.append(script)
        return ["RSRC_X " + json.dumps(payload)]
    monkeypatch.setattr(browser, "execute", execute)
    return scripts


def test_recorded_browser_page(monkeypatch):
    doc = json.loads((FIXTURES / "x_graphql_latest.json").read_text())
    timeline = doc["data"]["search_by_raw_query"]["search_timeline"]["timeline"]
    tweets = [entry["content"]["itemContent"]["tweet_results"]["result"]
              for instruction in timeline["instructions"] for entry in instruction.get("entries", [])
              if "tweet_results" in entry["content"].get("itemContent", {})]
    scripts = session_result(monkeypatch, {"tweets": tweets, "cursor": None})
    with browser.Searcher(Client(max_requests=2), {}) as session:
        rows, cursor = session.page("q", "Latest", None, 20)
    assert rows == [r | {"source": "x-browser"} for r in search.parse_page(doc)[0]]
    assert cursor is None and session.client.requests_made == 2
    assert len(scripts) == 2 and "task.finish" in scripts[-1]


@pytest.mark.parametrize(("code", "dispatched"), [
    ("auth_failed", False), ("auth_failed", True), ("rate_limited", True),
    ("blocked", True), ("upstream_error", True)])
def test_browser_errors_stop_the_search(monkeypatch, code, dispatched):
    scripts = session_result(monkeypatch, {"error": code, "dispatched": dispatched})
    with pytest.raises(browser.BrowserError) as exc:
        with browser.Searcher(Client(max_requests=2), {}) as session:
            session.page("q", "Latest", None, 20)
    assert exc.value.code == code
    assert len(scripts) == 1 and session.client.requests_made == 1 + dispatched


def test_one_request_cannot_open_browser():
    with browser.Searcher(Client(max_requests=1), {}) as session:
        with pytest.raises(browser.CookieError, match="--max-requests 2"):
            session.page("q", "Top", None, 20)
        assert session.client.requests_made == 0


def test_concurrent_browser_searches_stop_before_navigation():
    with browser.Searcher(Client(max_requests=2), {}):
        with pytest.raises(browser.CookieError, match="another X browser command"):
            with browser.Searcher(Client(max_requests=2), {}):
                pytest.fail("acquired a busy browser")


def test_missing_executable(monkeypatch):
    monkeypatch.setenv("PATH", "")
    with pytest.raises(browser.CookieError, match="ego-browser is missing"):
        browser.execute("unused")


def test_stopped_browser_hides_transcript(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "private detail"))
    with pytest.raises(browser.BrowserError, match="open -a") as exc:
        browser.execute("unused")
    assert "private detail" not in str(exc.value)


def test_remedy_preserves_query_and_flags(run_x, monkeypatch):
    import shlex
    from rsrc.channels import x
    monkeypatch.setattr(x.shutil, "which", lambda _: "ego-browser")
    query = """a 'quoted' query; $(whoami)"""
    argv = ["search", "--top", "--since", "2026-11-01", "--per-window", "10", "--", query]
    code, result, _ = run_x(argv, lambda *_: (503, b""))
    assert code == 1
    command = result["error"]["message"].split("run: ")[-1]
    assert shlex.split(command) == ["rsrc", "x", "search", "--backend", "x-browser", "--since",
                                    "2026-11-01", "--per-window", "10", "--top", "--", query]


def test_no_browser_repair_command(run_x, monkeypatch):
    from rsrc.channels import x
    monkeypatch.setattr(x.shutil, "which", lambda _: None)
    code, result, _ = run_x(["search", "q"], lambda *_: (503, b""))
    assert code == 1 and "ego-browser is unavailable" in result["error"]["message"]
    assert result["error"]["message"].endswith("open https://lite.ego.app")


def test_cli_browser_error_has_no_fallback(run_x, monkeypatch):
    session_result(monkeypatch, {"error": "rate_limited"})
    code, result, _ = run_x(["search", "q", "--backend", "x-browser"], None)
    assert code == 1 and not result["ok"] and result["error"]["code"] == "rate_limited"
    assert [a["backend"] for a in result["meta"]["attempts"]] == ["x-browser"]


@pytest.mark.parametrize(("cap", "success", "reason"), [
    (2, False, "max-requests stopped"), (6, False, "every SearchTimeline"),
    (3, True, None)])
def test_rejected_id(monkeypatch, tmp_path, cap, success, reason):
    ids = []
    def execute(script):
        if not script.startswith("const INPUT"):
            return []
        ids.append(json.loads(script.splitlines()[0][14:-1])["id"])
        data = {"tweets": [], "cursor": None} if success and len(ids) == 2 else {"error": "query_id_rejected"}
        return ["RSRC_X " + json.dumps(data)]
    monkeypatch.setattr(browser, "execute", execute)
    with browser.Searcher(Client(max_requests=cap), {"XDG_CACHE_HOME": str(tmp_path)}) as session:
        if reason:
            with pytest.raises(search.SearchError, match=reason):
                session.page("test", "Top", None, 1)
        else:
            assert session.page("test", "Top", None, 1) == ([], None)
            assert search._read_cache(session.cache_path) == ids[-1]
    assert ids == list(search.QUERY_IDS[:len(ids)])
    assert session.client.requests_made == 1 + len(ids)
