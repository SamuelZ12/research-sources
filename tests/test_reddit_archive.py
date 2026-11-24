"""Replay a trimmed, anonymized Arctic Shift post and comment tree through the CLI."""

import json
from pathlib import Path

import pytest

from rsrc import cli, proc
from rsrc.channels import reddit, reddit_archive
from rsrc.envelope import ITEM_KEYS

FIXTURES = Path(__file__).parent / "fixtures"
POST = (FIXTURES / "reddit_archive_post.json").read_bytes()
TREE = (FIXTURES / "reddit_archive_tree.json").read_bytes()
POST_URL = "https://www.reddit.com/r/ModSupport/comments/1641krf/"
POST_API = reddit_archive.API + "/posts/ids?ids=1641krf"
TREE_API = reddit_archive.API + "/comments/tree?link_id=1641krf&limit=200"


@pytest.fixture
def run(monkeypatch, capsys):
    def command(args, *responses, ego=False):
        urls = []
        remaining = iter(responses)

        def transport(method, url, headers, body):
            assert (method, headers, body) == ("GET", {"user-agent": "research-sources"}, None)
            urls.append(url)
            return next(remaining)

        def runner(argv, timeout):
            if args[0] == "check":
                assert argv[:3] == ["ego-browser", "nodejs", "-e"]
                raise proc.Missing("ego-browser")
            pytest.fail("Archive read must not invoke a subprocess")

        def redirect(*args):
            pytest.fail("Archive read must not resolve share links through Reddit")

        monkeypatch.setattr(reddit, "which", lambda _: "/bin/ego-browser" if ego else None)
        ctx = cli.Context(transport=transport, runner=runner, redirect=redirect, sleep=lambda _: None)
        exit_code = cli.main(args, ctx)
        assert next(remaining, None) is None
        return exit_code, json.loads(capsys.readouterr().out), urls

    return command


def response(data):
    return 200, json.dumps(data).encode()


def test_fallback_maps_existing_item_contract_and_snapshot(run, monkeypatch):
    monkeypatch.setattr(reddit_archive.time, "time", lambda: 1754346903 + 90000)
    code, out, urls = run(["reddit", "read", POST_URL], (200, POST), (200, TREE))
    assert code == 0 and out["ok"] and out["backend"] == "arctic-shift"
    assert urls == [POST_API, TREE_API]
    assert out["meta"]["attempts"][0]["requests"] == 2
    post, root, reply, removed = out["items"]
    assert all(tuple(item) == ITEM_KEYS for item in out["items"])
    assert [item["raw"]["kind"] for item in out["items"]] == ["post", "comment", "comment", "comment"]
    assert post["id"] == "t3_1641krf" and post["engagement"] == {"score": 35, "comments": 21}
    assert post["url"] == POST_URL and post["published_at"] == "2023-08-28T23:22:41Z"
    assert root["text"] == "Example parent comment." and root["author"] == "example_parent"
    assert reply["title"] is None and reply["url"] == f"{POST_URL}_/jy5yacd/"
    assert reply["raw"]["attrs"]["depth"] == "1" and reply["raw"]["attrs"]["parentid"] == root["id"]
    assert removed["text"] == "[removed]" and removed["author"] is None
    notes = " ".join(out["meta"]["notes"])
    assert "Arctic Shift archive copy" in notes and "2025-08-04T22:35:03Z (1 day old)" in notes
    assert "descending archived score" in notes
    assert "Returned 3 comments" in notes and "against 21 comments" in notes
    assert "0 returned items as deleted and 1 as removed" in notes and "raw.attrs.post-language" in notes


def test_explicit_backend_and_comment_limit_rank_by_score(run):
    tree = json.loads(TREE)
    tree["data"][1]["data"]["score"] = 10
    code, out, urls = run(["reddit", "read", "t3_1641krf", "--backend", "arctic-shift", "--comments", "1"],
                          (200, POST), response(tree), ego=True)
    assert code == 0 and urls == [POST_API, TREE_API]
    assert [item["id"] for item in out["items"]] == ["t3_1641krf", "t1_jy7o5p5"]
    assert "Returned 1 comment. The fetched tree supplied 3" in " ".join(out["meta"]["notes"])


