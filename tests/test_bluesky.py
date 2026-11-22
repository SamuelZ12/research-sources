import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from rsrc import cli

FIXTURES = Path(__file__).parent / "fixtures"


def recorded(name):
    return json.loads((FIXTURES / f"bluesky_{name}.json").read_text())


@pytest.fixture
def run(capsys):
    def invoke(args, *responses):
        calls = []
        def transport(method, url, headers, body):
            assert method == "GET" and not headers and body is None
            status, data = responses[len(calls)]
            calls.append(urlsplit(url))
            return status, data if isinstance(data, bytes) else json.dumps(data).encode()
        code = cli.main(["bluesky", *args], cli.Context(transport=transport, sleep=lambda _: None))
        return code, json.loads(capsys.readouterr().out), calls
    return invoke


def test_search_filters_and_host(run):
    code, out, calls = run(["search", "python", "--since", "2026-11-21", "--until", "2026-11-23",
                            "--domain", "example.com", "--top"], (200, recorded("search")))
    assert code == 0 and out["items"][0]["engagement"]["likes"] >= 0
    assert calls[0].hostname == "api.bsky.app"
    assert parse_qs(calls[0].query) == {"q": ["python"], "since": ["2026-11-21"], "until": ["2026-11-23"],
                                       "sort": ["top"], "limit": ["20"], "domain": ["example.com"]}


@pytest.mark.parametrize("status,payload,code,reason", [
    (200, recorded("empty"), 66, "no_results"), (429, {}, 1, "rate_limited"),
    (403, (FIXTURES / "bluesky_forbidden.html").read_bytes(), 1, "blocked"), (401, {}, 1, "auth_failed"),
    (200, (FIXTURES / "login_wall.html").read_bytes(), 1, "blocked"),
    (200, b'{"posts":', 1, "upstream_error"), (200, {"posts": {}}, 1, "upstream_error"),
    (200, {"error": "InvalidRequest", "posts": []}, 1, "upstream_error"),
])
def test_failures_stop(run, status, payload, code, reason):
    actual, out, calls = run(["search", "python"], (status, payload))
    assert actual == code and len(calls) == 1 and not out["items"]
    assert out["meta"]["attempts"][0]["outcome"] == reason


@pytest.mark.parametrize("action", ["read", "replies"])
def test_thread_identity_and_links(run, action):
    thread = recorded("thread")
    root = thread["thread"]["post"]
    url = "https://bsky.app/profile/bsky.app/post/" + root["uri"].split("/")[-1]
    code, out, calls = run([action, url + "?ref=share"], (200, thread))
    assert code == 0 and calls[0].hostname == "public.api.bsky.app"
    item = out["items"][0]
    if action == "read":
        assert item["url"] == url and item["id"] == root["uri"]
        assert "https://bsky.app/settings/beta" in item["text"]
    else:
        assert item["raw"]["parent_uri"] == root["uri"] and item["raw"]["depth"] == 1
    assert run([action, root["uri"]], (200, thread))[0] == 0
    assert run([action, url + "x"], (200, thread))[1]["error"]["code"] == "url_mismatch"
    assert run([action, url], (400, recorded("missing")))[1]["error"]["code"] == "not_found"


def test_profile_cards_quotes_and_cap(run):
    args = ["timeline", "@atproto.com"]
    profile, feed = recorded("profile"), recorded("feed")
    code, out, urls = run(args, (200, profile), (200, feed))
    assert code == 0 and len(urls) == 2
    account, card, quote = out["items"]
    assert account["raw"]["kind"] == "profile" and account["title"] == profile["displayName"]
    assert account["engagement"] == {"followers": profile["followersCount"], "following": profile["followsCount"],
                                     "posts": profile["postsCount"]}
    assert card["raw"]["embed"]["external"]["uri"] in card["text"]
    assert quote["raw"]["embed"]["record"]["value"]["text"]
    assert quote["raw"]["quote"]["text"] == quote["raw"]["embed"]["record"]["value"]["text"]
    code, out, urls = run([*args, "--max-requests", "1"], (200, profile))
    assert code == 1 and len(urls) == 1 and out["meta"]["truncated"] == "request_cap"
    feed["feed"][0]["post"]["author"]["did"] += "x"
    assert run(args, (200, profile), (200, feed))[1]["error"]["code"] == "url_mismatch"


def test_invalid_args(run):
    for args in (["read", "https://elsewhere.com/profile/a.b/post/1"], ["timeline", "a/b"],
                 ["search", "term", "--since", "2026-11-22", "--until", "2026-11-21"]):
        code, _, calls = run(args)
        assert code == 2 and not calls


def test_url_discussion_without_keywords(run):
    url = "https://atproto.com/blog/moderation-in-spaces"
    code, _, calls = run(["search", "--url", url], (200, recorded("empty")))
    assert code == 66 and parse_qs(calls[0].query)["url"] == [url]
    assert parse_qs(calls[0].query)["q"] == ["*"]


def test_inaccessible_replies_and_wrong_parent(run):
    data = recorded("thread")
    thread = data["thread"]
    uri = thread["post"]["uri"]
    thread["replies"][0]["post"]["record"]["reply"]["parent"]["uri"] += "x"
    assert run(["replies", uri], (200, data))[1]["error"]["code"] == "url_mismatch"
    thread["replies"] = [{"$type": "app.bsky.feed.defs#blockedPost"}]
    assert run(["replies", uri], (200, data))[1]["error"]["code"] == "blocked"
    thread["replies"] = []
    assert run(["replies", uri], (200, data))[0] == 66


def test_link_facet_offsets_count_utf8_bytes(run):
    thread = recorded("thread")
    post = thread["thread"]["post"]
    record = post["record"]
    record["text"] = "🦋 " + record["text"]
    for facet in record["facets"]:
        for key in ("byteStart", "byteEnd"):
            facet["index"][key] += len("🦋 ".encode())
    code, out, _ = run(["read", post["uri"]], (200, thread))
    assert code == 0 and out["items"][0]["text"].startswith("🦋 ")
    assert "https://bsky.app/settings/beta" in out["items"][0]["text"]


def test_feed_failure_preserves_profile_and_stops(run):
    code, out, urls = run(["timeline", "atproto.com"], (200, recorded("profile")), (403, b""))
    assert code == 1 and len(urls) == 2 and out["error"]["code"] == "blocked"
    assert [i["author"] for i in out["items"]] == ["atproto.com"]
    assert all(url.hostname == "public.api.bsky.app" for url in urls)
