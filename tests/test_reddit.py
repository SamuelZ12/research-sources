"""`rsrc reddit` through rsrc.cli.main with a fake ego-browser runner.

Fixtures are DOM reads of pages captured through Ego on 2026-11-06 (search "claude code",
past month; thread t3_1xootop sorted by top, first 12 comments), extracted by reddit_page.js.
"""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from rsrc import cli, proc
from rsrc.channels import reddit
from rsrc.envelope import ITEM_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = json.loads((FIXTURES / "reddit_search.json").read_text())
THREAD = json.loads((FIXTURES / "reddit_thread.json").read_text())
READ_STATES = json.loads((FIXTURES / "reddit_read_states.json").read_text())
COMMENT_READ = json.loads((FIXTURES / "reddit_comment_read.json").read_text())
BLOCK_TEXT = ("You've been blocked by network security. If you think you've been blocked by mistake, "
              "file a ticket below and we'll look into it. File a ticket")


def page(**fields) -> dict:
    base = {"loads": 1, "url": "https://www.reddit.com/x", "title": "", "page_text": "", "search_page": False,
            "posts": [], "post": None, "comments": []}
    return {**base, **fields}


def printed(result: dict, before: str = "", ensure_ascii: bool = True) -> proc.Result:
    """ego-browser prints the script's console output on stderr. `ensure_ascii=False` mimics
    JSON.stringify, which leaves non-ASCII characters (including U+2028) raw."""
    return proc.Result(0, "", before + reddit.MARKER + json.dumps(result, ensure_ascii=ensure_ascii) + "\n")


