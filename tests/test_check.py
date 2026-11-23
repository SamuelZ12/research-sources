"""`rsrc check` through rsrc.cli.main with fake cookies, gh runner and transport."""

import json
from pathlib import Path

import pytest

from rsrc import cli, proc
from rsrc.channels import check
from rsrc.envelope import ITEM_KEYS

TOKEN = "SENTINELAUTHTOKEN0123"
CT0 = "SENTINELCTZERO4567"
RATE_LIMIT = json.dumps({"resources": {
    "core": {"limit": 5000, "used": 1, "remaining": 4999, "reset": 1762400000},
    "search": {"limit": 30, "used": 0, "remaining": 30, "reset": 1762400000}}})
NOT_LOGGED_IN = proc.Result(1, "", "You are not logged into any GitHub hosts. "
                                   "To log in, run: gh auth login\n")
FX_STATUS = json.dumps({"code": 200, "message": "OK", "status": {
    "id": "20", "url": "https://x.com/jack/status/20", "text": "just setting up my twttr",
    "author": {"screen_name": "jack"}, "created_timestamp": 1142974214,
    "likes": 300000, "reposts": 120000, "replies": 20000}}).encode()
FX_SEARCH = (Path(__file__).parent / "fixtures" / "x_fx_search_latest.json").read_bytes()
HN_PROBE_URL = "https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage=1"
HN_FRONT_PAGE = json.dumps({"hits": json.loads(
    (Path(__file__).parent / "fixtures" / "hn_search.json").read_text())["hits"][:1]}).encode()
POLYMARKET_PROBE_URL = ("https://gamma-api.polymarket.com/public-search"
                        "?q=bitcoin&page=1&events_status=active&keep_closed_markets=0")
APPLE_APP = (Path(__file__).parent / "fixtures/appstore/wiki.json").read_bytes()
POLYMARKET_PAGE = (Path(__file__).parent / "fixtures" / "polymarket_search_p1.json").read_bytes()  # 5 events
BLUESKY_PAGE = (Path(__file__).parent / "fixtures" / "bluesky_search.json").read_bytes()
YOUTUBE_PROBE = ["yt-dlp", "--ignore-config", "--no-cookies-from-browser", "--retries", "0", "--extractor-retries", "0",
                 "--no-playlist", "--dump-single-json", "--skip-download",
                 "--extractor-args", "youtube:player_client=default,-tv_samsung",
                 "https://www.youtube.com/watch?v=jNQXAC9IVRw"]
# Hand-written: the probe video was not recorded. `_version` has the shape both recorded metadata runs printed.
YOUTUBE_META = json.dumps({"id": "jNQXAC9IVRw", "title": "Me at the zoo", "duration": 19, "language": "en",
                           "subtitles": {}, "automatic_captions": {},
                           "_version": {"version": "2026.08.19", "repository": "yt-dlp/yt-dlp"}})


def ok(stdout: str = "") -> proc.Result:
    return proc.Result(0, stdout, "")


class FakeGh:
    """Answers runner calls in order from canned Results or exceptions; records argv."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.argvs: list[list[str]] = []

    def __call__(self, argv, timeout, **kwargs):
        self.argvs.append(argv)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.urls: list[str] = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        return self.responses.pop(0)


def cookie_file(tmp_path: Path, mode=0o600, text=f"AUTH_TOKEN={TOKEN}\nCT0={CT0}\n") -> Path:
    path = tmp_path / "x.env"
    path.write_text(text)
    path.chmod(mode)
    return path


def assert_no_secret_in_files(tmp_path: Path, own_cookie_file: Path | None = None) -> None:
    """Sentinel cookie values appear in no file under tmp_path except the test's own cookie file."""
    for path in tmp_path.rglob("*"):
        if path.is_file() and path != own_cookie_file:
            data = path.read_bytes()
            assert TOKEN.encode() not in data and CT0.encode() not in data, path


def programs(tmp_path: Path, *names: str) -> str:
    """A PATH directory holding empty executables (never started: the runner is fake)."""
    directory = tmp_path / "bin"
    directory.mkdir(exist_ok=True)
    for name in names:
        (directory / name).write_text("")
        (directory / name).chmod(0o755)
    return str(directory)


@pytest.fixture
def environ(tmp_path):
    return {"RSRC_X_ENV": str(cookie_file(tmp_path)), "XDG_CACHE_HOME": str(tmp_path / "cache"),
            "PATH": programs(tmp_path, "yt-dlp", "deno", "vidscribe")}


