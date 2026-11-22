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
# Reduced archived pages (Wayback timestamps): CenturyClub 20260103115225,
# The_Donald 20250518184233, /login/ 20241104000710, and ib3lzb 20230617202108.
# The missing-page heading was recorded in the signed-in browser.
READ_STATES = json.loads((FIXTURES / "reddit_read_states.json").read_text())
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
    code, out = rsrc(["read", "1xootop", "--comments", "0"], FakeEgo(printed(THREAD)))
    assert [i["id"] for i in out["items"]] == ["t3_1xootop"]


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


def best_thread(post_id=THREAD["post"]["attrs"]["id"]):
    # The thread fixture supplies recorded comment bodies and parent/depth relationships.
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
    # Signed-in Best page for the first "mechanical keyboards" search hit.
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
