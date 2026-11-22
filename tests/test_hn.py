"""`rsrc hn` through rsrc.cli.main with a fake transport and the recorded Algolia answers."""

import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from rsrc import cli
from rsrc.channels import hn
from rsrc.envelope import ITEM_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
SEARCH = (FIXTURES / "hn_search.json").read_bytes()
ITEM = (FIXTURES / "hn_item.json").read_bytes()  # story 50148855: 128 comments, up to 7 deep


def hit(object_id: str) -> dict:
    [found] = [h for h in json.loads(SEARCH)["hits"] if h["objectID"] == object_id]
    return found


def depth_first(node: dict, depth: int = 0):
    """(id, depth) for a node and everything below it, children in the API's order."""
    yield str(node["id"]), depth
    for child in node["children"]:
        yield from depth_first(child, depth + 1)


def ids_and_depths(out: dict) -> list[tuple]:
    return [(i["id"], i["raw"]["depth"]) for i in out["items"]]


def answer(data) -> tuple[int, bytes]:
    return 200, json.dumps(data).encode()


class FakeTransport:
    """Answers requests in order from canned (status, body) pairs or exceptions; records each request."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[tuple] = []

    def __call__(self, method, url, headers, body):
        self.requests.append((method, url, headers, body))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    @property
    def urls(self):
        return [url for _, url, _, _ in self.requests]


@pytest.fixture
def rsrc(monkeypatch, capsys):
    monkeypatch.setattr(cli, "CHANNELS", [hn])

    def run(argv, transport):
        code = cli.main(["hn", *argv], cli.Context(environ={}, transport=transport, sleep=lambda s: None))
        out = capsys.readouterr().out
        assert len(out.splitlines()) == 1
        return code, json.loads(out)

    return run


def item_by_id(out: dict, item_id: str) -> dict:
    [found] = [i for i in out["items"] if i["id"] == item_id]
    return found


# text conversion

def test_p_starts_a_new_paragraph():
    assert hn.plain_text("First.<p>Second.<p>Third.") == "First.\n\nSecond.\n\nThird."


def test_a_link_becomes_its_href_not_the_shortened_visible_text():
    html = hit("50300683")["story_text"]
    assert html.endswith("security-notic...</a>")  # Hacker News cut the visible text
    assert hn.plain_text(html) == "https://cursor.com/blog/claude-code-extension-security-notice"


def test_a_link_inside_a_sentence_keeps_the_words_around_it():
    html = 'See <a href="https:&#x2F;&#x2F;example.com&#x2F;a?b=1&amp;c=2" rel="nofollow">example.com&#x2F;a...</a> first.'
    assert hn.plain_text(html) == "See https://example.com/a?b=1&c=2 first."


def test_other_tags_are_dropped_and_their_content_kept():
    html = "It is <i>really</i> fast:<p><pre><code>  x = 1\n  y = 2\n</code></pre>"
    assert hn.plain_text(html) == "It is really fast:\n\n  x = 1\n  y = 2"


def test_entities_are_decoded():
    assert hn.plain_text("I haven&#x27;t &quot;tried&quot; a&#x2F;b &amp; c &lt;d&gt;") == 'I haven\'t "tried" a/b & c <d>'


@pytest.mark.parametrize("html", [None, "", "  \n", "<p>"])
def test_empty_or_missing_text_is_none(html):
    assert hn.plain_text(html) is None


def test_a_link_without_href_keeps_its_text():
    assert hn.plain_text('See <a name="top">the anchor</a> here.') == "See the anchor here."


def test_an_unclosed_link_does_not_swallow_the_next_paragraph():
    html = 'See <a href="https://example.com/x">example.com/x and more<p>Second paragraph.'
    assert hn.plain_text(html) == "See https://example.com/x\n\nSecond paragraph."


def test_paragraphs_lose_edge_newlines_and_trailing_spaces_but_keep_indentation():
    assert hn.plain_text("First. <p>\n  x = 1  \n<p>Last.") == "First.\n\n  x = 1\n\nLast."


def test_hn_is_reachable_through_the_real_channel_list(capsys):
    code = cli.main(["hn", "search", ""], cli.Context(environ={}))  # usage: no request is made
    out = json.loads(capsys.readouterr().out)

    assert code == 2 and (out["channel"], out["action"]) == ("hn", "search")
    assert "QUERY" in out["error"]["message"]


# search

def test_search_makes_one_keyless_get_for_stories(rsrc):
    transport = FakeTransport((200, SEARCH))
    code, out = rsrc(["search", "Claude Code"], transport)

    assert code == 0 and out["ok"] and out["error"] is None
    [(method, url, headers, body)] = transport.requests
    assert method == "GET" and not headers and body is None
    assert url.startswith("https://hn.algolia.com/api/v1/search?")
    assert parse_qs(urlsplit(url).query) == {"query": ["Claude Code"], "tags": ["story"], "hitsPerPage": ["30"]}


def test_search_envelope_names_the_channel_backend_and_one_attempt(rsrc):
    code, out = rsrc(["search", "Claude Code", "--since", "2026-10-15", "--limit", "5"], FakeTransport((200, SEARCH)))

    assert (out["channel"], out["action"], out["backend"]) == ("hn", "search", "algolia")
    assert out["input"] == {"query": "Claude Code", "since": "2026-10-15", "until": None,
                            "sort": "relevance", "limit": 5}
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("algolia", "ok", 1)


def test_search_sort_date_uses_the_by_date_endpoint(rsrc):
    transport = FakeTransport((200, SEARCH))
    rsrc(["search", "Claude Code", "--sort", "date", "--limit", "7"], transport)

    [url] = transport.urls
    assert urlsplit(url).path == "/api/v1/search_by_date"
    assert parse_qs(urlsplit(url).query)["hitsPerPage"] == ["7"]


@pytest.mark.parametrize("flags, filters", [
    (["--since", "2026-10-15", "--until", "2026-11-15"], "created_at_i>=1792022400,created_at_i<1794700800"),
    (["--since", "2026-10-15"], "created_at_i>=1792022400"),
    (["--until", "2026-11-15"], "created_at_i<1794700800"),
])
def test_search_dates_become_utc_midnight_bounds(rsrc, flags, filters):
    transport = FakeTransport((200, SEARCH))
    rsrc(["search", "Claude Code", *flags], transport)

    [url] = transport.urls
    assert parse_qs(urlsplit(url).query)["numericFilters"] == [filters]


def test_search_maps_each_hit_in_algolias_order(rsrc):
    code, out = rsrc(["search", "Claude Code"], FakeTransport((200, SEARCH)))

    hits = json.loads(SEARCH)["hits"]
    assert len(hits) == 30 and [i["id"] for i in out["items"]] == [h["objectID"] for h in hits]
    assert all(set(i) == set(ITEM_KEYS) and i["source"] == "hackernews" for i in out["items"])
    assert [i["raw"] for i in out["items"]] == hits

    first = out["items"][0]
    assert first["id"] == "50385272"
    assert first["url"] == "https://news.ycombinator.com/item?id=50385272"  # the discussion, not the article
    assert first["raw"]["url"] == "https://github.com/anthropics/claude-code/issues/107757"
    assert first["title"] == "Claude Code sends your email address to websites"
    assert first["author"] == "motoroco"
    assert first["published_at"] == "2026-11-05T22:17:15Z"
    assert first["engagement"] == {"points": 1616, "comments": 535}
    assert first["text"] is None


def test_search_text_is_the_story_text_as_plain_text(rsrc):
    code, out = rsrc(["search", "Claude Code"], FakeTransport((200, SEARCH)))

    assert item_by_id(out, "50286306")["text"] == (
        "The prompt issue:\n\nhttps://github.com/anthropics/claude-code/issues/81138\n\n"
        "The LLM sends it to websites:\n\n"
        "https://www.reddit.com/r/ClaudeCode/comments/1xhqvzn/opus_55_just_shared_my_email_address/")
    bodiless = hit("50480909")  # a Tell HN post with a title only
    assert "story_text" not in bodiless and "url" not in bodiless
    assert item_by_id(out, "50480909")["text"] is None


def test_search_leaves_out_a_count_the_hit_lacks(rsrc):
    data = json.loads(SEARCH)
    del data["hits"][0]["points"]
    data["hits"][1]["num_comments"] = None
    code, out = rsrc(["search", "Claude Code"], FakeTransport(answer(data)))

    assert out["items"][0]["engagement"] == {"comments": 535}
    assert out["items"][1]["engagement"] == {"points": 1014}


def test_search_without_hits_exits_66(rsrc):
    data = {**json.loads(SEARCH), "hits": []}
    code, out = rsrc(["search", "zzzzqqqq"], FakeTransport(answer(data)))

    assert code == 66 and out["ok"] and out["items"] == [] and out["error"] is None
    assert out["meta"]["attempts"][0]["outcome"] == "no_results"


@pytest.mark.parametrize("argv, words", [
    (["search", ""], "QUERY"),
    (["search", "   "], "QUERY"),
    (["search", "q", "--limit", "0"], "--limit"),
    (["search", "q", "--limit", "101"], "--limit"),
    (["search", "q", "--since", "yesterday"], "YYYY-MM-DD"),
    (["search", "q", "--until", "2026-13-45"], "YYYY-MM-DD"),
    (["search", "q", "--since", "2026-11-15", "--until", "2026-11-15"], "--until"),
    (["search", "q", "--since", "2026-11-15", "--until", "2026-10-15"], "--until"),
    (["search", "q", "--sort", "points"], "--sort"),
])
def test_search_usage_errors_make_no_request(rsrc, argv, words):
    transport = FakeTransport()
    code, out = rsrc(argv, transport)

    assert code == 2 and not out["ok"] and out["items"] == []
    assert (out["channel"], out["action"]) == ("hn", "search")
    assert out["error"]["code"] == "usage" and words in out["error"]["message"]
    assert transport.requests == []


def assert_failed(out: dict, code: str) -> None:
    """One failed envelope: the code on the error and on the single one-request attempt."""
    assert not out["ok"] and out["items"] == [] and out["error"]["code"] == code
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("algolia", code, 1)


def test_search_rate_limit_stops_without_another_request(rsrc):
    transport = FakeTransport((429, b"Too Many Requests"), (200, SEARCH))
    code, out = rsrc(["search", "Claude Code"], transport)

    assert code == 1
    assert_failed(out, "rate_limited")
    assert len(transport.requests) == 1


@pytest.mark.parametrize("limit", ["1", "100"])
def test_search_accepts_the_limit_bounds(rsrc, limit):
    transport = FakeTransport((200, SEARCH))
    code, out = rsrc(["search", "Claude Code", "--limit", limit], transport)

    assert code == 0
    assert parse_qs(urlsplit(transport.urls[0]).query)["hitsPerPage"] == [limit]


@pytest.mark.parametrize("created_at_i", ["1731520931", True, None, 10**18, -10**12])
def test_search_time_that_is_not_a_usable_integer_is_null(rsrc, created_at_i):
    data = json.loads(SEARCH)
    data["hits"][0]["created_at_i"] = created_at_i
    code, out = rsrc(["search", "Claude Code"], FakeTransport(answer(data)))

    assert code == 0 and len(out["items"]) == 30
    assert out["items"][0]["published_at"] is None


def test_search_leaves_out_a_count_that_is_not_an_integer(rsrc):
    data = json.loads(SEARCH)
    data["hits"][0].update(points=True, num_comments="535")
    code, out = rsrc(["search", "Claude Code"], FakeTransport(answer(data)))

    assert code == 0 and out["items"][0]["engagement"] is None


@pytest.mark.parametrize("failure", [ConnectionRefusedError("refused"), TimeoutError("timed out")])
def test_search_without_a_response_is_network_error(rsrc, failure):
    code, out = rsrc(["search", "Claude Code"], FakeTransport(failure))

    assert code == 1
    assert_failed(out, "network_error")


@pytest.mark.parametrize("status, body", [
    (500, b"Internal Server Error"),
    (503, b""),
    (400, b'{"message": "invalid numericFilters"}'),
    (404, b'{"message": "not found"}'),  # only `read` has a missing item to report
    (200, b"\xff\xfe"),
    (200, b'{"message": "no hits key"}'),
    (200, b'["not", "an", "object"]'),
    (200, b'{"hits": {"not": "a list"}}'),
    (200, b'{"hits": [{"title": "a hit without objectID"}]}'),
    (200, b'{"hits": [{"objectID": null}]}'),
    (200, b'{"hits": [{"objectID": "1", "story_text": 5}]}'),
])
def test_search_bad_answers_are_upstream_error_naming_the_status(rsrc, status, body):
    code, out = rsrc(["search", "Claude Code"], FakeTransport((status, body)))

    assert code == 1
    assert_failed(out, "upstream_error")
    assert f"HTTP {status}" in out["error"]["message"]


# read

@pytest.mark.parametrize("given", [
    "50148855",
    "https://news.ycombinator.com/item?id=50148855",
    "https://news.ycombinator.com/item?id=50148855&p=2",
    "https://news.ycombinator.com/item?p=2&id=50148855#50149524",
    "https://News.YCombinator.com/item?id=50148855",
    " 50148855\n",
])
def test_read_makes_one_keyless_get_for_a_url_or_an_id(rsrc, given):
    transport = FakeTransport((200, ITEM))
    code, out = rsrc(["read", given], transport)

    assert code == 0 and out["ok"] and out["error"] is None
    assert transport.requests == [("GET", "https://hn.algolia.com/api/v1/items/50148855", None, None)]
    assert (out["channel"], out["action"], out["backend"]) == ("hn", "read", "algolia")
    assert out["input"] == {"item": given, "comments": 50}
    [attempt] = out["meta"]["attempts"]
    assert (attempt["backend"], attempt["outcome"], attempt["requests"]) == ("algolia", "ok", 1)


def test_read_returns_the_story_first(rsrc):
    code, out = rsrc(["read", "50148855"], FakeTransport((200, ITEM)))

    story = out["items"][0]
    assert set(story) == set(ITEM_KEYS)
    assert story["id"] == "50148855"
    assert story["url"] == "https://news.ycombinator.com/item?id=50148855"
    assert story["title"] == "Tmux and Claude Code: Sessions, Send-Keys, and the Ceiling"
    assert story["author"] == "ankitg12"
    assert story["published_at"] == "2026-10-19T05:55:31Z"
    assert story["text"] is None  # a link post
    assert story["source"] == "hackernews"
    assert story["engagement"] == {"points": 174}
    assert story["raw"]["depth"] == 0
    assert story["raw"]["item"]["url"] == "https://crystl.dev/guides/tmux-for-ai-agents/"
    assert story["raw"]["item"]["parent_id"] is None and story["raw"]["item"]["story_id"] == 50148855


def test_read_lists_comments_depth_first_in_the_apis_order(rsrc):
    code, out = rsrc(["read", "50148855", "--comments", "200"], FakeTransport((200, ITEM)))

    assert ids_and_depths(out)[:9] == [
        ("50148855", 0), ("50149524", 1), ("50149676", 2), ("50153167", 3), ("50149686", 2),
        ("50151883", 2), ("50171449", 3), ("50153465", 2), ("50149933", 1)]
    assert ids_and_depths(out) == list(depth_first(json.loads(ITEM)))
    assert len(out["items"]) == 129 and ("50151773", 7) in ids_and_depths(out)
    assert out["meta"]["notes"] == []


def test_read_maps_a_comment(rsrc):
    code, out = rsrc(["read", "50148855", "--comments", "200"], FakeTransport((200, ITEM)))

    assert all(set(i) == set(ITEM_KEYS) and i["source"] == "hackernews" for i in out["items"])
    comment = item_by_id(out, "50150539")
    assert comment["url"] == "https://news.ycombinator.com/item?id=50150539"
    assert comment["title"] is None and comment["engagement"] is None
    assert comment["author"] == "nklmilojevic"
    assert comment["published_at"] == "2026-10-19T10:14:08Z"
    assert comment["text"] == (
        "I've made a terminal that is essentially ghostty + herder, but all natively and has a session "
        "persistence. https://thurm.rs\n\nthere is an ios application in testflight and web client, nothing "
        "is on the servers, using tailscale through clients or you can use classic remote hosts.")
    assert "<p>" in comment["raw"]["item"]["text"]  # raw keeps the HTML
    assert comment["raw"]["item"]["parent_id"] == 50148855 and comment["raw"]["item"]["story_id"] == 50148855


def test_read_raw_item_lists_direct_child_ids_instead_of_the_subtree(rsrc):
    code, out = rsrc(["read", "50148855", "--comments", "200"], FakeTransport((200, ITEM)))

    tree = json.loads(ITEM)
    assert out["items"][0]["raw"]["item"] == {**tree, "children": [c["id"] for c in tree["children"]]}
    assert len(out["items"][0]["raw"]["item"]["children"]) == 46
    first = tree["children"][0]
    assert item_by_id(out, "50149524")["raw"]["item"] == {
        **first, "children": [50149676, 50149686, 50151883, 50153465]}
    assert item_by_id(out, "50153167")["raw"]["item"]["children"] == []


@pytest.mark.parametrize("flags, last_id, note", [
    ([], "50151125", "50 of 128 comments returned"),
    (["--comments", "3"], "50153167", "3 of 128 comments returned"),
    (["--comments", "127"], "50175712", "127 of 128 comments returned"),
])
def test_read_cuts_the_comments_and_says_so(rsrc, flags, last_id, note):
    code, out = rsrc(["read", "50148855", *flags], FakeTransport((200, ITEM)))

    expected = list(depth_first(json.loads(ITEM)))[:int(note.split()[0]) + 1]
    assert code == 0 and ids_and_depths(out) == expected
    assert out["items"][-1]["id"] == last_id
    assert out["meta"]["notes"] == [note]


@pytest.mark.parametrize("comments", ["128", "200"])
def test_read_with_no_more_comments_than_asked_has_no_note(rsrc, comments):
    code, out = rsrc(["read", "50148855", "--comments", comments], FakeTransport((200, ITEM)))

    assert code == 0 and len(out["items"]) == 129 and out["meta"]["notes"] == []


def test_read_comments_0_returns_the_story_alone(rsrc):
    code, out = rsrc(["read", "50148855", "--comments", "0"], FakeTransport((200, ITEM)))

    assert code == 0 and [i["id"] for i in out["items"]] == ["50148855"]
    assert out["meta"]["notes"] == ["0 of 128 comments returned"]


def test_read_skips_a_comment_without_text_and_keeps_its_replies(rsrc):
    tree = json.loads(ITEM)
    deleted = tree["children"][0]  # 50149524, with six comments below it
    deleted["text"] = None
    deleted["author"] = None
    deleted["children"][0]["text"] = ""  # 50149676, the parent of 50153167
    code, out = rsrc(["read", "50148855", "--comments", "3"], FakeTransport(answer(tree)))

    assert ids_and_depths(out) == [("50148855", 0), ("50153167", 3), ("50149686", 2), ("50151883", 2)]
    assert out["meta"]["notes"] == ["3 of 126 comments returned"]  # the two skipped are not counted
    assert out["items"][0]["raw"]["item"]["children"][0] == 50149524  # raw still names the skipped child


def test_read_accepts_a_comment_id(rsrc):
    comment = json.loads(ITEM)["children"][0]  # what the API answers for /items/50149524
    transport = FakeTransport(answer(comment))
    code, out = rsrc(["read", "https://news.ycombinator.com/item?id=50149524"], transport)

    assert code == 0 and transport.urls == ["https://hn.algolia.com/api/v1/items/50149524"]
    assert ids_and_depths(out) == [("50149524", 0), ("50149676", 1), ("50153167", 2), ("50149686", 1),
                                   ("50151883", 1), ("50171449", 2), ("50153465", 1)]
    requested = out["items"][0]
    assert requested["title"] is None and requested["engagement"] is None
    assert requested["text"].startswith("I really liked Tmux and Zellij")
    assert requested["raw"]["item"]["parent_id"] == 50148855


@pytest.mark.parametrize("argv, words", [
    (["read", ""], "numeric ID"),
    (["read", "claude code"], "numeric ID"),
    (["read", "12abc"], "numeric ID"),
    (["read", "50148855/"], "numeric ID"),
    (["read", "https://example.com/item?id=50148855"], "numeric ID"),
    (["read", "https://news.ycombinator.com.example.com/item?id=50148855"], "numeric ID"),
    (["read", "https://news.ycombinator.com/user?id=50148855"], "numeric ID"),
    (["read", "https://news.ycombinator.com/item"], "numeric ID"),
    (["read", "https://news.ycombinator.com/item?id=abc"], "numeric ID"),
    (["read", "https://news.ycombinator.com/item?id=1&id=2"], "numeric ID"),
    (["read", "https://[news.ycombinator.com/item?id=1"], "numeric ID"),
    (["read", "http://news.ycombinator.com/item?id=50148855"], "numeric ID"),
    (["read", "https://news.ycombinator.com/item?id=&id=50148855"], "numeric ID"),
    (["read", "https://news.ycombinator.com/item?id=5014\n8855"], "numeric ID"),
    (["read", "1234567890123456"], "numeric ID"),
    (["read", "50148855", "--comments", "-1"], "--comments"),
    (["read", "50148855", "--comments", "201"], "--comments"),
    (["read", "50148855", "--comments", "many"], "--comments"),
])
def test_read_usage_errors_make_no_request(rsrc, argv, words):
    transport = FakeTransport()
    code, out = rsrc(argv, transport)

    assert code == 2 and not out["ok"] and out["items"] == []
    assert (out["channel"], out["action"]) == ("hn", "read")
    assert out["error"]["code"] == "usage" and words in out["error"]["message"]
    assert transport.requests == []


def test_read_asks_for_the_id_without_leading_zeros(rsrc):
    transport = FakeTransport((200, ITEM))
    code, out = rsrc(["read", "0050148855"], transport)

    assert code == 0 and transport.urls == ["https://hn.algolia.com/api/v1/items/50148855"]


def test_read_missing_item_is_not_found(rsrc):
    code, out = rsrc(["read", "99999999999"], FakeTransport((404, b'{"status": 404, "error": "Not Found"}')))

    assert code == 1
    assert_failed(out, "not_found")
    assert "99999999999" in out["error"]["message"]


def test_read_of_a_different_item_than_asked_for_is_url_mismatch(rsrc):
    code, out = rsrc(["read", "50148856"], FakeTransport((200, ITEM)))  # the answer is item 50148855

    assert code == 1
    assert_failed(out, "url_mismatch")
    assert "50148856" in out["error"]["message"] and "50148855" in out["error"]["message"]
    assert out["meta"]["rejected"] == [{"url": "https://news.ycombinator.com/item?id=50148855",
                                        "reason": "url_mismatch"}]


def test_read_rate_limit_stops_without_another_request(rsrc):
    transport = FakeTransport((429, b"Too Many Requests"), (200, ITEM))
    code, out = rsrc(["read", "50148855"], transport)

    assert code == 1
    assert_failed(out, "rate_limited")
    assert len(transport.requests) == 1


def test_read_without_a_response_is_network_error(rsrc):
    code, out = rsrc(["read", "50148855"], FakeTransport(TimeoutError("timed out")))

    assert code == 1
    assert_failed(out, "network_error")


@pytest.mark.parametrize("status, body", [
    (500, b"Internal Server Error"),
    (200, b"null"),
    (200, b'{"children": [], "title": "an item without id"}'),
    (200, b'{"id": 50148855, "title": "an item without children"}'),
    (200, b'{"id": 50148855, "children": [50149524]}'),
    (200, b'{"id": 50148855, "children": [{"id": 50149524, "children": [{"text": "a reply without id"}]}]}'),
    (200, b'{"id": null, "children": []}'),
    (200, b'{"id": "50148855", "children": []}'),
    (200, b'{"id": 50148855, "children": [], "text": 5}'),
    (200, b'{"id": 50148855, "children": [{"id": null, "children": [], "text": "a reply with a null id"}]}'),
])
def test_read_bad_answers_are_upstream_error_naming_the_status(rsrc, status, body):
    code, out = rsrc(["read", "50148855"], FakeTransport((status, body)))

    assert code == 1
    assert_failed(out, "upstream_error")
    assert f"HTTP {status}" in out["error"]["message"]


@pytest.mark.parametrize("argv", [["search", "Claude Code"], ["read", "50148855"]])
@pytest.mark.parametrize("status, body", [(403, b"Forbidden"), (200, b"<html>maintenance</html>")])
def test_refusals_and_pages_are_blocked_naming_the_status(rsrc, argv, status, body):
    code, out = rsrc(argv, FakeTransport((status, body)))

    assert code == 1
    assert_failed(out, "blocked")
    assert f"HTTP {status}" in out["error"]["message"]


def test_read_answer_nested_too_deep_to_parse_is_upstream_error(rsrc):
    depth = 100_000
    body = b'{"id": 50148855, "children": [' * depth + b"]}" * depth
    code, out = rsrc(["read", "50148855"], FakeTransport((200, body)))

    assert code == 1
    assert_failed(out, "upstream_error")