@pytest.fixture
def rsrc(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CHANNELS", [check])

    def run(argv, *, environ, runner=None, transport=None):
        ctx = cli.Context(environ=environ, runner=runner or FakeGh(),
                          transport=transport or FakeTransport(), sleep=lambda s: None)
        code = cli.main(["check", *argv], ctx)
        captured = capsys.readouterr()
        assert len(captured.out.splitlines()) == 1
        for secret in (TOKEN, CT0):
            assert secret not in captured.out and secret not in captured.err
        return code, json.loads(captured.out)

    return run


def healthy_gh() -> FakeGh:
    return FakeGh(ok("github.com\n  Logged in to github.com\n"), ok(RATE_LIMIT))


def healthy_runner() -> FakeGh:
    """gh (auth status, rate_limit), then the reddit probe, then the youtube probe."""
    return FakeGh(ok("github.com\n  Logged in to github.com\n"), ok(RATE_LIMIT), proc.Result(0, "", "RSRC_REDDIT_OK 3\n"),
                  ok(YOUTUBE_META))


# all channels

LINKEDIN_POST = (Path(__file__).parent / "fixtures/linkedin_post.html").read_bytes()
PH_FIX = Path(__file__).parent / "fixtures" / "producthunt"
PH_RESPONSE = (200, (PH_FIX / "search.json").read_bytes())


def test_all_healthy_checks_every_channel_without_any_x_or_reddit_request(rsrc, environ, tmp_path):
    runner, transport = healthy_runner(), FakeTransport((200, HN_FRONT_PAGE), (200, POLYMARKET_PAGE), (200, BLUESKY_PAGE), PH_RESPONSE, (200, APPLE_APP), (200, b'{"jobs":[]}'), (200, LINKEDIN_POST))
    code, out = rsrc([], environ=environ, runner=runner, transport=transport)

    assert code == 0 and out["ok"] and out["error"] is None
    assert (out["channel"], out["action"]) == ("check", "check")
    assert out["input"] == {"channels": ["x", "gh", "reddit", "hn", "polymarket", "youtube", "bluesky", "producthunt", "appstore", "ashby", "linkedin"], "live_x": False,
                            "live_reddit": False}
    assert [i["id"] for i in out["items"]] == ["x", "gh", "reddit", "hn", "polymarket", "youtube", "bluesky", "producthunt", "appstore", "ashby", "linkedin"]
    assert all(set(i) == set(ITEM_KEYS) and i["source"] == i["id"] for i in out["items"])
    assert out["meta"]["sources"] == {"x": "ok", "gh": "ok", "reddit": "ok", "hn": "ok", "polymarket": "ok",
                                      "youtube": "ok", "bluesky": "ok", "producthunt": "ok", "appstore": "ok", "ashby": "ok", "linkedin": "ok"}
    assert transport.urls == [HN_PROBE_URL, POLYMARKET_PROBE_URL,
                              "https://api.bsky.app/xrpc/app.bsky.feed.searchPosts?q=python&sort=latest&limit=1",
                              check.producthunt.ENDPOINT,
                              "https://itunes.apple.com/search?entity=software&country=us&term=Wikipedia&limit=1",
                              "https://api.ashbyhq.com/posting-api/job-board/linear?includeCompensation=true", check.LINKEDIN_POST]
    assert [argv[0] for argv in runner.argvs] == ["gh", "gh", "ego-browser", "yt-dlp"]  # no `uv` run
    assert runner.argvs[:2] == [["gh", "auth", "status"], ["gh", "api", "rate_limit"]]
    [ego, youtube_probe] = runner.argvs[2:]
    assert youtube_probe == YOUTUBE_PROBE
    assert ego[:3] == ["ego-browser", "nodejs", "-e"] and "goto" not in ego[3] and "reddit.com" not in ego[3]

    x, gh, *_ = out["items"]
    assert x["raw"]["file_mode"] == "600" and x["raw"]["env_keys"] == [] and x["raw"]["x_graphql"] == "ready"
    assert x["raw"]["live"] is None
    assert "absent" in x["raw"]["query_id_cache"]["state"]
    assert "4999/5000" in gh["text"] and "search 30" in gh["text"]
    assert gh["raw"]["rate_limit"]["core"]["remaining"] == 4999
    assert [a["backend"] for a in out["meta"]["attempts"]] == ["gh", "gh", "ego-browser", "algolia", "gamma",
                                                               "yt-dlp", "api.bsky.app", "producthunt-mcp", "itunes", "ashby-posting-api", "linkedin-public"]
    assert_no_secret_in_files(tmp_path, Path(environ["RSRC_X_ENV"]))


def test_duplicate_channel_names_are_checked_once(rsrc, environ):
    code, out = rsrc(["gh", "gh"], environ=environ, runner=healthy_gh())
    assert code == 0 and [i["id"] for i in out["items"]] == ["gh"]
    assert out["input"]["channels"] == ["gh"]


def test_unknown_channel_is_a_usage_error(rsrc, environ):
    code, out = rsrc(["bogus"], environ=environ)
    assert code == 2 and not out["ok"]
    assert out["error"]["code"] == "usage" and "bogus" in out["error"]["message"]
    assert out["channel"] == "check"


def test_community_is_not_a_channel_and_nothing_runs(rsrc, environ):
    runner, transport = FakeGh(), FakeTransport()
    code, out = rsrc(["community"], environ=environ, runner=runner, transport=transport)
    assert code == 2 and not out["ok"]
    assert out["error"]["code"] == "usage" and "invalid channel 'community'" in out["error"]["message"]
    assert out["error"]["message"].endswith("(choose from x, gh, reddit, hn, polymarket, youtube, bluesky, producthunt, appstore, ashby, linkedin)")
    assert runner.argvs == [] and transport.urls == []


# x

def test_live_x_reads_one_post_and_runs_one_search(rsrc, environ):
    transport, runner = FakeTransport((200, FX_STATUS), (200, FX_SEARCH)), FakeGh()
    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": "fxtwitter"}, runner=runner, transport=transport)

    assert code == 0 and out["meta"]["sources"] == {"x": "ok"}
    assert [url.split("?")[0] for url in transport.urls] == ["https://api.fxtwitter.com/2/status/20",
                                                             "https://api.fxtwitter.com/2/search"]
    assert runner.argvs == []
    [item] = out["items"]
    assert item["raw"]["live"] == {"post": "20", "source": "fxtwitter", "requests": 2, "search": "ok",
                                   "search_error": None}
    assert "fxtwitter search ok" in item["text"]
    assert [(a["backend"], a["outcome"], a["requests"]) for a in out["meta"]["attempts"]] == [
        ("fxtwitter", "ok", 1), ("fxtwitter", "ok", 1)]


def test_live_x_sends_no_cookie_even_with_cookies_configured(rsrc, environ):
    sent = []

    def transport(method, url, headers, body):
        sent.append({k.lower(): v for k, v in (headers or {}).items()})
        return (200, FX_STATUS) if "/status/" in url else (404, b"")

    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": "fxtwitter,x-graphql"}, transport=transport)
    assert code == 0 and len(sent) == 2
    assert all(set(headers) == {"user-agent"} for headers in sent)


def test_live_x_search_with_no_results_is_still_a_working_search(rsrc, environ):
    transport = FakeTransport((200, FX_STATUS), (200, b'{"code": 200, "results": []}'))
    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": "fxtwitter"}, transport=transport)
    assert code == 0 and out["items"][0]["raw"]["live"]["search"] == "ok"


def test_live_x_dead_search_fails_x_after_one_search_request(rsrc, environ):
    transport = FakeTransport((200, FX_STATUS), (404, b""))
    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": "fxtwitter"}, transport=transport)
    assert code == 1 and out["error"]["code"] == "upstream_error"
    assert out["meta"]["sources"] == {"x": "upstream_error"} and len(transport.urls) == 2
    assert "fxtwitter search: HTTP 404" in out["error"]["message"] and "RSRC_X_SEARCH" in out["error"]["message"]
    assert out["items"][0]["raw"]["live"]["search"] == "upstream_error"


@pytest.mark.parametrize(("order", "role"), [("fxtwitter,x-graphql", "so searches fall back to x-graphql"), ("x-graphql,fxtwitter", "the fallback here")])
def test_live_x_dead_search_with_a_usable_session_leaves_x_ok(rsrc, environ, order, role):
    transport = FakeTransport((200, FX_STATUS), (404, b""))
    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": order}, transport=transport)
    [item] = out["items"]
    assert code == 0 and out["meta"]["sources"] == {"x": "ok"}
    assert "fxtwitter search: HTTP 404" in item["text"] and role in item["text"]
    assert "which this check did not try" in item["text"]
    assert item["raw"]["live"]["search"] == "upstream_error"
    assert all("api.fxtwitter.com" in url for url in transport.urls)  # nothing is sent to x.com


def test_live_x_dead_search_with_a_fallback_but_no_cookies_fails_x(rsrc, tmp_path):
    environ = {"RSRC_X_ENV": str(tmp_path / "absent"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
               "RSRC_X_SEARCH": "fxtwitter,x-graphql"}
    transport = FakeTransport((200, FX_STATUS), (404, b""))
    code, out = rsrc(["x", "--live-x"], environ=environ, transport=transport)
    assert code == 1 and out["meta"]["sources"] == {"x": "upstream_error"}
    message = out["error"]["message"] if out.get("error") else out["items"][0]["text"]
    assert "x-graphql is in RSRC_X_SEARCH but cannot take over" in message and "missing AUTH_TOKEN, CT0" in message
    assert "put x-graphql in RSRC_X_SEARCH" not in message


@pytest.mark.parametrize("order", ["fxtwitter,x-graphql", "x-graphql,fxtwitter"])
def test_live_x_rate_limited_search_fails_x_even_with_a_fallback(rsrc, environ, order):
    transport = FakeTransport((200, FX_STATUS), (429, b"{}"))
    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": order}, transport=transport)
    assert code == 1 and out["meta"]["sources"] == {"x": "rate_limited"} and len(transport.urls) == 2


def test_live_x_rate_limit_fails_the_x_probe(rsrc, environ):
    transport = FakeTransport((429, b"{}"))
    code, out = rsrc(["x", "--live-x"], environ=environ, transport=transport)
    assert code == 1 and out["error"]["code"] == "rate_limited"
    assert out["meta"]["sources"] == {"x": "rate_limited"} and len(transport.urls) == 1


def test_live_x_unresolvable_post_fails_with_the_read_failure_code(rsrc, environ):
    transport = FakeTransport((404, b""), (404, b""))
    code, out = rsrc(["x", "--live-x"], environ=environ, transport=transport)
    assert code == 1 and out["error"]["code"] == "not_found"
    assert out["meta"]["sources"] == {"x": "not_found"} and len(transport.urls) == 2


def test_missing_cookies_leave_x_ok_and_name_the_unavailable_backend(rsrc, tmp_path):
    # x search defaults to FxTwitter (no login); only `--backend x-graphql` needs the cookies.
    environ = {"RSRC_X_ENV": str(tmp_path / "absent"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
               "PATH": programs(tmp_path, "yt-dlp", "deno")}
    code, out = rsrc([], environ=environ, runner=healthy_runner(),
                     transport=FakeTransport((200, HN_FRONT_PAGE), (200, POLYMARKET_PAGE), (200, BLUESKY_PAGE), PH_RESPONSE, (200, APPLE_APP), (200, b'{"jobs":[]}'), (200, LINKEDIN_POST)))

    assert code == 0 and out["ok"] and out["error"] is None
    assert out["meta"]["sources"] == {"x": "ok", "gh": "ok", "reddit": "ok", "hn": "ok", "polymarket": "ok",
                                      "youtube": "ok", "bluesky": "ok", "producthunt": "ok", "appstore": "ok", "ashby": "ok", "linkedin": "ok"}
    x = out["items"][0]
    assert "x-graphql unavailable: missing AUTH_TOKEN, CT0" in x["text"]
    assert x["raw"]["file_exists"] is False and x["raw"]["x_graphql"] == "unavailable"


def test_partial_cookie_file_never_echoes_the_value(rsrc, tmp_path):
    own = cookie_file(tmp_path, text=f"AUTH_TOKEN={TOKEN}\n")
    environ = {"RSRC_X_ENV": str(own),
               "XDG_CACHE_HOME": str(tmp_path / "cache")}
    code, out = rsrc(["x"], environ=environ)
    assert code == 2 and "missing CT0" in out["items"][0]["text"]
    assert_no_secret_in_files(tmp_path, own)


def test_cookies_from_environment_are_reported_by_key_only(rsrc, tmp_path):
    environ = {"AUTH_TOKEN": TOKEN, "CT0": CT0, "RSRC_X_ENV": str(tmp_path / "absent"),
               "XDG_CACHE_HOME": str(tmp_path / "cache")}
    code, out = rsrc(["x"], environ=environ)
    assert code == 0 and out["items"][0]["raw"]["env_keys"] == ["AUTH_TOKEN", "CT0"]
    assert "environment" in out["items"][0]["text"]
    assert_no_secret_in_files(tmp_path)


def test_cookie_file_readable_by_others_is_config(rsrc, tmp_path):
    path = cookie_file(tmp_path, mode=0o644)
    code, out = rsrc(["x"], environ={"RSRC_X_ENV": str(path), "XDG_CACHE_HOME": str(tmp_path / "cache")})
    assert code == 2 and out["error"]["code"] == "config"
    assert out["meta"]["sources"] == {"x": "config"}
    assert "chmod 600" in out["items"][0]["text"] and out["items"][0]["raw"]["file_mode"] == "644"
    assert_no_secret_in_files(tmp_path, path)


def test_query_id_cache_state_is_reported(rsrc, environ, tmp_path):
    cache = tmp_path / "cache" / "rsrc" / "x-query-ids.json"
    cache.parent.mkdir(parents=True)
    cache.write_text('{"SearchTimeline": "abc123"}\n')
    code, out = rsrc(["x"], environ=environ)
    assert code == 0 and out["items"][0]["raw"]["query_id_cache"]["state"] == "readable"


def test_x_reports_its_default_paths_under_rsrc_names(rsrc, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    code, out = rsrc(["x"], environ={})
    raw = out["items"][0]["raw"]
    assert code == 0
    assert raw["cookie_file"] == str(tmp_path / ".config" / "rsrc" / "x.env")
    assert raw["query_id_cache"]["path"] == str(tmp_path / ".cache" / "rsrc" / "x-query-ids.json")


# gh

@pytest.mark.parametrize("result", [NOT_LOGGED_IN, proc.Missing("gh")])
def test_gh_public_only_is_usable(rsrc, environ, result):
    repo = json.loads((Path(__file__).parent / "fixtures/gh_repo.json").read_text())
    repo["full_name"] = "cli/cli"
    transport = FakeTransport((200, json.dumps(repo).encode()))
    code, out = rsrc(["gh"], environ=environ, runner=FakeGh(result), transport=transport)
    assert code == 0 and out["meta"]["sources"] == {"gh": "ok"}
    assert out["items"][0]["raw"] == {"auth": "not logged in", "public_reads": "ok"}
    assert transport.urls == ["https://api.github.com/repos/cli/cli"]


def test_gh_auth_status_failure_output_is_not_echoed(rsrc, environ):
    runner = FakeGh(proc.Result(1, "", "github.com\n  X Failed to log in using token (GH_TOKEN)\n"))
    code, out = rsrc(["gh"], environ=environ, runner=runner)
    assert code == 2 and out["error"]["code"] == "config"
    assert "GH_TOKEN" not in json.dumps(out)


def test_gh_timeout(rsrc, environ):
    code, out = rsrc(["gh"], environ=environ, runner=FakeGh(proc.Timeout("gh timed out")))
    assert out["error"]["code"] == "timeout" and out["meta"]["sources"] == {"gh": "timeout"}


def test_gh_rate_limited(rsrc, environ):
    runner = FakeGh(ok(), proc.Result(1, "", "gh: API rate limit exceeded for user ID 1. (HTTP 403)\n"))
    code, out = rsrc(["gh"], environ=environ, runner=runner)
    assert code == 1 and out["error"]["code"] == "rate_limited"


def test_gh_malformed_rate_limit_json_is_upstream_error(rsrc, environ):
    code, out = rsrc(["gh"], environ=environ, runner=FakeGh(ok(), ok('{"resources": {}}')))
    assert code == 1 and out["error"]["code"] == "upstream_error"
    assert out["meta"]["attempts"][-1]["outcome"] == "upstream_error"


# reddit

@pytest.mark.parametrize("response, code_", [
    (proc.Missing("ego-browser"), "config"),
    (proc.Timeout("slow"), "timeout"),
    (proc.Result(1, "", "Error: connect ECONNREFUSED\n"), "upstream_error"),
    (ok("no marker\n"), "upstream_error"),
])
def test_reddit_probe_failures(rsrc, environ, response, code_):
    code, out = rsrc(["reddit"], environ=environ, runner=FakeGh(response))
    assert out["error"]["code"] == code_ and out["meta"]["sources"] == {"reddit": code_}
    assert code == (2 if code_ == "config" else 1)


def reddit_page(**fields) -> proc.Result:
    """What reddit_page.js prints for one loaded page."""
    page = {"loads": 1, "url": "https://www.reddit.com/search/", "title": "", "page_text": "",
            "search_page": True, "posts": [], "post": None, "comments": []}
    return proc.Result(0, "", "RSRC_REDDIT " + json.dumps({**page, **fields}) + "\n")


REDDIT_OK = proc.Result(0, "", "RSRC_REDDIT_OK 3\n")
REDDIT_POSTS = json.loads((Path(__file__).parent / "fixtures" / "reddit_search.json").read_text())["posts"]


def test_live_reddit_loads_one_search_page_after_the_local_probe(rsrc, environ):
    runner = FakeGh(REDDIT_OK, reddit_page(posts=REDDIT_POSTS))
    code, out = rsrc(["reddit", "--live-reddit"], environ=environ, runner=runner)
    [item] = out["items"]
    assert code == 0 and out["meta"]["sources"] == {"reddit": "ok"} and out["input"]["live_reddit"] is True
    assert len(runner.argvs) == 2 and "reddit.com/search/" in runner.argvs[1][3]
    assert item["raw"] == {"ego_browser": "ok", "live": {"query": "python", "posts": 1}}
    assert "1 page load" in item["text"]
    assert [(a["backend"], a["outcome"]) for a in out["meta"]["attempts"]] == [("ego-browser", "ok")] * 2


@pytest.mark.parametrize("page, code_", [
    (reddit_page(search_page=False, page_text="You've been blocked by network security."), "blocked"),
    (reddit_page(search_page=False, title="Too Many Requests", page_text="whoa there, pardner!"), "rate_limited"),
    (reddit_page(search_page=False, title="reddit"), "upstream_error"),   # markup rsrc cannot read
    (reddit_page(), "upstream_error"),                                     # a search page with no results
])
def test_live_reddit_fails_like_a_search_would(rsrc, environ, page, code_):
    code, out = rsrc(["reddit", "--live-reddit"], environ=environ, runner=FakeGh(REDDIT_OK, page))
    assert code == 1 and out["meta"]["sources"] == {"reddit": code_}
    assert out["meta"]["attempts"][-1]["outcome"] == code_


def test_live_reddit_is_skipped_when_ego_does_not_answer(rsrc, environ):
    runner = FakeGh(proc.Timeout("slow"))
    code, out = rsrc(["reddit", "--live-reddit"], environ=environ, runner=runner)
    assert out["meta"]["sources"] == {"reddit": "timeout"} and len(runner.argvs) == 1


# hn

def test_hn_probe_makes_exactly_one_keyless_request(rsrc, environ):
    transport, runner = FakeTransport((200, HN_FRONT_PAGE)), FakeGh()
    code, out = rsrc(["hn"], environ=environ, runner=runner, transport=transport)

    assert code == 0 and out["ok"] and out["meta"]["sources"] == {"hn": "ok"}
    assert transport.urls == [HN_PROBE_URL]
    assert runner.argvs == []
    [item] = out["items"]
    assert set(item) == set(ITEM_KEYS) and item["id"] == item["source"] == "hn"
    assert "1 request" in item["text"]
    assert item["raw"] == {"front_page_story": json.loads(HN_FRONT_PAGE)["hits"][0]["objectID"]}
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("algolia", "ok", 1)


@pytest.mark.parametrize("response, code_", [
    ((429, b"Too Many Requests"), "rate_limited"),
    ((503, b""), "upstream_error"),
    ((200, b"<html>maintenance</html>"), "blocked"),
    ((200, b'{"hits": []}'), "upstream_error"),  # an empty front page is not a working API
])
def test_hn_probe_failures(rsrc, environ, response, code_):
    transport = FakeTransport(response, (200, HN_FRONT_PAGE))
    code, out = rsrc(["hn"], environ=environ, transport=transport)

    assert code == 1 and out["error"]["code"] == code_ and out["error"]["message"].startswith("hn: ")
    assert out["meta"]["sources"] == {"hn": code_}
    assert [a["outcome"] for a in out["meta"]["attempts"]] == [code_]
    assert len(transport.urls) == 1


def test_hn_probe_without_a_response_is_network_error(rsrc, environ):
    def refused(method, url, headers, body):
        raise ConnectionRefusedError("refused")

    code, out = rsrc(["hn"], environ=environ, transport=refused)
    assert code == 1 and out["error"]["code"] == "network_error"
    assert out["meta"]["sources"] == {"hn": "network_error"}


# polymarket

def test_polymarket_probe_makes_exactly_one_keyless_request(rsrc, environ):
    transport, runner = FakeTransport((200, POLYMARKET_PAGE)), FakeGh()
    code, out = rsrc(["polymarket"], environ=environ, runner=runner, transport=transport)

    assert code == 0 and out["ok"] and out["meta"]["sources"] == {"polymarket": "ok"}
    assert transport.urls == [POLYMARKET_PROBE_URL]
    assert runner.argvs == []
    [item] = out["items"]
    assert set(item) == set(ITEM_KEYS) and item["id"] == item["source"] == "polymarket"
    assert "1 request" in item["text"]
    assert item["raw"] == {"events": 5}
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("gamma", "ok", 1)


@pytest.mark.parametrize("body", [
    (Path(__file__).parent / "fixtures" / "polymarket_search_empty.json").read_bytes(),  # recorded: no `events` key
    b'{"events": [], "pagination": {"hasMore": false, "totalResults": 0}}',
])
def test_polymarket_probe_is_ok_when_the_search_matches_nothing(rsrc, environ, body):
    code, out = rsrc(["polymarket"], environ=environ, transport=FakeTransport((200, body)))
    assert code == 0 and out["meta"]["sources"] == {"polymarket": "ok"}
    assert out["items"][0]["raw"] == {"events": 0}


@pytest.mark.parametrize("response, code_", [
    ((429, b"Too Many Requests"), "rate_limited"),
    ((503, b""), "upstream_error"),
    ((200, b"<html>maintenance</html>"), "blocked"),
    ((200, b'{"pagination": {"hasMore": false}}'), "upstream_error"),
])
def test_polymarket_probe_failures(rsrc, environ, response, code_):
    transport = FakeTransport(response, (200, POLYMARKET_PAGE))
    code, out = rsrc(["polymarket"], environ=environ, transport=transport)

    assert code == 1 and out["error"]["code"] == code_ and out["error"]["message"].startswith("polymarket: ")
    assert out["meta"]["sources"] == {"polymarket": code_}
    assert [a["outcome"] for a in out["meta"]["attempts"]] == [code_]
    assert len(transport.urls) == 1


def test_polymarket_probe_without_a_response_is_network_error(rsrc, environ):
    def refused(method, url, headers, body):
        raise ConnectionRefusedError("refused")

    code, out = rsrc(["polymarket"], environ=environ, transport=refused)
    assert code == 1 and out["error"]["code"] == "network_error"
    assert out["meta"]["sources"] == {"polymarket": "network_error"}


# every channel failing

def test_every_channel_failing_reports_the_first_failure(rsrc, tmp_path):
    environ = {"RSRC_X_ENV": str(cookie_file(tmp_path, mode=0o644)), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    code, out = rsrc([], environ=environ, runner=FakeGh(proc.Timeout("slow"), proc.Missing("ego-browser")),
                     transport=FakeTransport(*((503, b""),) * 7))
    assert code == 2 and not out["ok"]
    assert out["error"]["code"] == "config" and out["error"]["message"].startswith("x: ")
    assert out["meta"]["sources"] == {"x": "config", "gh": "timeout", "reddit": "config",
                                      "hn": "upstream_error", "polymarket": "upstream_error", "youtube": "config",
                                      "bluesky": "upstream_error", "producthunt": "upstream_error",
                                      "appstore": "upstream_error", "ashby": "upstream_error", "linkedin": "upstream_error"}
    assert len(out["items"]) == 11


def test_every_checked_channel_failing_with_runtime_error_exits_1(rsrc, environ):
    runner = FakeGh(proc.Timeout("slow"), proc.Missing("ego-browser"))
    code, out = rsrc(["gh", "reddit"], environ=environ, runner=runner)
    assert code == 1 and out["error"]["code"] == "timeout"
    assert out["meta"]["sources"] == {"gh": "timeout", "reddit": "config"}


# youtube

def test_youtube_probe_is_one_metadata_run_for_a_fixed_public_video(rsrc, environ):
    runner = FakeGh(ok(YOUTUBE_META))

    code, out = rsrc(["youtube"], environ=environ, runner=runner)

    [item] = out["items"]
    assert code == 0 and out["meta"]["sources"] == {"youtube": "ok"}
    assert runner.argvs == [YOUTUBE_PROBE]
    assert item["raw"] == {"video": "jNQXAC9IVRw", "yt_dlp_version": "2026.08.19", "vidscribe": True}
    assert "yt-dlp 2026.08.19" in item["text"] and "vidscribe found" in item["text"]
    assert out["meta"]["attempts"] == [
        {"backend": "yt-dlp", "outcome": "ok", "requests": 1, "ms": out["meta"]["attempts"][0]["ms"]}]


def test_youtube_probe_reports_a_missing_vidscribe_without_failing(rsrc, environ):
    Path(environ["PATH"], "vidscribe").unlink()

    code, out = rsrc(["youtube"], environ=environ, runner=FakeGh(ok(YOUTUBE_META)))

    [item] = out["items"]
    assert code == 0 and out["meta"]["sources"] == {"youtube": "ok"}
    assert item["raw"]["vidscribe"] is False and "vidscribe not found" in item["text"]


def test_youtube_probe_without_a_stated_version_is_still_ok(rsrc, environ):
    meta = json.dumps({key: value for key, value in json.loads(YOUTUBE_META).items() if key != "_version"})

    code, out = rsrc(["youtube"], environ=environ, runner=FakeGh(ok(meta)))

    [item] = out["items"]
    assert code == 0 and out["meta"]["sources"] == {"youtube": "ok"}
    assert item["raw"]["yt_dlp_version"] is None and "yt-dlp of unknown version" in item["text"]


@pytest.mark.parametrize("program", ["yt-dlp", "deno"])
def test_youtube_probe_without_a_needed_program_is_config_with_no_run(rsrc, environ, program):
    Path(environ["PATH"], program).unlink()
    runner = FakeGh()

    code, out = rsrc(["youtube"], environ=environ, runner=runner)

    assert code == 2 and out["meta"]["sources"] == {"youtube": "config"} and runner.argvs == []
    assert program in out["error"]["message"] and out["error"]["message"].startswith("youtube: ")
    assert out["items"][0]["raw"] == {"video": "jNQXAC9IVRw", "yt_dlp_version": None, "vidscribe": True}
    assert out["meta"]["attempts"][0]["outcome"] == "config" and out["meta"]["attempts"][0]["requests"] == 0


def test_youtube_probe_with_a_warning_is_not_ok(rsrc, environ):
    # the state of 2026-11-11 and 2026-11-14: yt-dlp exits 0, warns, and every read would return no transcript
    warning = "WARNING: [youtube] jNQXAC9IVRw: Some web client https formats have been skipped\n"

    code, out = rsrc(["youtube"], environ=environ, runner=FakeGh(proc.Result(0, YOUTUBE_META, warning)))

    assert code == 1 and out["meta"]["sources"] == {"youtube": "upstream_error"}
    assert "Some web client https formats have been skipped" in out["error"]["message"]
    assert out["items"][0]["raw"]["yt_dlp_version"] == "2026.08.19"


@pytest.mark.parametrize("response, code_", [
    (proc.Result(1, "", "ERROR: [youtube] jNQXAC9IVRw: HTTP Error 429: Too Many Requests\n"), "rate_limited"),
    (proc.Result(1, "", "ERROR: [youtube] jNQXAC9IVRw: Sign in to confirm you’re not a bot.\n"), "blocked"),
    (proc.Result(1, "", "ERROR: [youtube] jNQXAC9IVRw: Video unavailable\n"), "not_found"),
    (proc.Result(1, "", "ERROR: unexpected\n"), "upstream_error"),
    (proc.Timeout("yt-dlp timed out after 30 s"), "timeout"),
    (ok("not json"), "upstream_error"),
    (ok(json.dumps({"id": "C33hJGt-ySg"})), "url_mismatch"),
])
def test_youtube_probe_failures(rsrc, environ, response, code_):
    runner = FakeGh(response)

    code, out = rsrc(["youtube"], environ=environ, runner=runner)

    assert code == 1 and out["meta"]["sources"] == {"youtube": code_} and len(runner.argvs) == 1
    assert out["error"]["code"] == code_ and out["meta"]["attempts"][0]["outcome"] == code_


def test_x_reports_the_search_fallback_when_configured(rsrc, tmp_path):
    environ = {"RSRC_X_ENV": str(tmp_path / "absent"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
               "RSRC_X_SEARCH": "fxtwitter,x-graphql"}
    code, out = rsrc(["x"], environ=environ)
    assert code == 0 and out["items"][0]["raw"]["search_order"] == ["fxtwitter", "x-graphql"]
    assert "then x-graphql with your X session" in out["items"][0]["text"]


def test_x_defaults_to_fxtwitter_when_unset(rsrc, tmp_path):
    environ = {"RSRC_X_ENV": str(tmp_path / "absent"), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    code, out = rsrc(["x"], environ=environ)
    assert code == 0 and out["items"][0]["raw"]["search_order"] == ["fxtwitter"]


def test_x_bad_fallback_setting_is_config(rsrc, tmp_path):
    environ = {"RSRC_X_ENV": str(tmp_path / "absent"), "XDG_CACHE_HOME": str(tmp_path / "cache"),
               "RSRC_X_SEARCH": "yes"}
    code, out = rsrc(["x"], environ=environ)
    assert code == 2 and out["error"]["code"] == "config" and out["meta"]["sources"] == {"x": "config"}


@pytest.mark.parametrize("order", ["x-graphql", "x-graphql,fxtwitter"])
def test_x_session_first_requires_cookies_even_without_live_probe(rsrc, tmp_path, order):
    transport = FakeTransport()
    environ = {"RSRC_X_SEARCH": order, "RSRC_X_ENV": str(tmp_path / "absent")}
    code, out = rsrc(["x"], environ=environ, transport=transport)
    assert code == 2 and out["error"]["code"] == "config"
    assert out["meta"]["sources"] == {"x": "config"}
    assert out["items"][0]["raw"]["search_order"] == order.split(",")
    assert out["meta"]["attempts"] == [] and transport.urls == []


def test_live_x_session_only_reads_the_public_post(rsrc, environ):
    transport = FakeTransport((200, FX_STATUS))
    code, out = rsrc(["x", "--live-x"], environ=environ | {"RSRC_X_SEARCH": "x-graphql"}, transport=transport)
    assert code == 0 and out["meta"]["sources"] == {"x": "ok"}
    assert out["items"][0]["raw"]["search_order"] == ["x-graphql"]
    assert "searches run on x-graphql with your X session, which this check did not try" in out["items"][0]["text"]
    assert transport.urls == ["https://api.fxtwitter.com/2/status/20"]


def test_help_explains_default_live_probes(capsys):
    assert cli.main(["check", "--help"]) == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for probe in ("Bluesky search", "Product Hunt search", "App Store catalog", "YouTube metadata",
                  "GitHub access"):
        assert probe in help_text


def test_public_github_probe_must_reach_the_api(rsrc, environ):
    class Unreachable:
        def __call__(self, *args):
            raise OSError("offline")
    code, out = rsrc(["gh"], environ=environ, runner=FakeGh(NOT_LOGGED_IN), transport=Unreachable())
    assert code == 1 and out["meta"]["sources"] == {"gh": "network_error"}
    assert out["items"][0]["text"] == "Could not read GitHub API response"