def test_marked_content_never_leaks_into_raw_fields(run):
    post = json.loads(POST)
    row = post["data"][0]
    row["_meta"].update(was_deleted_later=True, removal_type="deleted")
    row["url"] = "https://example.com/secret-title"
    tree = json.loads(TREE)
    row = tree["data"][0]["data"]
    row["_meta"].update(was_deleted_later=True, removal_type="moderator")
    code, out, _ = run(["reddit", "read", POST_URL], response(post), response(tree))
    assert code == 0
    post, root, reply, _ = out["items"]
    assert post["text"] == "[deleted]" and post["title"] is None
    assert root["text"] == "[removed]" and reply["text"] == "Example reply."
    for text in ("Example post", "Example parent", "example_poster", "example_parent", "secret-title"):
        assert text not in json.dumps(out)
    assert "1 returned items as deleted and 2 as removed" in " ".join(out["meta"]["notes"])


def test_restored_comment_is_readable(run):
    tree = json.loads(TREE)
    row = tree["data"][0]["data"]
    row["_meta"].update(was_initially_deleted=True, removal_type="moderator")
    code, out, _ = run(["reddit", "read", POST_URL], (200, POST), response(tree))
    assert code == 0 and out["items"][1]["text"] == "Example parent comment."


def test_collapsed_comments_are_reported_without_fake_items(run):
    tree = json.loads(TREE)
    tree["data"].append({"kind": "more", "data": {"children": ["abcde"]}})
    code, out, _ = run(["reddit", "read", POST_URL], (200, POST), response(tree))
    assert code == 0 and len(out["items"]) == 4
    assert "collapsed parts of the tree" in " ".join(out["meta"]["notes"])


def test_only_post_uses_one_request_and_first_capture_if_needed(run):
    post = json.loads(POST)
    post["data"][0].pop("_meta")
    code, out, urls = run(["reddit", "read", "https://redd.it/1641krf", "--comments", "0"], response(post))
    assert code == 0 and urls == [POST_API] and len(out["items"]) == 1
    assert "2023-08-28T23:23:02Z" in " ".join(out["meta"]["notes"])


@pytest.mark.parametrize("args", [
    ["https://www.reddit.com/r/ModSupport/s/example"],
    [POST_URL, "--sort", "best"],
])
def test_unsupported_archive_inputs_fail_without_network(run, args):
    code, out, urls = run(["reddit", "read", *args])
    assert code == 2 and not out["ok"] and not urls


@pytest.mark.parametrize(("upstream", "error"), [
    ((200, b'{"data":[]}'), "not_found"),
    ((429, {"Retry-After": "30"}, b""), "rate_limited"),
    ((200, b"<html>security check</html>"), "upstream_error"),
    ((302, {"Location": "https://reddit.com/"}, b""), "upstream_error"),
    ((200, b'{"data":[],"error":"Query timed out"}'), "upstream_error"),
    ((200, b'{"data":[{"id":"wrong","title":"Wrong post"}]}'), "upstream_error"),
    ((200, b'{"data":[{"id":"1641krf"}]}'), "upstream_error"),
])
def test_bad_post_responses_stop_immediately(run, upstream, error):
    code, out, urls = run(["reddit", "read", POST_URL], upstream)
    assert code == 1 and not out["ok"] and out["error"]["code"] == error
    assert out["items"] == [] and urls == [POST_API]
    assert out["meta"]["attempts"][0]["outcome"] == error


@pytest.mark.parametrize(("upstream", "error"), [
    ((429, b""), "rate_limited"),
    ((200, b'{"data":[]}'), "upstream_error"),
    ((200, b'{"data":[{"kind":"t1","data":{"id":"junk","link_id":"wrong"}}]}'), "upstream_error"),
])
def test_failed_comment_fetch_keeps_post_but_exits_nonzero(run, upstream, error):
    code, out, urls = run(["reddit", "read", POST_URL], (200, POST), upstream)
    assert code == 1 and not out["ok"] and out["error"]["code"] == error
    assert urls == [POST_API, TREE_API] and len(out["items"]) == 1


def test_zero_comment_thread_is_a_real_post_only_result(run):
    post = json.loads(POST)
    post["data"][0]["num_comments"] = 0
    code, out, urls = run(["reddit", "read", POST_URL], response(post), response({"data": []}))
    assert code == 0 and len(out["items"]) == 1 and urls == [POST_API, TREE_API]


def test_check_confirms_archive_read_when_ego_is_absent(run):
    code, out, urls = run(["check", "reddit"], (200, POST), (200, TREE))
    assert code == 0 and urls == [POST_API, TREE_API] and out["meta"]["sources"] == {"reddit": "ok"}
    assert out["items"][0]["text"] == "Thread reads work through the Arctic Shift archive. Reddit search requires Ego Lite."


def test_check_live_requires_ego(run):
    code, out, urls = run(["check", "reddit", "--live-reddit"])
    assert code == 2 and not urls and out["error"]["code"] == "config"