class FakeEgo:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, timeout, **kwargs):
        self.calls.append((argv, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def input(self, n=0) -> dict:
        """The INPUT object the generated script carries."""
        script = self.calls[n][0][3]
        first, _, _ = script.partition("\n")
        assert first.startswith("const INPUT = ") and first.endswith(";")
        return json.loads(first[len("const INPUT = "):-1])


@pytest.fixture
def rsrc(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CHANNELS", [reddit])
    monkeypatch.setattr(reddit, "which", lambda name: name)

    def run(argv, fake, redirect=None):
        ctx = cli.Context(environ={}, runner=fake, redirect=redirect, sleep=lambda s: None)
        code = cli.main(["reddit", *argv], ctx)
        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        return code, json.loads(out)

    return run


# search

def test_search_maps_each_result(rsrc):
    fake = FakeEgo(printed(SEARCH))
    code, out = rsrc(["search", "claude code"], fake)

    assert code == 0 and out["ok"] and out["error"] is None
    assert (out["channel"], out["action"], out["backend"]) == ("reddit", "search", "ego-browser")
    assert out["input"] == {"query": "claude code", "subreddit": None, "time": "month", "sort": "relevance",
                            "limit": 25}
    [(argv, timeout)] = fake.calls
    assert argv[:3] == ["ego-browser", "nodejs", "-e"] and timeout == reddit.TIMEOUT
    sent = fake.input()
    assert sent["action"] == "search" and sent["limit"] == 25
    assert sent["url"] == "https://www.reddit.com/search/?q=claude+code&type=posts&t=month&sort=relevance"

    assert len(out["items"]) == 7 and all(set(i) == set(ITEM_KEYS) for i in out["items"])
    second = out["items"][1]
    assert second["id"] == "t3_1xlwzs3"
    assert second["url"] == ("https://www.reddit.com/r/codex/comments/1xlwzs3/"
                             "switched_from_codex_to_claude_code_on_the_same_20/")
    assert second["title"] == "Switched from Codex to Claude Code on the same $20 plan, and I'm blown away"
    assert second["author"] == "Accomplished-Sky773"
    assert second["published_at"] == "2026-11-02T13:20:41Z"
    assert second["text"] == "I recently switched from Codex to Claude Code, and I am blown away."
    assert second["engagement"] == {"score": 1028, "comments": 382}
    assert second["source"] == "reddit" and second["raw"]["context"]["subreddit"]["name"] == "codex"
    assert out["meta"]["attempts"] == [{"backend": "ego-browser", "outcome": "ok", "requests": 1,
                                        "ms": out["meta"]["attempts"][0]["ms"]}]
    assert out["meta"]["notes"] == ["7 of --limit 25: Reddit loaded no more results"]


def test_search_limit_cuts_results_without_a_note(rsrc):
    code, out = rsrc(["search", "claude code", "--limit", "3"], FakeEgo(printed(SEARCH)))
    assert code == 0 and [i["id"] for i in out["items"]] == ["t3_1xootop", "t3_1xlwzs3", "t3_1xnnqkg"]
    assert out["meta"]["notes"] == []


def test_search_drops_duplicate_posts_loaded_twice(rsrc):
    doubled = {**SEARCH, "posts": SEARCH["posts"] + SEARCH["posts"][:2]}
    code, out = rsrc(["search", "claude code"], FakeEgo(printed(doubled)))
    assert len(out["items"]) == 7 and len({i["id"] for i in out["items"]}) == 7


@pytest.mark.parametrize("given", ["r/ClaudeCode", "/r/ClaudeCode/", "ClaudeCode"])
def test_search_one_subreddit(rsrc, given):
    fake = FakeEgo(printed(SEARCH))
    rsrc(["search", "codex", "--subreddit", given, "--time", "week", "--sort", "top"], fake)
    assert fake.input()["url"] == ("https://www.reddit.com/r/ClaudeCode/search/"
                                   "?q=codex&type=posts&t=week&sort=top&restrict_sr=1")


def test_note_says_when_scrolling_hit_its_cap(rsrc):
    code, out = rsrc(["search", "q", "--limit", "100"], FakeEgo(printed({**SEARCH, "loads": 1 + reddit.MAX_SCROLLS})))
    assert out["meta"]["notes"] == [f"7 of --limit 100: stopped after {reddit.MAX_SCROLLS} scrolls"]


def test_query_reaches_the_script_as_json_not_code(rsrc):
    query = 'x"; await task.finish(); //\n</script>`${1}`'
    fake = FakeEgo(printed(SEARCH))
    rsrc(["search", query], fake)
    assert parse_qs(urlsplit(fake.input()["url"]).query)["q"] == [query]


def test_empty_search_page_is_no_results(rsrc):
    fake = FakeEgo(printed(page(search_page=True, title="zzqx - Reddit Search!")))
    code, out = rsrc(["search", "zzqx"], fake)
    assert code == 66 and out["ok"] and out["items"] == []
    assert out["meta"]["attempts"][0]["outcome"] == "no_results"


@pytest.mark.parametrize("fields, code_", [
    ({"page_text": BLOCK_TEXT}, "blocked"),
    ({"title": "Too Many Requests", "page_text": "whoa there, pardner! ..."}, "rate_limited"),
    ({"title": "Reddit - Dive into anything", "page_text": "Home Popular"}, "upstream_error"),
])
def test_search_page_without_results_is_classified(rsrc, fields, code_):
    code, out = rsrc(["search", "claude code"], FakeEgo(printed(page(**fields))))
    assert code == 1 and not out["ok"] and out["error"]["code"] == code_ and out["items"] == []
    assert out["meta"]["attempts"][0]["outcome"] == code_


def test_unrecognized_page_says_markup_may_have_changed(rsrc):
    code, out = rsrc(["search", "q"], FakeEgo(printed(page(title="Something else"))))
    assert "markup may have changed" in out["error"]["message"]


def test_block_phrase_inside_real_results_is_kept(rsrc):
    posts = [dict(SEARCH["posts"][0], snippet="access denied when I log in to continue")]
    code, out = rsrc(["search", "q"], FakeEgo(printed(page(search_page=True, posts=posts))))
    assert code == 0 and out["items"][0]["text"] == "access denied when I log in to continue"


def test_results_without_post_ids_fail_loudly(rsrc):
    posts = [{"context": {}, "snippet": None, "href": None, "ts": None, "counts": []}] * 3
    code, out = rsrc(["search", "q"], FakeEgo(printed(page(search_page=True, posts=posts))))
    assert code == 1 and out["error"]["code"] == "upstream_error" and "markup may have changed" in out["error"]["message"]


def test_one_unreadable_result_is_dropped_and_the_rest_kept(rsrc):
    posts = [{"context": {}, "counts": []}, *SEARCH["posts"][:2]]
    code, out = rsrc(["search", "q"], FakeEgo(printed(page(search_page=True, posts=posts))))
    assert code == 0 and [i["id"] for i in out["items"]] == ["t3_1xootop", "t3_1xlwzs3"]


@pytest.mark.parametrize("argv", [
    ["search", "   "],
    ["search", "q", "--limit", "0"],
    ["search", "q", "--limit", "101"],
    ["search", "q", "--subreddit", "not a sub"],
    ["search", "q", "--time", "decade"],
])
def test_search_usage_errors_make_no_call(rsrc, argv):
    fake = FakeEgo()
    code, out = rsrc(argv, fake)
    assert code == 2 and out["error"]["code"] == "usage" and fake.calls == []


# read

def test_read_returns_post_then_comments(rsrc):
    fake = FakeEgo(printed(THREAD))
    code, out = rsrc(["read", "https://www.reddit.com/r/claude/comments/1xootop/i_thought/"], fake)

    assert code == 0 and out["ok"] and out["action"] == "read"
    assert out["input"] == {"post": "https://www.reddit.com/r/claude/comments/1xootop/i_thought/",
                            "sort": "top", "comments": 50}
    assert fake.input()["url"] == "https://www.reddit.com/comments/1xootop/?sort=top"

    post, *comments = out["items"]
    assert post["id"] == "t3_1xootop" and post["author"] == "StephCurrySauce"
    assert post["url"] == ("https://www.reddit.com/r/claude/comments/1xootop/"
                           "i_thought_claude_was_just_another_llm_and_then_i/")
    assert post["published_at"] == "2026-11-05T17:34:19Z"
    assert post["engagement"] == {"score": 557, "comments": 159}
    assert post["text"].startswith("thought claude was just another LLM")
    assert post["raw"]["attrs"]["subreddit-prefixed-name"] == "r/claude"
    assert [item["raw"]["kind"] for item in out["items"]] == ["post"] + ["comment"] * len(comments)

    assert len(comments) == 12
    first, reply = comments[:2]
    assert first["id"] == "t1_19s6shbgfi0f4" and first["title"] is None and first["author"] == "GatePorters"
    assert first["engagement"] == {"score": 284} and first["published_at"] == "2026-11-05T17:46:09Z"
    assert first["text"].startswith("Yo dawg") and first["raw"]["attrs"]["depth"] == "0"
    assert reply["raw"]["attrs"]["parentid"] == first["id"] and reply["raw"]["attrs"]["depth"] == "1"


def test_read_never_asks_the_page_for_viewer_fields(rsrc):
    fake = FakeEgo(printed(THREAD))
    rsrc(["read", "1xootop"], fake)
    asked = fake.input()["attrs"]
    assert not {"user-id", "user-logged-in"} & set(asked["post"] + asked["comment"])


def test_read_comment_limit(rsrc):
    code, out = rsrc(["read", "1xootop", "--comments", "3"], FakeEgo(printed(THREAD)))
    assert [i["id"] for i in out["items"]] == ["t3_1xootop"] + [c["attrs"]["thingid"] for c in THREAD["comments"][:3]]
    code, out = rsrc(["read", "https://www.reddit.com/comments/1xootop.json", "--comments", "0"],
                      FakeEgo(printed(THREAD)))
    assert [i["id"] for i in out["items"]] == ["t3_1xootop"]


@pytest.mark.parametrize("suffix", [
    "comment/lq6bsnz/", "_/lq6bsnz/", "descriptive-title/lq6bsnz/?context=3",
    "comment/lq6bsnz.json", "comment/lq6bsnz/.json",
])
@pytest.mark.parametrize("limit, context", [(0, []), (10, ["t1_lpjwaxi"])])
def test_read_focuses_on_comment_and_context(rsrc, suffix, limit, context):
    fake = FakeEgo(printed(COMMENT_READ["focused"]))
    url = "https://www.reddit.com/r/DrewGooden/comments/1fs5dql/" + suffix
    code, out = rsrc(["read", url, "--comments", str(limit)], fake)
    assert code == 0
    assert [i["id"] for i in out["items"]] == ["t3_1fs5dql", "t1_lq6bsnz", *context]
    target = out["items"][1]
    assert target["raw"]["linked"] is True
    assert target["url"] == COMMENT_READ["focused"]["url"].split("?")[0]
    assert out["input"]["comment_id"] == "lq6bsnz"
    assert out["meta"]["notes"] == ["Focused on comment t1_lq6bsnz."]
    assert len(fake.calls) == out["meta"]["attempts"][0]["requests"] == 1
    assert fake.input()["url"] == "https://www.reddit.com/comments/1fs5dql/comment/lq6bsnz/?sort=top"


def test_focused_read_keeps_nested_replies_in_page_order(rsrc):
    fixture = COMMENT_READ["focused"]
    url = "https://www.reddit.com" + fixture["comments"][2]["attrs"]["permalink"]
    code, out = rsrc(["read", url, "--comments", "3"], FakeEgo(printed({**fixture, "url": url})))
    assert code == 0
    assert [item["id"] for item in out["items"]] == ["t3_1fs5dql", "t1_lphy20c", "t1_lq6du1s", "t1_lu4fjfn"]


@pytest.mark.parametrize("state, comment_id, expected", [
    ("missing", "zzzzzzz", "not_found"),
    ("wrong-confirm", "lq6bsnz", "url_mismatch"),
    ("removed", "lq6bsnz", "not_found"),
    ("unreadable", "lq6bsnz", "upstream_error"),
    ("wrong-post-attribute", "lq6bsnz", "url_mismatch"),
])
def test_focused_read_reports_missing_or_crossed_comments(rsrc, state, comment_id, expected):
    fixture = COMMENT_READ.get(state, COMMENT_READ["focused"])
    if state in ("removed", "unreadable", "wrong-post-attribute"):
        ancestor, linked, *rest = fixture["comments"]
        if state in ("removed", "unreadable"):
            linked = {**linked, "text": "[removed]" if state == "removed" else None}
        else:
            linked = {**linked, "attrs": {**linked["attrs"], "postid": "t3_1641krf"}}
        fixture = {**fixture, "comments": [ancestor, linked, *rest]}
    post_id = "1641krf" if state == "wrong-confirm" else "1fs5dql"
    url = f"https://www.reddit.com/comments/{post_id}/comment/{comment_id}/"
    code, out = rsrc(["read", url], FakeEgo(printed(fixture)))
    assert code == 1 and out["error"]["code"] == expected
    # A verified post stays readable when only its linked comment fails.
    assert [item["id"] for item in out["items"]] == ([] if expected == "url_mismatch" else ["t3_1fs5dql"])
    assert f"t1_{comment_id}" in out["error"]["message"]


def test_focused_read_times_out_when_the_comment_section_rendered_without_the_comment(rsrc):
    """Recorded live Nov 25 2026: a comment that does not exist gets a page with the post and no
    comment section, so a rendered section without the linked comment means it was still loading."""
    fixture = COMMENT_READ["focused"]
    others = [c for c in fixture["comments"] if c["attrs"]["thingid"] != "t1_lq6bsnz"]
    url = "https://www.reddit.com/comments/1fs5dql/comment/lq6bsnz/"
    code, out = rsrc(["read", url], FakeEgo(printed({**fixture, "comments": others, "comment_tree": True})))
    assert code == 1 and out["error"]["code"] == "timeout" and "t1_lq6bsnz" in out["error"]["message"]
    assert [item["id"] for item in out["items"]] == ["t3_1fs5dql"]
    assert out["meta"]["attempts"][-1]["outcome"] == "timeout"

    code, out = rsrc(["read", url], FakeEgo(printed({**COMMENT_READ["missing"], "comment_tree": False})))
    assert code == 1 and out["error"]["code"] == "not_found"


@pytest.mark.parametrize("body", [None, "  ", "[deleted]", "Comment removed by moderator"])
def test_focused_read_skips_context_comments_without_a_readable_body(rsrc, body):
    fixture = COMMENT_READ["focused"]
    ancestor, linked, *rest = fixture["comments"]
    url = "https://www.reddit.com/comments/1fs5dql/comment/lq6bsnz/"
    code, out = rsrc(["read", url], FakeEgo(printed({**fixture, "comments": [{**ancestor, "text": body}, linked, *rest]})))
    assert code == 0 and [item["id"] for item in out["items"]] == ["t3_1fs5dql", "t1_lq6bsnz"]


def test_focused_read_does_not_call_a_comment_without_a_post_id_another_posts(rsrc):
    fixture = COMMENT_READ["focused"]
    ancestor, linked, *rest = fixture["comments"]
    attrs = {key: value for key, value in linked["attrs"].items() if key != "postid"}
    url = "https://www.reddit.com/comments/1fs5dql/comment/lq6bsnz/"
    code, out = rsrc(["read", url], FakeEgo(printed({**fixture, "comments": [ancestor, {**linked, "attrs": attrs}, *rest]})))
    assert code == 1 and out["error"]["code"] == "upstream_error" and "t1_lq6bsnz" in out["error"]["message"]
    assert [item["id"] for item in out["items"]] == ["t3_1fs5dql"]


@pytest.mark.parametrize("url, target", [
    ("https://www.reddit.com/comments/1fs5dql.rss", ("1fs5dql", None)),
    ("https://www.reddit.com/r/DrewGooden/comments/1fs5dql/slug/lq6bsnz.json", ("1fs5dql", "lq6bsnz")),
    ("https://www.reddit.com/comments/1fs5dql/comment/lq6\u212Asnz/", None),
    ("https://redd.it/abc\u212A", None),
    ("abc\u212A", None),
])
def test_post_urls_keep_feed_suffixes_and_reject_non_ascii_ids(url, target):
    assert reddit.parse_post(url) == target


def test_read_rejects_an_invalid_comment_id_without_loading_the_thread(rsrc):
    fake = FakeEgo(printed(COMMENT_READ["focused"]))
    code, out = rsrc(["read", "https://www.reddit.com/comments/1fs5dql/comment/lq6!bsnz/"], fake)
    assert code == 2 and out["error"]["code"] == "usage" and not fake.calls


def test_line_separator_characters_in_comments_survive(rsrc):
    """JSON.stringify leaves U+2028/U+2029/U+0085 raw; they must not cut the result line."""
    comment = {"attrs": dict(THREAD["comments"][0]["attrs"]), "text": "one\u2028two\u2029three\u0085four"}
    code, out = rsrc(["read", "1xootop"], FakeEgo(printed({**THREAD, "comments": [comment]}, ensure_ascii=False)))
    assert code == 0 and out["items"][1]["text"] == "one\u2028two\u2029three\u0085four"


@pytest.mark.parametrize("ref", [
    "https://www.reddit.com/r/claude/comments/1xootop/i_thought/",
    "https://old.reddit.com/r/claude/comments/1xootop/",
    "https://reddit.com/comments/1xootop",
    "https://redd.it/1xootop",
    "https://redd.it/1xootop?utm_source=share",
    "https://www.reddit.com/user/someone/comments/1xootop/title/",
    "https://m.reddit.com/r/claude/comments/1xootop/",
    "t3_1xootop",
    "1XOOTOP",
])
def test_read_accepts_post_references(rsrc, ref):
    fake = FakeEgo(printed(THREAD))
    code, out = rsrc(["read", ref], fake)
    assert code == 0 and fake.input()["url"] == "https://www.reddit.com/comments/1xootop/?sort=top"


@pytest.mark.parametrize("ref, phrase", [
    ("https://example.com/r/claude/comments/1xootop/", "not a Reddit post"),
    ("not an id!", "not a Reddit post"),
])
def test_read_rejects_other_references_before_any_call(rsrc, ref, phrase):
    fake = FakeEgo()
    code, out = rsrc(["read", ref], fake)
    assert code == 2 and out["error"]["code"] == "usage" and phrase in out["error"]["message"]
    assert fake.calls == []


class FakeRedirect:
    """Stands in for reddit.com answering a share link: (status, the Location header)."""

    def __init__(self, status=301, location=""):
        self.response = status, location.encode()
        self.calls = []

    def __call__(self, method, url, headers, body):
        self.calls.append((method, url, headers))
        if isinstance(self.response[0], Exception):
            raise self.response[0]
        return self.response


SHARED = "https://www.reddit.com/r/claude/comments/1xootop/i_thought/?share_id=abc&utm_source=share"


@pytest.mark.parametrize("link", ["https://www.reddit.com/r/claude/s/AbCdEf123",
                                  "https://reddit.com/r/claude/s/AbCdEf123/",
                                  "http://old.reddit.com/r/claude/s/AbCdEf123?utm_medium=web"])
def test_read_follows_a_share_link_to_its_post(rsrc, link):
    fake, redirect = FakeEgo(printed(THREAD)), FakeRedirect(301, SHARED)
    code, out = rsrc(["read", link, "--sort", "new"], fake, redirect)
    assert code == 0 and out["items"][0]["id"] == "t3_1xootop" and len(out["items"]) == 13
    assert out["input"]["post"] == link
    assert [(method, url) for method, url, _ in redirect.calls] == [
        ("GET", "https://www.reddit.com/r/claude/s/AbCdEf123")]
    assert fake.input()["url"] == "https://www.reddit.com/comments/1xootop/?sort=new"
    assert [(a["backend"], a["outcome"], a["requests"]) for a in out["meta"]["attempts"]] == [
        ("reddit-redirect", "ok", 1), ("ego-browser", "ok", 1)]


def test_share_link_request_carries_no_cookie(rsrc):
    redirect = FakeRedirect(301, SHARED)
    rsrc(["read", "https://www.reddit.com/r/claude/s/AbCdEf123"], FakeEgo(printed(THREAD)), redirect)
    [(_, _, headers)] = redirect.calls
    assert [k.lower() for k in headers] == ["user-agent"]


@pytest.mark.parametrize("status, location", [
    (403, ""),                                               # blocked: no redirect at all
    (301, "abcd12"),                                         # a bare ID is user input, not a redirect target
    (301, "https://www.reddit.com/r/claude/"),               # redirects, but not to a post
    (200, SHARED),                                           # a Location on a non-redirect is not trusted
])
def test_share_link_that_leads_to_no_post_fails_before_ego(rsrc, status, location):
    fake = FakeEgo()
    code, out = rsrc(["read", "https://www.reddit.com/r/claude/s/AbCdEf123"], fake, FakeRedirect(status, location))
    assert code == 1 and out["error"]["code"] == "upstream_error" and fake.calls == []
    assert f"HTTP {status}" in out["error"]["message"] and "/comments/ URL" in out["error"]["message"]
    assert [(a["backend"], a["outcome"]) for a in out["meta"]["attempts"]] == [("reddit-redirect", "upstream_error")]


def test_share_link_redirect_may_be_relative(rsrc):
    fake, redirect = FakeEgo(printed(THREAD)), FakeRedirect(302, "/r/claude/comments/1xootop/i_thought/")
    code, out = rsrc(["read", "https://www.reddit.com/r/claude/s/AbCdEf123"], fake, redirect)
    assert code == 0 and fake.input()["url"] == "https://www.reddit.com/comments/1xootop/?sort=top"


@pytest.mark.parametrize("status, code_", [(429, "rate_limited"), (404, "not_found")])
def test_share_link_rate_limit_and_missing_link_have_their_own_codes(rsrc, status, code_):
    fake = FakeEgo()
    code, out = rsrc(["read", "https://www.reddit.com/r/claude/s/AbCdEf123"], fake, FakeRedirect(status))
    assert code == 1 and out["error"]["code"] == code_ and fake.calls == []
    assert "browser" not in out["error"]["message"]  # nothing sends the agent on to another Reddit request
    assert [(a["backend"], a["outcome"]) for a in out["meta"]["attempts"]] == [("reddit-redirect", code_)]


def test_share_link_without_a_response_is_a_network_error(rsrc):
    fake = FakeEgo()
    code, out = rsrc(["read", "https://www.reddit.com/r/claude/s/AbCdEf123"], fake, FakeRedirect(OSError("down")))
    assert code == 1 and out["error"]["code"] == "network_error" and fake.calls == []


def test_share_link_to_another_post_than_the_page_shows_is_rejected(rsrc):
    redirect = FakeRedirect(301, "https://www.reddit.com/r/claude/comments/abc123/other/")
    code, out = rsrc(["read", "https://www.reddit.com/r/claude/s/AbCdEf123"], FakeEgo(printed(THREAD)), redirect)
    assert code == 1 and out["error"]["code"] == "url_mismatch"


def test_read_rejects_a_different_post(rsrc):
    code, out = rsrc(["read", "abc123"], FakeEgo(printed(THREAD)))
    assert code == 1 and out["error"]["code"] == "url_mismatch" and out["items"] == []
    assert out["meta"]["rejected"] == [{"url": "https://www.reddit.com" + THREAD["post"]["attrs"]["permalink"],
                                        "reason": "url_mismatch"}]
    assert "t3_abc123" in out["error"]["message"] and "t3_1xootop" in out["error"]["message"]


@pytest.mark.parametrize("fields, code_", [
    ({"title": "Reddit - Page not found", "page_text": "Page not found ..."}, "not_found"),
    ({"page_text": BLOCK_TEXT}, "blocked"),
    ({"page_text": "whoa there, pardner!"}, "rate_limited"),
    ({"title": "Reddit", "page_text": "Home"}, "upstream_error"),
])
def test_read_page_without_post_is_classified(rsrc, fields, code_):
    code, out = rsrc(["read", "1xootop"], FakeEgo(printed(page(**fields))))
    assert code == 1 and out["error"]["code"] == code_ and out["items"] == []


@pytest.mark.parametrize("state,code_,message", [
    ("private", "blocked", "r/CenturyClub is private"),
    ("banned", "blocked", "r/The_Donald is banned"),
    ("login", "blocked", "requires login"),
    ("missing", "not_found", "no such post"),
])
def test_recorded_thread_walls(rsrc, state, code_, message):
    code, out = rsrc(["read", "ib3lzb"], FakeEgo(printed(page(**READ_STATES[state]))))
    assert code == 1 and not out["ok"] and out["items"] == []
    assert out["error"]["code"] == code_ and message in out["error"]["message"]


def test_recorded_network_block_keeps_its_diagnosis(rsrc):
    html = (FIXTURES / "block_reddit.html").read_text()
    code, out = rsrc(["read", "ib3lzb"], FakeEgo(printed(page(page_text=html))))
    assert code == 1 and not out["ok"] and out["error"]["code"] == "blocked"


def test_removed_post_keeps_the_visible_record_and_removal_notice(rsrc):
    code, out = rsrc(["read", "ib3lzb"], FakeEgo(printed(page(**READ_STATES["removed"]))))
    assert code == 0 and out["items"][0]["id"] == "t3_ib3lzb"
    assert out["items"][0]["text"] is None
    assert out["meta"]["notes"] == ["Sorry, this post has been removed by the moderators of r/science."]


# ego-browser failures

@pytest.mark.parametrize("response, code_, exit_", [
    (proc.Missing("ego-browser not found"), "config", 2),
    (proc.Timeout("slow"), "timeout", 1),
    (proc.Result(1, "", "Error: EGO_TASK_SPACE_NAME_IN_USE something\n"), "upstream_error", 1),
    (proc.Result(0, "hello\n", ""), "upstream_error", 1),
    (proc.Result(0, reddit.MARKER + "{not json\n", ""), "upstream_error", 1),
])
def test_ego_failures(rsrc, response, code_, exit_):
    code, out = rsrc(["search", "q"], FakeEgo(response))
    assert code == exit_ and out["error"]["code"] == code_
    assert out["meta"]["attempts"][0]["outcome"] == code_ and out["meta"]["attempts"][0]["backend"] == "ego-browser"


def test_nonzero_exit_reports_stderr_tail(rsrc):
    code, out = rsrc(["read", "1xootop"], FakeEgo(proc.Result(1, "", "Error: EGO_PAGE_UNRESPONSIVE on p1\n")))
    assert "EGO_PAGE_UNRESPONSIVE" in out["error"]["message"]


def test_notice_lines_before_the_result_are_ignored(rsrc):
    code, out = rsrc(["search", "q"], FakeEgo(printed(SEARCH, before="[ego-browser:notice] update available\n")))
    assert code == 0 and len(out["items"]) == 7


def test_requests_count_page_loads(rsrc):
    code, out = rsrc(["search", "q"], FakeEgo(printed({**SEARCH, "loads": 3})))
    assert out["meta"]["attempts"][0]["requests"] == 3


def test_each_run_gets_its_own_task_space(rsrc):
    fake = FakeEgo(printed(SEARCH), printed(SEARCH))
    rsrc(["search", "q"], fake)
    rsrc(["search", "q"], fake)
    first, second = fake.input(0)["space"], fake.input(1)["space"]
    assert first.startswith("rsrc reddit ") and first != second


def test_a_killed_batch_finishes_its_own_space(rsrc):
    fake = FakeEgo(printed(SEARCH), proc.Timeout("slow"), proc.Result(0, "", ""))
    code, out = rsrc(["search", "q", "--with-comments", "1"], fake)
    assert out["error"]["code"] == "timeout"
    assert fake.input(2) == {"space": fake.input(1)["space"]}
    assert fake.calls[2][0][3].endswith(reddit.CLEANUP.read_text())


def best_thread(post_id=THREAD["post"]["attrs"]["id"]):
    return {**THREAD, "post": {**THREAD["post"], "attrs": {**THREAD["post"]["attrs"], "id": post_id}},
            "comments": [{**c, "attrs": {**c["attrs"], "postid": post_id,
                                      "permalink": c["attrs"]["permalink"].replace("1xootop", post_id[3:])}} for c in THREAD["comments"]],
            "comment_sort": "Sort by Best"}


def batch_pages(*pages):
    rows = [{"page": p, "ms": 1} if p is not None else {"cancelled": True} for p in pages]
    return printed({"rows": rows, "loads": len(rows)})


@pytest.mark.parametrize("n", [0, -1, 6])
def test_comment_thread_limit_fails_before_search(rsrc, n):
    fake = FakeEgo()
    code, out = rsrc(["search", "test", "--with-comments", str(n)], fake)
    assert code == 2 and not fake.calls


def test_search_groups_best_comments_and_keeps_other_threads_on_failure(rsrc):
    first, second, third = [r["context"]["post"]["id"] for r in SEARCH["posts"][:3]]
    fake = FakeEgo(printed(SEARCH), batch_pages(best_thread(), page(**READ_STATES["missing"]), best_thread(third)))
    code, out = rsrc(["search", "q", "--with-comments"], fake)
    assert code == 0 and out["error"] is None and len(fake.calls) == 2
    assert fake.input(1)["urls"][0].endswith("?sort=confidence")
    posts = [i for i in out["items"] if i["raw"]["kind"] == "post"]
    assert len(posts) == 7 and [i["id"] for i in posts[:3]] == [first, second, third]
    assert out["meta"]["failed"] == [{"input": posts[1]["url"], "code": "not_found",
                                      "message": "no such post at https://www.reddit.com/x"}]
    roots = [c for c in THREAD["comments"] if c["attrs"]["depth"] == "0"][:3]
    for i, c in zip(out["items"][1:1 + len(roots)], roots, strict=True):
        assert i["id"] == c["attrs"]["thingid"] and i["text"] == c["text"]
        assert i["raw"]["kind"] == "comment" and i["raw"]["attrs"]["postid"] == first
    assert sum(a["requests"] for a in out["meta"]["attempts"]) == 4


@pytest.mark.parametrize("failure,code_", [(page(page_text=BLOCK_TEXT), "blocked"),
                                          (page(status=429), "rate_limited")])
def test_block_cancels_pending_threads_and_preserves_all_posts(rsrc, failure, code_):
    fake = FakeEgo(printed(SEARCH), batch_pages(best_thread(), failure, None, None, None))
    code, out = rsrc(["search", "q", "--with-comments", "5"], fake)
    assert code == 1 and out["error"]["code"] == code_ and len(fake.calls) == 2
    assert "5 thread pages had already been requested" in out["meta"]["notes"][-1]
    assert sum(a["requests"] for a in out["meta"]["attempts"]) == 6
    assert [i["id"] for i in out["items"] if i["raw"]["kind"] == "post"] == [
        r["context"]["post"]["id"] for r in SEARCH["posts"]]
    assert out["items"][1]["raw"]["kind"] == "comment"
    assert len(out["meta"]["failed"]) == 1 and out["meta"]["failed"][0]["code"] == code_


def test_search_rejects_unconfirmed_sort_and_wrong_parent(rsrc):
    thread = best_thread()
    thread["comments"][0]["attrs"]["postid"] = "t3_other"
    for bad, reason in [(thread, "url_mismatch"), ({**thread, "comment_sort": "Sort by New"}, "upstream_error"),
                        (READ_STATES["removed"], "url_mismatch")]:
        code, out = rsrc(["search", "q", "--with-comments", "1"], FakeEgo(printed(SEARCH), batch_pages(bad)))
        assert code == 1 and out["error"]["code"] == reason and len(out["items"]) == 7


def test_blocked_search_pagination_does_not_start_thread_reads(rsrc):
    fake = FakeEgo(printed({**SEARCH, "status": 429}))
    code, out = rsrc(["search", "q", "--with-comments"], fake)
    assert code == 1 and len(fake.calls) == 1 and len(out["items"]) == 7
    assert out["error"]["code"] == "rate_limited"


def test_no_comments_is_success_and_removed_thread_is_named(rsrc):
    thread = best_thread()
    thread["comments"] = []
    thread["post"]["attrs"]["comment-count"] = "0"
    code, out = rsrc(["search", "q", "--with-comments", "1"], FakeEgo(printed(SEARCH), batch_pages(thread)))
    assert code == 0 and out["meta"]["failed"] == []
    thread["post"]["removal_notice"] = READ_STATES["removed"]["post"]["removal_notice"]
    code, out = rsrc(["search", "q", "--with-comments", "1"], FakeEgo(printed(SEARCH), batch_pages(thread)))
    assert code == 1 and out["meta"]["failed"][0]["code"] == "not_found"


def test_read_best_uses_reddits_confidence_sort(rsrc):
    fake = FakeEgo(printed(THREAD))
    code, out = rsrc(["read", "1xootop", "--sort", "best"], fake)
    assert code == 0 and out["input"]["sort"] == "best"
    assert fake.input()["url"].endswith("?sort=confidence")


def test_recorded_best_order_keeps_scores_authors_and_links():
    thread = json.loads((FIXTURES / "reddit_best_comments.json").read_text())
    post_id = thread["post"]["attrs"]["id"]
    items, error = reddit.best_comments(thread, post_id)
    assert error is None and len(items) == 3
    for item, row in zip(items, thread["comments"], strict=True):
        attrs = row["attrs"]
        assert (item["id"], item["author"], item["engagement"]["score"]) == (
            attrs["thingid"], attrs["author"], int(attrs["score"]))
        assert item["url"] == reddit.BASE + attrs["permalink"] and item["published_at"]
        assert item["text"] == row["text"] and item["raw"]["attrs"]["postid"] == post_id


def test_search_replaced_by_login_keeps_posts_and_stops(rsrc):
    fake = FakeEgo(printed({**SEARCH, "lost_results": True, "page_text": "Log in to continue"}))
    code, out = rsrc(["search", "q", "--with-comments"], fake)
    assert code == 1 and out["error"]["code"] == "blocked" and len(out["items"]) == 7
    assert len(fake.calls) == 1


def test_crossed_batch_results_never_attach_a_comment_to_another_thread(rsrc):
    second = SEARCH["posts"][1]["context"]["post"]["id"]
    fake = FakeEgo(printed(SEARCH), batch_pages(best_thread(second), best_thread()))
    code, out = rsrc(["search", "q", "--with-comments", "2"], fake)
    assert code == 1 and [f["code"] for f in out["meta"]["failed"]] == ["url_mismatch"] * 2
    assert all(i["raw"]["kind"] == "post" for i in out["items"])


@pytest.mark.parametrize("shortfall,reason", [
    ("complete", "all comments accounted for"),
    ("deadline", "completeness unconfirmed at the 15 s deadline"),
])
def test_short_groups_keep_their_comments_and_name_the_reason(rsrc, shortfall, reason):
    thread = json.loads((FIXTURES / "reddit_best_comments.json").read_text())
    record = dict(SEARCH["posts"][0])
    record["context"] = {"post": {"id": thread["post"]["attrs"]["id"]}}
    record["href"] = "/comments/1y0zet7/"
    thread["comments"] = thread["comments"][:2]
    thread["shortfall"] = shortfall
    fake = FakeEgo(printed({**SEARCH, "posts": [record]}), batch_pages(thread))
    code, out = rsrc(["search", "q", "--limit", "1", "--with-comments"], fake)
    assert code == 0 and len(out["items"]) == 3
    assert out["meta"]["notes"] == [f"t3_1y0zet7: 2 of 3 readable top-level comments ({reason})"]


@pytest.mark.parametrize("remaining", [0, 2, 3])
def test_pin_omission_and_shortfall_notes_belong_to_the_thread(rsrc, remaining):
    thread = json.loads((FIXTURES / "reddit_pinned_comments.json").read_text())
    post_id = thread["post"]["attrs"]["id"]
    post = {**SEARCH["posts"][0], "context": {"post": {"id": post_id}}, "href": "/comments/1xzcsdg/"}
    thread.update(comments=thread["comments"][1:1 + remaining], skipped_pinned=1)
    if remaining < 3:
        thread["shortfall"] = "complete"
    fake = FakeEgo(printed({**SEARCH, "posts": [post]}), batch_pages(thread))
    code, out = rsrc(["search", "q", "--limit", "1", "--with-comments"], fake)
    assert code == 0 and len(out["items"]) == remaining + 1
    notes = [f"{post_id}: skipped 1 pinned comment"]
    if remaining < 3:
        notes.append(f"{post_id}: {remaining} of 3 readable top-level comments (all comments accounted for)")
    assert out["meta"]["notes"] == notes
