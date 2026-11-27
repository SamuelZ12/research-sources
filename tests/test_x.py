"""`rsrc x`: envelope mapping of search/read/replies, offline via fake transports."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from rsrc import cli
from rsrc.channels import x
from rsrc.envelope import ITEM_KEYS
from rsrc.channels.x.fx import status_record
from rsrc.channels.x.search import QUERY_IDS, parse_page

GQL = ["search", "--backend", "x-graphql"]  # the cookie-authenticated X web search

FIXTURES = Path(__file__).parent / "fixtures"  # recorded responses
AUTH_TOKEN, CT0 = "TOKENSENTINEL", "CSRFSENTINEL"
POST = "2117770299454177696"
ENGAGEMENT = ("likes", "reposts", "replies", "quotes", "views")


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeTransport:
    """Serves (status, body) responses in order, routed by host; records every request."""

    def __init__(self, x=(), fx=(), syndication=()):
        self.queues = {"x.com": list(x), "api.fxtwitter.com": list(fx), "cdn.syndication.twimg.com": list(syndication)}
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        host = url.split("/")[2]
        status, data = self.queues[host].pop(0)
        return status, json.dumps(data).encode() if isinstance(data, dict) else data


@pytest.fixture
def environ(tmp_path):
    return {"AUTH_TOKEN": AUTH_TOKEN, "CT0": CT0, "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "RSRC_X_ENV": str(tmp_path / "missing-env")}


@pytest.fixture
def run(monkeypatch, capsys, environ):
    monkeypatch.setattr(cli, "CHANNELS", [x])

    def run(argv, transport, env=None):
        ctx = cli.Context(environ=environ if env is None else env, transport=transport, sleep=lambda s: None)
        code = cli.main(["x", *argv], ctx)
        out, err = capsys.readouterr()
        lines = out.splitlines()
        assert len(lines) == 1, out
        return code, json.loads(lines[0]), out + err

    return run


def tweet(rest_id, **legacy):
    return {"__typename": "Tweet", "rest_id": rest_id,
            "core": {"user_results": {"result": {"core": {"screen_name": "alice"}}}},
            "legacy": {"full_text": "text", "created_at": "Wed Nov 04 22:42:53 +0000 2026", **legacy}}


def page(*results):
    entries = [{"content": {"entryType": "TimelineTimelineItem", "itemContent": {"tweet_results": {"result": r}}}}
               for r in results]
    return {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}}


# --- search: x-graphql backend ------------------------------------------------

def test_search_maps_records_into_items(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run([*GQL, '"Letta Code"'], fake)

    assert code == 0 and env["ok"] is True and env["error"] is None
    assert (env["channel"], env["action"], env["backend"]) == ("x", "search", "x-graphql")
    assert env["input"]["query"] == '"Letta Code"' and env["input"]["max_requests"] == 25
    assert env["input"]["backend"] == "x-graphql"
    expected = {r["id"]: r for r in parse_page(load("x_graphql_latest.json"))[0]}
    assert len(env["items"]) == len(expected) == 20
    for item in env["items"]:
        raw = expected[item["id"]]
        assert tuple(item) == ITEM_KEYS
        assert item["raw"] == raw
        assert (item["url"], item["author"], item["published_at"], item["text"]) == (
            raw["url"], raw["author"], raw["created_at"], raw["text"])
        assert item["title"] is None and item["source"] == raw["source"] == "x-graphql"
        assert item["engagement"] == {k: raw[k] for k in ENGAGEMENT if raw[k] is not None}
    assert env["meta"]["attempts"] == [
        {"backend": "x-graphql", "outcome": "ok", "requests": 1, "ms": env["meta"]["attempts"][0]["ms"]}]
    assert env["meta"]["truncated"] is None and env["meta"]["notes"] == []


def test_search_engagement_drops_nulls_and_is_none_when_all_null(run):
    counted = tweet("10", favorite_count=5, retweet_count=None)
    fake = FakeTransport(x=[(200, page(counted, tweet("11")))])
    _, env, _ = run([*GQL, "q"], fake)
    by_id = {i["id"]: i for i in env["items"]}
    assert by_id["10"]["engagement"] == {"likes": 5}
    assert by_id["11"]["engagement"] is None


def test_search_dates_are_echoed_as_iso_strings(run):
    fake = FakeTransport(x=[(200, page())])
    code, env, _ = run([*GQL, "q", "--since", "2026-11-01", "--until", "2026-11-03", "--window-days", "7"], fake)
    assert code == 66 and env["ok"] is True and env["items"] == []
    assert env["input"]["since"] == "2026-11-01" and env["input"]["until"] == "2026-11-03"
    assert env["meta"]["attempts"][0]["outcome"] == "no_results"


@pytest.mark.parametrize("query_data", [None, {"search_timeline": {"timeline": {}}},
    {"search_timeline": {"timeline": {"instructions": []}}},
    {"search_timeline": {"timeline": {"instructions": [{"type": "Unknown"}]}}},
])
def test_unrecognized_search_page_is_not_an_empty_result(run, query_data):
    bad = {"data": {"search_by_raw_query": query_data}}
    code, env, _ = run([*GQL, "q"], FakeTransport(x=[(200, bad)]))
    assert code == 1 and env["error"]["code"] == "upstream_error"
    assert env["meta"]["attempts"][0]["outcome"] == "upstream_error"


def test_missing_search_page_preserves_earlier_posts(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json")), (200, {})])
    code, env, _ = run([*GQL, "q", "--per-window", "40"], fake)
    assert code == 1 and env["error"]["code"] == "upstream_error"
    assert len(env["items"]) == 20


def test_search_out_of_window_drops_become_a_note(run):
    old = tweet("12", created_at="Mon Oct 05 10:00:00 +0000 2026")
    fake = FakeTransport(x=[(200, page(old, tweet("13", created_at="Mon Nov 02 10:00:00 +0000 2026")))])
    _, env, _ = run([*GQL, "q", "--since", "2026-11-01", "--until", "2026-11-03"], fake)
    assert [i["id"] for i in env["items"]] == ["13"]
    assert env["meta"]["notes"] == ["1 post(s) outside their date window dropped"]


def in_late_window(record):
    return "2026-10-29" <= record["created_at"][:10] < "2026-11-05"


def test_windowed_search_keeps_every_in_window_post(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run([*GQL, "q", "--since", "2026-10-08", "--until", "2026-11-05", "--window-days", "28"], fake)
    assert code == 0
    assert [i["raw"] for i in env["items"]] == parse_page(load("x_graphql_latest.json"))[0]
    assert all(i["raw"]["article"] is None for i in env["items"])
    assert env["meta"]["attempts"][0]["requests"] == len(fake.urls) == 1 and env["meta"]["notes"] == []


def test_recorded_posts_outside_their_window_are_dropped_and_counted(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json")), (200, page())])
    code, env, _ = run([*GQL, "q", "--since", "2026-10-29", "--until", "2026-11-05"], fake)
    assert code == 0
    assert [i["raw"] for i in env["items"]] == [r for r in parse_page(load("x_graphql_latest.json"))[0]
                                                if in_late_window(r)]
    assert len(env["items"]) == 8 and env["meta"]["attempts"][0]["requests"] == 2
    assert env["meta"]["notes"] == ["12 post(s) outside their date window dropped"]


def test_request_cap_keeps_in_window_posts_and_drop_count(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run([*GQL, "q", "--since", "2026-10-29", "--until", "2026-11-05", "--max-requests", "1"],
                       fake)
    assert code == 0 and env["meta"]["truncated"] == "request_cap"
    assert [i["raw"] for i in env["items"]] == [r for r in parse_page(load("x_graphql_latest.json"))[0]
                                                if in_late_window(r)]
    assert env["meta"]["attempts"][0]["requests"] == len(fake.urls) == 1
    assert env["meta"]["notes"] == ["12 post(s) outside their date window dropped"]


def test_rate_limit_mid_search_stops_and_keeps_earlier_items(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json")), (429, b"{}"), (200, load("x_graphql_top.json"))])
    code, env, _ = run([*GQL, "q", "--per-window", "40"], fake)

    assert code == 1 and env["ok"] is False
    assert env["error"]["code"] == "rate_limited"
    assert env["error"]["message"] == "HTTP 429: rate limited; stop and retry later"
    assert len(env["items"]) == 20
    assert len(fake.urls) == 2  # no request after the 429
    assert env["meta"]["attempts"][0]["outcome"] == "rate_limited"
    assert env["meta"]["attempts"][0]["requests"] == 2


def test_401_is_auth_failed(run):
    fake = FakeTransport(x=[(401, b"")])
    code, env, _ = run([*GQL, "q"], fake)
    assert code == 1 and env["ok"] is False
    assert env["error"]["code"] == "auth_failed"
    assert "refresh AUTH_TOKEN/CT0" in env["error"]["message"]


def test_missing_cookies_is_config_with_zero_requests(run, environ):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    env_without_cookies = {k: v for k, v in environ.items() if k not in ("AUTH_TOKEN", "CT0")}
    code, env, _ = run([*GQL, "q"], fake, env_without_cookies)
    assert code == 2 and env["error"]["code"] == "config"
    assert "missing AUTH_TOKEN, CT0" in env["error"]["message"]
    assert fake.urls == [] and env["meta"]["attempts"] == []


def test_cap_during_query_id_rotation_reports_rejection(run):
    fake = FakeTransport(x=[(404, b"")])
    code, env, _ = run([*GQL, "q", "--max-requests", "1"], fake)
    assert code == 1 and env["ok"] is False and env["items"] == []
    assert env["error"]["code"] == "upstream_error"
    assert f"HTTP 404 for query ID {QUERY_IDS[0]}" in env["error"]["message"]
    assert "--max-requests stopped rotation before all SearchTimeline query IDs were tried" in env["error"]["message"]
    assert env["meta"]["truncated"] is None and attempts(env) == [("x-graphql", "upstream_error", 1)]
    assert [u.partition("?")[0] for u in fake.urls] == [f"https://x.com/i/api/graphql/{QUERY_IDS[0]}/SearchTimeline"]


def test_request_cap_is_ok_and_truncated(run):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json")), (200, load("x_graphql_top.json"))])
    code, env, _ = run([*GQL, "q", "--per-window", "40", "--max-requests", "1"], fake)
    assert code == 0 and env["ok"] is True and env["error"] is None
    assert env["meta"]["truncated"] == "request_cap"
    assert len(env["items"]) == 20 and attempts(env) == [("x-graphql", "ok", 1)]
    assert [u.partition("?")[0] for u in fake.urls] == [f"https://x.com/i/api/graphql/{QUERY_IDS[0]}/SearchTimeline"]


@pytest.mark.parametrize(("action", "target", "fixture", "rows", "limit", "expected"), [
    ("search", "q", "x_fx_search_latest.json", 6, 42, "Partial X search: returned 6 posts (limit 42); request cap reached."),
    ("search", "q", "x_fx_search_latest.json", 6, None, "Partial X search: returned 6 posts; request cap reached."),
    ("timeline", "mitchellh", "x_fx_timeline.json", 4, 200, "Partial X timeline: returned 4 posts (limit 200); request cap reached."),
    ("search", "q", "x_fx_search_latest.json", 6, 6, None),
    ("timeline", "mitchellh", "x_fx_timeline.json", 4, 4, None),
])
def test_request_cap_warning_in_json_and_stderr(capfd, action, target, fixture, rows, limit, expected):
    recorded = load(fixture)
    recorded["results"] = recorded["results"][:rows]
    fake = FakeTransport(fx=[(200, recorded)])
    options = ["--limit", str(limit)] if limit else []
    ctx = cli.Context(transport=fake, environ={}, sleep=lambda _: None)
    code = cli.main(["x", action, target, "--backend", "fxtwitter", "--max-requests", "1", *options], ctx)
    stdout, stderr = capfd.readouterr()
    result = json.loads(stdout)
    assert code == 0 and result["ok"] and len(fake.urls) == 1
    assert len(result["items"]) == rows + (action == "timeline")
    if expected:
        assert result["warning"] == expected and stderr.strip() == expected
        assert stdout.index('"warning"') < stdout.index('"items"')
    else:
        assert "warning" not in result and not stderr


@pytest.mark.parametrize("cap", [1, 2])
def test_empty_search_warns_only_if_a_date_window_went_unchecked(capfd, cap):
    empty = {"code": 200, "results": []}
    fake = FakeTransport(fx=[(200, empty)] * cap)
    ctx = cli.Context(transport=fake, environ={}, sleep=lambda _: None)
    code = cli.main(["x", "search", "q", "--backend", "fxtwitter", "--max-requests", str(cap),
                     "--since", "2026-11-01", "--until", "2026-11-15", "--limit", "42"], ctx)
    stdout, stderr = capfd.readouterr()
    result = json.loads(stdout)
    assert code == 66 and not result["items"] and len(fake.urls) == cap
    if cap == 1:
        assert result["warning"] == stderr.strip() == "Partial X search: returned 0 posts (limit 42); request cap reached."
    else:
        assert "warning" not in result and not stderr


def test_bad_date_range_is_usage_with_zero_requests(run):
    fake = FakeTransport()
    code, env, _ = run(["search", "q", "--since", "2026-11-05", "--until", "2026-11-01"], fake)
    assert code == 2 and env["error"]["code"] == "usage" and fake.urls == []


def test_bad_flag_value_is_usage(run):
    code, env, _ = run(["search", "q", "--max-requests", "0"], FakeTransport())
    assert code == 2 and env["error"]["code"] == "usage"
    assert (env["channel"], env["action"]) == ("x", "search")


@pytest.mark.parametrize("responses", [[(200, "x_graphql_latest.json")], [(401, b"")]])
def test_cookie_values_never_reach_output_or_files(run, responses, tmp_path):
    fake = FakeTransport(x=[(s, load(b) if isinstance(b, str) else b) for s, b in responses])
    code, _, output = run([*GQL, "q"], fake)
    assert AUTH_TOKEN not in output and CT0 not in output
    # Cookies come from the environment here, so every file under tmp_path was written by the run
    # (including $XDG_CACHE_HOME/rsrc/x-query-ids.json on success).
    written = [p for p in tmp_path.rglob("*") if p.is_file()]
    if code == 0:
        assert tmp_path / "cache" / "rsrc" / "x-query-ids.json" in written
    for path in written:
        data = path.read_bytes()
        assert AUTH_TOKEN.encode() not in data and CT0.encode() not in data, path


def test_query_id_cache_is_written_only_to_cache_rsrc(run, tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))  # a stray write under the home directory would show up below
    code, _, _ = run([*GQL, "q"], FakeTransport(x=[(200, load("x_graphql_latest.json"))]))
    assert code == 0
    assert [p for p in tmp_path.rglob("*") if p.is_file()] == [tmp_path / "cache" / "rsrc" / "x-query-ids.json"]


def cached_query_id(path):
    path.parent.mkdir(parents=True)
    path.write_text('{"SearchTimeline": "CACHEDID"}\n')


def test_cached_query_id_is_read_from_cache_rsrc(run, tmp_path):
    cached_query_id(tmp_path / "cache" / "rsrc" / "x-query-ids.json")
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, _, _ = run([*GQL, "q"], fake)
    assert code == 0 and "/graphql/CACHEDID/SearchTimeline" in fake.urls[0]


def test_query_id_cache_at_the_retired_path_is_ignored(run, tmp_path, retired):
    cached_query_id(tmp_path / "cache" / retired.dir / "query-ids.json")
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, _, _ = run([*GQL, "q"], fake)
    assert code == 0 and "/graphql/CACHEDID/SearchTimeline" not in fake.urls[0]


# --- search: fxtwitter backend (default) -----------------------------------------

FX_EMPTY = {"code": 200, "results": [], "cursor": {"top": None, "bottom": "LAST"}}


def fx_params(url):
    assert url.startswith("https://api.fxtwitter.com/2/search?"), url
    return parse_qs(urlsplit(url).query)


def test_search_defaults_to_fxtwitter_and_needs_no_cookies(run):
    recorded = load("x_fx_search_latest.json")
    fake = FakeTransport(fx=[(200, recorded), (200, FX_EMPTY)])
    code, env, _ = run(["search", '"Letta Code"', "--since", "2026-10-22", "--until", "2026-11-05",
                        "--window-days", "14"], fake, env={})

    assert code == 0 and env["error"] is None
    assert env["backend"] == env["input"]["backend"] == "fxtwitter"
    assert [i["raw"] for i in env["items"]] == [status_record(s) for s in recorded["results"]]
    assert len(env["items"]) == 11 and all(i["source"] == "fxtwitter" for i in env["items"])
    assert fx_params(fake.urls[0]) == {"q": ['"Letta Code" since_time:1792627200 until_time:1793836800'],
                                       "feed": ["latest"], "count": ["20"]}
    # 11 posts < --per-window 20, so the cursor is followed once; the empty page ends the window.
    assert fx_params(fake.urls[1])["cursor"] == [recorded["cursor"]["bottom"]]
    assert env["meta"]["attempts"] == [
        {"backend": "fxtwitter", "outcome": "ok", "requests": 2, "ms": env["meta"]["attempts"][0]["ms"]}]


def test_fxtwitter_top_uses_the_top_feed_and_pages_of_up_to_100(run):
    fake = FakeTransport(fx=[(200, FX_EMPTY)])
    code, env, _ = run(["search", "q", "--top", "--per-window", "250"], fake, env={})
    assert code == 66 and len(fake.urls) == 1
    assert fx_params(fake.urls[0])["feed"] == ["top"] and fx_params(fake.urls[0])["count"] == ["100"]


def test_fxtwitter_rate_limit_stops_without_switching_to_x_graphql(run, environ):
    # Cookies are available (default environ), yet nothing may go to x.com after the 429.
    fake = FakeTransport(fx=[(200, load("x_fx_search_latest.json")), (429, b"")])
    code, env, _ = run(["search", "q", "--per-window", "40"], fake, env=environ | {"RSRC_X_SEARCH": "fxtwitter,x-graphql"})
    assert code == 1 and env["error"]["code"] == "rate_limited"
    assert len(env["items"]) == 11 and len(fake.urls) == 2
    assert all(u.startswith("https://api.fxtwitter.com/") for u in fake.urls)


@pytest.mark.parametrize("status, requests", [(500, 3), (404, 3), (400, 1), (401, 1), (403, 1)])
def test_fxtwitter_failure_is_upstream_error(run, status, requests):
    # 5xx and 404 get FxTwitter's usual retries: a search with no results is a 200 with `results: []`
    # (probed 2026-11-11), and FxTwitter intermittently 404s. Other 4xx would fail the same way again.
    fake = FakeTransport(fx=[(status, b"")] * requests)
    code, env, _ = run(["search", "q"], fake, env={})
    assert code == 1 and env["error"]["code"] == "upstream_error"
    assert env["error"]["message"].startswith(f"fxtwitter search: HTTP {status}. Public X search is unavailable.")
    assert len(fake.urls) == requests and env["meta"]["attempts"][0]["requests"] == requests


# --- read ----------------------------------------------------------------------

def test_read_keeps_good_post_and_records_unresolvable_one(run):
    missing = "2117770299454177697"
    fake = FakeTransport(fx=[(200, load("x_fx_status.json"))] + [(404, {})] * 3, syndication=[(404, b"")])
    code, env, _ = run(["read", f"https://x.com/bkdgiffug/status/{POST}", missing], fake)

    assert code == 0 and env["ok"] is True and env["error"] is None
    assert env["backend"] == "fxtwitter"
    assert [i["raw"] for i in env["items"]] == [status_record(load("x_fx_status.json")["status"])]
    [item] = env["items"]
    assert item["source"] == "fxtwitter"
    assert item["title"] is None and item["text"] == load("x_fx_status.json")["status"]["text"]
    assert item["raw"]["article"] is None
    assert env["meta"]["failed"] == [{"input": missing, "code": "not_found",
                                      "message": "fxtwitter: HTTP 404; syndication: HTTP 404"}]
    assert [(a["backend"], a["outcome"], a["requests"]) for a in env["meta"]["attempts"]] == [
        ("fxtwitter", "ok", 1), ("fxtwitter", "not_found", 2)]


@pytest.mark.parametrize("arg", ["1234567890", "https://x.com/example/article/1234567890"])
def test_read_rejects_a_post_whose_id_differs_from_the_request(run, arg):
    fake = FakeTransport(fx=[(200, load("x_fx_status.json"))])
    code, env, _ = run(["read", arg], fake)
    assert code == 1 and env["ok"] is False
    assert env["error"]["code"] == "url_mismatch"
    assert env["items"] == []
    assert env["meta"]["rejected"] == [{"url": f"https://x.com/bkdgiffug/status/{POST}", "reason": "url_mismatch"}]


def test_read_rejects_a_login_wall(run):
    status = load("x_fx_status.json")
    status["status"]["text"] = "Log in to continue"
    code, env, _ = run(["read", POST], FakeTransport(fx=[(200, status)]))
    assert code == 1 and env["error"]["code"] == "blocked"
    assert env["meta"]["rejected"][0]["reason"] == "blocked_page"


def test_read_falls_back_to_syndication(run):
    fake = FakeTransport(fx=[(500, b"")] * 3, syndication=[(200, load("x_syndication.json"))])
    code, env, _ = run(["read", POST], fake)
    assert code == 0 and env["items"][0]["source"] == "syndication"
    assert env["items"][0]["raw"]["article"] is None
    assert sum("api.fxtwitter.com" in u for u in fake.urls) == 3
    assert env["meta"]["attempts"][0]["backend"] == "syndication"


def test_read_says_when_syndication_cut_a_long_post_short(run):
    fake = FakeTransport(fx=[(500, b"")] * 3, syndication=[(200, load("x_syndication_long.json"))])
    code, env, _ = run(["read", "2126088961902981135"], fake)
    assert code == 0 and env["items"][0]["raw"]["text_truncated"] is True
    assert env["meta"]["notes"] == [
        "2126088961902981135: X's embed endpoint sent only the start of the text (a long post); "
        "FxTwitter was unavailable. Rerun later for the full text."]


def test_read_all_failed_uses_first_failure(run):
    fake = FakeTransport(fx=[(500, b"")] * 3, syndication=[(503, b"")])
    code, env, _ = run(["read", POST], fake)
    assert code == 1 and env["ok"] is False
    assert env["error"] == {"code": "upstream_error",
                            "message": f"{POST}: fxtwitter: HTTP 500; syndication: HTTP 503"}


def test_read_unparseable_input_is_usage_without_requests(run):
    fake = FakeTransport()
    code, env, _ = run(["read", "hello"], fake)
    assert code == 2 and env["error"]["code"] == "usage" and fake.urls == []
    assert env["meta"]["failed"] == [{"input": "hello", "code": "usage", "message": "not a post URL or ID"}]


def test_read_article_id_link_asks_for_the_post_without_requests(run):
    fake = FakeTransport()
    code, env, _ = run(["read", "https://x.com/i/article/2097761142995308544"], fake)
    assert code == 2 and env["error"]["code"] == "usage" and fake.urls == []
    assert "read the post that links to the article" in env["error"]["message"]


def test_read_returns_poll_results_and_keeps_the_question_verbatim(run):
    data = load("x_fx_poll.json")
    code, out, _ = run(["read", data["status"]["id"]], FakeTransport(fx=[(200, data)]))
    item, = out["items"]
    assert code == 0 and item["raw"]["poll"] == data["status"]["poll"]
    assert item["raw"]["text"] == data["status"]["text"]
    assert all(s in item["text"] for s in ("Final results", "17,502,391 votes", "Yes: 57.5%", "No: 42.5%"))


def test_poll_when_only_syndication_answers(run):
    poll = load("x_syndication_poll.json")
    code, out, _ = run(["read", poll["id_str"]], FakeTransport(fx=[(500, b"")] * 3, syndication=[(200, poll)]))
    item, = out["items"]
    assert code == 0 and item["source"] == "syndication"
    assert all(s in item["text"] for s in ("Final results", "Yes: 10,063,044 votes", "No: 7,439,347 votes"))
    assert "%" not in item["text"]


def test_unreported_poll_numbers_are_not_invented(run):
    data = load("x_fx_poll.json")
    data["status"]["poll"] = {"choices": [{"label": "Yes"}, {"label": "No"}]}
    code, out, _ = run(["read", data["status"]["id"]], FakeTransport(fx=[(200, data)]))
    item, = out["items"]
    assert code == 0 and "Yes: result unavailable" in item["text"]
    assert "%" not in item["text"] and "Final results" not in item["text"]


def test_fx_poll_does_not_publish_nonfinite_numbers_or_require_question_text(run):
    data = load("x_fx_poll.json")
    status = data["status"]
    status["text"] = []
    status["poll"].update(total_votes=1, choices=[{"label": "Yes", "percentage": float("inf"), "count": 1}])
    status["quote"] = dict(status)
    code, out, _ = run(["read", status["id"]], FakeTransport(fx=[(200, data)]))
    item, = out["items"]
    assert code == 0 and "1 votes" not in item["text"] and "Yes: 1 vote" in item["text"]
    assert item["raw"]["quoted"]["poll"] == item["raw"]["poll"]
    json.dumps(out, allow_nan=False)


@pytest.mark.parametrize("fallback", [False, True])
def test_poll_on_a_quoted_post_is_not_attributed_to_the_author(run, fallback):
    if fallback:
        poll = load("x_syndication_poll.json")
        main = load("x_syndication.json")
        main["quoted_tweet"] = poll
        fake = FakeTransport(fx=[(500, b"")] * 3, syndication=[(200, main)])
    else:
        main = load("x_fx_status.json")
        main["status"]["quote"] = load("x_fx_poll.json")["status"]
        fake = FakeTransport(fx=[(200, main)])
    code, out, _ = run(["read", POST], fake)
    item, = out["items"]
    assert code == 0 and item["raw"]["poll"] is None and "Final results" not in item["text"]
    assert item["raw"]["quoted"]["poll"]["choices"][0]["count"] == 10063044


def test_read_article_url_returns_the_article_from_the_publishing_post(run):
    fake = FakeTransport(fx=[(200, load("x_fx_article.json"))])
    code, env, _ = run(["read", "https://x.com/pmarca/article/2097762851788398971"], fake)
    assert code == 0 and env["meta"]["rejected"] == []
    assert fake.urls == ["https://api.fxtwitter.com/2/status/2097762851788398971"]
    [item] = env["items"]
    assert item["id"] == "2097762851788398971"
    assert item["url"] == "https://x.com/pmarca/status/2097762851788398971"
    assert item["title"] == "Software is about to eat the world much faster"
    assert item["text"] == ("Fifteen years ago, I wrote that software is eating the world\n\n"
                            "An infinite market demands infinite ambition.")
    assert item["raw"]["text"] == "https://x.com/i/article/2097761142995308544"


def test_read_renders_article_blocks_marks_unresolved_media_and_keeps_links_in_order(run):
    fake = FakeTransport(fx=[(200, load("x_fx_article_blocks.json"))])
    code, env, _ = run(["read", "https://x.com/example/status/1234567890"], fake)
    assert code == 0 and env["meta"]["rejected"] == []
    [item] = env["items"]
    assert item["title"] == "Article formatting"
    assert item["text"] == (
        "# Heading\n\n## Section\n\n### Subsection\n\n> Please log in\n\n- Bullet\n\n1. Step\n\n"
        "```\n  echo hello\n```\n\n[post: https://x.com/i/status/1234567892]\n\n[image]\n\n"
        "```\nuv tool install rsrc\n```\n\nExtra text"
    )
    assert item["raw"]["article"]["links"] == ["https://example.com/first", "https://example.com/second"]


def test_read_malformed_article_keeps_the_post_text(run):
    status = load("x_fx_article.json")
    status["status"]["article"]["content"]["blocks"] = "not a block list"
    code, env, _ = run(["read", "2097762851788398971"], FakeTransport(fx=[(200, status)]))
    assert code == 0
    [item] = env["items"]
    assert item["title"] is None and item["text"] == "https://x.com/i/article/2097761142995308544"
    assert item["raw"]["article"] is None


def test_read_rate_limit_stops_before_later_inputs(run):
    fake = FakeTransport(fx=[(200, load("x_fx_status.json")), (429, b"")])
    code, env, _ = run(["read", POST, "2117770299454177697", "2117770299454177698"], fake)
    assert code == 1 and env["error"]["code"] == "rate_limited"
    assert "rate limited" in env["error"]["message"]
    assert [i["id"] for i in env["items"]] == [POST]
    assert len(fake.urls) == 2


# --- replies -------------------------------------------------------------------

def test_replies_returns_items(run):
    empty = {"code": 200, "replies": [], "cursor": {}}
    conversation = load("x_fx_conversation.json")
    fake = FakeTransport(fx=[(200, conversation), (200, empty)])
    code, env, _ = run(["replies", POST, "--rank", "recency", "--limit", "50"], fake)
    assert code == 0 and env["backend"] == "fxtwitter"
    assert env["input"] == {"post": POST, "rank": "recency", "limit": 50}
    assert [i["raw"] for i in env["items"]] == [status_record(s) for s in conversation["replies"]]
    assert len(env["items"]) == 14
    assert all(i["raw"]["reply_to_id"] == POST and i["source"] == "fxtwitter" for i in env["items"])


def test_replies_bad_id_is_usage_without_requests(run):
    fake = FakeTransport()
    code, env, _ = run(["replies", "hello"], fake)
    assert code == 2 and env["error"]["code"] == "usage" and fake.urls == []
    assert env["error"]["message"] == "not a post URL or ID: hello"


def test_replies_upstream_failure(run):
    fake = FakeTransport(fx=[(500, b"")] * 3)
    code, env, _ = run(["replies", POST], fake)
    assert code == 1 and env["error"]["code"] == "upstream_error"
    assert env["error"]["message"] == f"fxtwitter conversation {POST}: HTTP 500"


def test_replies_to_a_missing_post_is_not_found(run):
    fake = FakeTransport(fx=[(404, {})] * 3)
    code, env, _ = run(["replies", POST], fake)
    assert code == 1 and env["error"]["code"] == "not_found"
    assert env["items"] == [] and len(fake.urls) == 3
    assert env["error"]["message"] == f"fxtwitter conversation {POST}: HTTP 404"
    assert env["meta"]["attempts"][0]["outcome"] == "not_found"


# --- error codes follow the kind of failure, not its wording -------------------

def offline(method, url, headers, body):
    raise OSError("connection reset")


@pytest.mark.parametrize("argv, message", [
    (["search", "q"], "fxtwitter search: api.fxtwitter.com: network error: connection reset"),
    ([*GQL, "q"], "x.com: network error: connection reset"),
    (["replies", POST], f"fxtwitter conversation {POST}: api.fxtwitter.com: network error: connection reset"),
    (["read", POST], f"{POST}: fxtwitter: api.fxtwitter.com: network error: connection reset; "
                     "syndication: cdn.syndication.twimg.com: network error: connection reset"),
])
def test_unreachable_host_is_network_error(run, argv, message):
    code, env, _ = run(argv, offline, env=None if "--backend" in argv else {})
    assert code == 1 and env["error"]["code"] == "network_error"
    if argv[0] == "search" and "--backend" not in argv:
        assert env["error"]["message"].startswith(message + ". Public X search is unavailable.")
    else:
        assert env["error"]["message"] == message
    assert env["meta"]["attempts"][0]["outcome"] == "network_error"


@pytest.mark.parametrize("response", [
    (500, b"upstream network error"),
    (200, {"errors": [{"message": "Temporary network error, try again"}]}),
])
def test_answer_from_x_that_mentions_a_network_error_is_upstream_error(run, response):
    # X answered, so the network worked: the code is not read out of X's wording.
    fake = FakeTransport(x=[response])
    code, env, _ = run([*GQL, "q"], fake)
    assert code == 1 and env["error"]["code"] == "upstream_error"
    assert "network error" in env["error"]["message"] and len(fake.urls) == 1


# --- search: configured backend order -------------------------

FX_FIRST = {"RSRC_X_SEARCH": "fxtwitter,x-graphql"}
SESSION_FIRST = {"RSRC_X_SEARCH": "x-graphql,fxtwitter"}


def attempts(env):
    return [(a["backend"], a["outcome"], a["requests"]) for a in env["meta"]["attempts"]]


def test_fallback_after_fxtwitter_upstream_error_returns_x_graphql_results(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3, x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run(["search", "q"], fake, env={**environ, **FX_FIRST})

    assert code == 0 and env["ok"] is True and env["error"] is None
    assert env["backend"] == "x-graphql" and env["input"]["backend"] == "fxtwitter"
    assert attempts(env) == [("fxtwitter", "upstream_error", 3), ("x-graphql", "ok", 1)]
    assert env["meta"]["notes"] == ["fxtwitter was tried first and failed: fxtwitter search: HTTP 404"]
    assert [urlsplit(u).hostname for u in fake.urls] == ["api.fxtwitter.com"] * 3 + ["x.com"]
    expected = [r["id"] for r in parse_page(load("x_graphql_latest.json"))[0]]
    assert [i["id"] for i in env["items"]] == expected
    assert all(i["source"] == "x-graphql" for i in env["items"])


def test_no_fallback_after_fxtwitter_no_results(run, environ):
    fake = FakeTransport(fx=[(200, FX_EMPTY)], x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run(["search", "q"], fake, env={**environ, **FX_FIRST})
    assert code == 66 and env["ok"] is True and env["backend"] == "fxtwitter" and env["items"] == []
    assert attempts(env) == [("fxtwitter", "no_results", 1)]
    assert all(u.startswith("https://api.fxtwitter.com/") for u in fake.urls)


def test_fallback_never_follows_a_fxtwitter_rate_limit(run, environ):
    fake = FakeTransport(fx=[(429, b"")], x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run(["search", "q"], fake, env={**environ, **FX_FIRST})
    assert code == 1 and env["error"]["code"] == "rate_limited" and env["backend"] == "fxtwitter"
    assert all(u.startswith("https://api.fxtwitter.com/") for u in fake.urls)
    assert attempts(env) == [("fxtwitter", "rate_limited", 1)]


def test_fxtwitter_results_are_kept_without_fallback(run, environ):
    fake = FakeTransport(fx=[(200, load("x_fx_search_latest.json")), (200, FX_EMPTY)])
    code, env, _ = run(["search", "q"], fake, env={**environ, **FX_FIRST})
    assert code == 0 and env["backend"] == "fxtwitter" and len(env["items"]) == 11
    assert [a["backend"] for a in env["meta"]["attempts"]] == ["fxtwitter"]


def test_no_fallback_when_setting_is_off(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3)
    code, env, _ = run(["search", "q"], fake, env={})
    assert code == 1 and env["error"]["code"] == "upstream_error" and env["backend"] == "fxtwitter"
    assert attempts(env) == [("fxtwitter", "upstream_error", 3)]


@pytest.mark.parametrize("settings", [FX_FIRST, {"RSRC_X_SEARCH": "invalid", "RSRC_X_FALLBACK": "x-graphql"}])
def test_explicit_fxtwitter_backend_ignores_the_setting(run, environ, settings):
    fake = FakeTransport(fx=[(404, b"")] * 3)
    code, env, _ = run(["search", "q", "--backend", "fxtwitter"], fake, env=environ | settings)
    assert code == 1 and env["error"]["code"] == "upstream_error"
    assert attempts(env) == [("fxtwitter", "upstream_error", 3)]


def test_fallback_reports_both_failures_when_cap_stops_query_id_rotation(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3, x=[(404, b"")])
    code, env, _ = run(["search", "q", "--max-requests", "4"], fake, env=environ | FX_FIRST)
    assert code == 1 and env["backend"] == "fxtwitter" and env["items"] == []
    assert env["error"] == {"code": "upstream_error", "message": "fxtwitter search: HTTP 404"}
    assert env["meta"]["notes"] == [f"x-graphql was tried next and failed: HTTP 404 for query ID {QUERY_IDS[0]}. "
                                    "--max-requests stopped rotation before all SearchTimeline query IDs were tried."]
    assert env["meta"]["truncated"] is None
    assert attempts(env) == [("fxtwitter", "upstream_error", 3), ("x-graphql", "upstream_error", 1)]
    assert [u.partition("?")[0] for u in fake.urls] == [
        "https://api.fxtwitter.com/2/search"] * 3 + [f"https://x.com/i/api/graphql/{QUERY_IDS[0]}/SearchTimeline"]


def test_fallback_shares_the_request_cap(run, environ):
    page2 = load("x_graphql_latest.json")
    fake = FakeTransport(fx=[(404, b"")] * 3, x=[(200, page2), (200, page2)])
    code, env, _ = run(["search", "q", "--per-window", "40", "--max-requests", "4"], fake,
                       env={**environ, **FX_FIRST})
    assert code == 0 and env["backend"] == "x-graphql" and env["meta"]["truncated"] == "request_cap"
    assert len(fake.urls) == 4
    assert attempts(env) == [("fxtwitter", "upstream_error", 3), ("x-graphql", "ok", 1)]


def test_no_fallback_when_fxtwitter_used_the_whole_cap(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3)
    code, env, _ = run(["search", "q", "--max-requests", "3"], fake, env={**environ, **FX_FIRST})
    assert code == 1 and env["error"]["code"] == "upstream_error" and len(fake.urls) == 3
    assert attempts(env) == [("fxtwitter", "upstream_error", 3)]


@pytest.mark.parametrize("value", ["reddit", "x-graphql,x-graphql", "fxtwitter,", ",x-graphql"])
def test_bad_search_order_is_config_with_zero_requests(run, environ, value):
    fake = FakeTransport()
    code, env, _ = run(["search", "q"], fake, env={**environ, "RSRC_X_SEARCH": value})
    assert code == 2 and env["error"]["code"] == "config"
    assert fake.urls == [] and env["meta"]["attempts"] == []


def test_blank_search_order_uses_configured_pair(run, environ):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run(["search", "q"], fake, env={**environ, "RSRC_X_SEARCH": " ", "RSRC_X_FALLBACK": ""})
    assert code == 0 and env["backend"] == "x-graphql" and [a["backend"] for a in env["meta"]["attempts"]] == ["x-graphql"]


def test_fallback_setting_is_read_from_the_cookie_file(run, environ, tmp_path):
    path = tmp_path / "x.env"
    path.write_text(f"AUTH_TOKEN={AUTH_TOKEN}\nCT0={CT0}\nRSRC_X_SEARCH=fxtwitter,x-graphql\n")
    path.chmod(0o600)
    fake = FakeTransport(fx=[(404, b"")] * 3, x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run(["search", "q"], fake, env={"RSRC_X_ENV": str(path), "XDG_CACHE_HOME": environ["XDG_CACHE_HOME"]})
    assert code == 0 and env["backend"] == "x-graphql"
    assert attempts(env) == [("fxtwitter", "upstream_error", 3), ("x-graphql", "ok", 1)]


def test_fallback_without_cookies_is_config_after_the_fxtwitter_attempt(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3)
    code, env, _ = run(["search", "q"], fake, env={"RSRC_X_ENV": environ["RSRC_X_ENV"], **FX_FIRST})
    assert code == 2 and env["error"]["code"] == "config" and len(fake.urls) == 3
    assert env["backend"] == "x-graphql" and attempts(env) == [("fxtwitter", "upstream_error", 3)]
    assert env["meta"]["notes"] == ["fxtwitter was tried first and failed: fxtwitter search: HTTP 404"]


def test_fallback_401_is_auth_failed_with_both_attempts(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3, x=[(401, b"")])
    code, env, _ = run(["search", "q"], fake, env={**environ, **FX_FIRST})
    assert code == 1 and env["error"]["code"] == "auth_failed" and env["backend"] == "x-graphql"
    assert attempts(env) == [("fxtwitter", "upstream_error", 3), ("x-graphql", "auth_failed", 1)]


def test_fxtwitter_404_cut_short_by_the_cap_is_upstream_error(run):
    # The 404 retry hitting --max-requests must not turn the outage into an empty ok result.
    fake = FakeTransport(fx=[(404, b"")] * 3)
    code, env, _ = run(["search", "q", "--max-requests", "1"], fake, env={})
    assert code == 1 and env["ok"] is False and env["error"]["code"] == "upstream_error"
    assert attempts(env) == [("fxtwitter", "upstream_error", 1)] and len(fake.urls) == 1

@pytest.mark.parametrize(("response", "exit_code", "outcome"), [
    ((200, page(tweet("11"))), 0, "ok"), ((200, page()), 66, "no_results"),
    ((401, b""), 1, "auth_failed"), ((429, b""), 1, "rate_limited"),
])
def test_session_first_stops_on_answer_or_account_error(run, environ, response, exit_code, outcome):
    fake = FakeTransport(x=[response])
    code, env, _ = run(["search", "q"], fake, env=environ | SESSION_FIRST)
    assert code == exit_code and env["backend"] == env["input"]["backend"] == "x-graphql"
    assert (env["error"]["code"] if env["error"] else None) == (outcome if code == 1 else None)
    assert [item["id"] for item in env["items"]] == (["11"] if outcome == "ok" else [])
    assert attempts(env) == [("x-graphql", outcome, 1)] and env["meta"]["notes"] == []
    assert [urlsplit(u).hostname for u in fake.urls] == ["x.com"]


def test_session_first_missing_cookies_stops_before_requests(run, environ):
    fake = FakeTransport()
    code, env, _ = run(["search", "q"], fake, env={"RSRC_X_ENV": environ["RSRC_X_ENV"], **SESSION_FIRST})
    assert code == 2 and env["error"]["code"] == "config" and env["backend"] == "x-graphql"
    assert attempts(env) == env["meta"]["notes"] == fake.urls == []


@pytest.mark.parametrize(("fx_status", "exit_code", "outcome"), [(200, 0, "ok"), (404, 1, "upstream_error")])
def test_session_error_passes_remaining_request_to_fxtwitter(run, environ, fx_status, exit_code, outcome):
    fake = FakeTransport(x=[(500, b"session outage")], fx=[(fx_status, load("x_fx_search_latest.json"))])
    code, env, _ = run(["search", "q", "--max-requests", "2"], fake, env=environ | SESSION_FIRST)
    assert code == exit_code and env["input"]["backend"] == "x-graphql"
    if fx_status == 200:
        assert env["backend"] == "fxtwitter" and env["error"] is None
        assert {item["source"] for item in env["items"]} == {"fxtwitter"}
        assert env["meta"]["truncated"] == "request_cap"
        assert env["meta"]["notes"] == ["x-graphql was tried first and failed: HTTP 500: session outage"]
    else:  # nothing answered: the result is the first backend's, the fallback's failure is a note
        assert env["backend"] == "x-graphql" and env["items"] == []
        assert env["error"] == {"code": "upstream_error", "message": "HTTP 500: session outage"}
        assert env["meta"]["notes"] == ["fxtwitter was tried next and failed: fxtwitter search: HTTP 404"]
    assert attempts(env) == [("x-graphql", "upstream_error", 1), ("fxtwitter", outcome, 1)]
    assert [urlsplit(u).hostname for u in fake.urls] == ["x.com", "api.fxtwitter.com"]


def test_session_first_keeps_its_posts_and_error_when_fxtwitter_fails_too(run, environ):
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json")), (500, b"page two")], fx=[(404, b"")] * 3)
    code, env, _ = run(["search", "q", "--per-window", "100"], fake, env=environ | SESSION_FIRST)
    assert code == 1 and env["backend"] == "x-graphql" and env["error"]["code"] == "upstream_error"
    assert "page two" in env["error"]["message"]
    assert len(env["items"]) == len(parse_page(load("x_graphql_latest.json"))[0]) > 0
    assert attempts(env) == [("x-graphql", "upstream_error", 2), ("fxtwitter", "upstream_error", 3)]
    assert env["meta"]["notes"] == ["fxtwitter was tried next and failed: fxtwitter search: HTTP 404"]


def test_fxtwitter_first_keeps_its_error_when_the_session_fails_too(run, environ):
    fake = FakeTransport(fx=[(404, b"")] * 3, x=[(500, b"session outage")])
    code, env, _ = run(["search", "q", "--max-requests", "4"], fake, env=environ | FX_FIRST)
    assert code == 1 and env["backend"] == "fxtwitter"
    assert env["error"] == {"code": "upstream_error", "message": "fxtwitter search: HTTP 404"}
    assert env["meta"]["notes"] == ["x-graphql was tried next and failed: HTTP 500: session outage"]


def test_session_first_does_not_reach_fxtwitter_once_the_cap_is_used(run, environ):
    fake = FakeTransport(x=[(500, b"session outage")])
    code, env, _ = run(["search", "q", "--max-requests", "1"], fake, env=environ | SESSION_FIRST)
    assert code == 1 and env["backend"] == "x-graphql" and env["error"]["code"] == "upstream_error"
    assert attempts(env) == [("x-graphql", "upstream_error", 1)] and env["meta"]["notes"] == []
    assert [urlsplit(u).hostname for u in fake.urls] == ["x.com"]


def test_blank_environment_value_falls_through_to_the_file(run, environ):
    path = Path(environ["RSRC_X_ENV"])
    path.write_text("RSRC_X_SEARCH=x-graphql\n")
    path.chmod(0o600)
    fake = FakeTransport(x=[(200, load("x_graphql_latest.json"))])
    code, env, _ = run(["search", "q", "--max-requests", "1"], fake, env=environ | {"RSRC_X_SEARCH": ""})
    assert code == 0 and env["backend"] == "x-graphql" and [urlsplit(u).hostname for u in fake.urls] == ["x.com"]


@pytest.mark.parametrize("in_file", [True, False])
def test_old_setting_requires_migration_even_with_search_order(run, environ, in_file):
    settings = environ | SESSION_FIRST
    if in_file:
        path = Path(settings["RSRC_X_ENV"])
        path.write_text("RSRC_X_FALLBACK=x-graphql\n")
        path.chmod(0o600)
    else:
        settings["RSRC_X_FALLBACK"] = "x-graphql"
    fake = FakeTransport()
    code, env, _ = run(["search", "q"], fake, env=settings)
    assert code == 2 and env["error"]["code"] == "config"
    assert "RSRC_X_SEARCH=fxtwitter,x-graphql" in env["error"]["message"]
    assert attempts(env) == env["meta"]["notes"] == fake.urls == []


def test_environment_search_order_overrides_file(run, environ):
    path = Path(environ["RSRC_X_ENV"])
    path.write_text("RSRC_X_SEARCH=fxtwitter\n")
    path.chmod(0o600)
    fake = FakeTransport(x=[(200, page(tweet("11")))])
    code, env, _ = run(["search", "q"], fake, env=environ | {"RSRC_X_SEARCH": "x-graphql"})
    assert code == 0 and env["error"] is None and env["backend"] == "x-graphql"
    assert attempts(env) == [("x-graphql", "ok", 1)] and env["meta"]["notes"] == []
    assert [urlsplit(u).hostname for u in fake.urls] == ["x.com"]
