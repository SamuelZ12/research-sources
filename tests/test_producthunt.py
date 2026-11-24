import json
from pathlib import Path

import pytest

from rsrc import cli
from rsrc.channels import producthunt as ph

FIX = Path(__file__).parent / "fixtures" / "producthunt"


def recording(name):
    return json.loads((FIX / f"{name}.json").read_text())


def command(capsys, args, *responses, **by_tool):
    calls = []

    def transport(method, url, headers, body):
        assert method == "POST" and url == ph.ENDPOINT
        request = json.loads(body)
        calls.append(request)
        assert request["method"] == headers["Mcp-Method"] == "tools/call"
        assert headers["Mcp-Name"] == request["params"]["name"]
        assert headers["MCP-Protocol-Version"] == request["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"]
        result = by_tool[headers["Mcp-Name"]] if by_tool else responses[len(calls) - 1]
        if isinstance(result, tuple):
            return result
        result["id"] = request["id"]
        return 200, json.dumps(result).encode()

    code = cli.main(["producthunt", *args], cli.Context(transport=transport, sleep=lambda _: None))
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == result["meta"]["attempts"][0]["requests"]
    return code, result, [{k: v for k, v in c["params"].items() if k != "_meta"} for c in calls]


def test_search(capsys):
    code, out, calls = command(capsys, ["search", "linear"], recording("search"))
    item, = out["items"]
    assert code == 0 and calls == [{"name": "search_products", "arguments": {"query": "linear", "limit": 10}}]
    assert item["url"] == "https://www.producthunt.com/products/linear"
    assert item["engagement"]["top_launch_points"] == 767


@pytest.mark.parametrize("value", ["linear", "https://www.producthunt.com/products/linear?ref=test"])
def test_profile(capsys, value):
    code, out, calls = command(capsys, ["read", value], recording("search"), recording("profile"))
    item, = out["items"]
    assert code == 0 and item["published_at"] == "2026-05-29T07:01:00Z"
    assert item["engagement"]["rating"] == 4.93
    assert set(item["raw"]["profile"]["review_highlights"][0]) == {"rating", "excerpt"}
    assert calls[1]["arguments"] == {"product_name": "linear"}


@pytest.mark.parametrize("path", ["posts/linear-mobile-2", "products/linear/launches/linear-mobile-2",
                                  "products/linear?launch=linear-mobile-2"])
def test_exact_launch(capsys, path):
    code, out, _ = command(capsys, ["read", "https://www.producthunt.com/" + path],
                           recording("search"), recording("profile"))
    item, = out["items"]
    assert code == 0 and item["engagement"]["points"] == 592
    assert item["published_at"] == "2024-09-20T07:01:00Z"


@pytest.mark.parametrize("path,slug,tagline,points,launched_at", [
    ("products/linear", "linear", "The product development system for teams and agents.", 767, "2026-05-29T07:01:00Z"),
    ("posts/linear-mobile-2", "linear-mobile-2", "The portable companion to the Linear system", 592, "2024-09-20T07:01:00Z"),
])
def test_read_when_profile_is_missing(capsys, path, slug, tagline, points, launched_at):
    url = "https://www.producthunt.com/" + path
    code, out, _ = command(capsys, ["read", url], search_products=recording("search"),
                           get_product_profile=recording("missing"))
    assert code == 0 and out["error"] is None
    item, = out["items"]
    assert item["id"] == slug and item["url"] == url and item["text"] == tagline
    assert item["published_at"] == launched_at
    points_key = "points" if path.startswith("posts/") else "top_launch_points"
    assert item["engagement"] == {"rating": 4.93, "reviews": 474, points_key: points}
    row = item["raw"]["product"] if path.startswith("posts/") else item["raw"]
    assert row["makers"][0]["name"] == "Lasse Boisen Andersen" and row["launches"][0]["slug"] == "linear-3"
    assert out["meta"]["notes"] == ["Product Hunt profile unavailable. Description and review highlights are missing."]
    assert out["meta"]["attempts"][0]["requests"] == 2


@pytest.mark.parametrize("path,error", [("products/unknown", "not_found"), ("posts/unknown", "upstream_error")])
def test_read_needs_exact_search_match_when_profile_is_missing(capsys, path, error):
    code, out, _ = command(capsys, ["read", "https://www.producthunt.com/" + path],
                           search_products=recording("search"), get_product_profile=recording("missing"))
    assert code == 1 and out["items"] == [] and out["error"]["code"] == error


def test_board(capsys):
    code, out, _ = command(capsys, ["leaderboard", "--date", "2026-11-21"], recording("board"))
    item, = out["items"]
    assert code == 0 and out["input"]["timezone"] == "America/Los_Angeles"
    assert (item["title"], item["engagement"]["points"], item["raw"]["rank"]) == ("AlterContacts", 282, 1)


@pytest.mark.parametrize("args,reply,exitcode,error", [
    (["search", "qzxvjjkppqzxvjjk"], "empty", 66, None),
    (["read", "qzxvjjkppqzxvjjk"], "missing", 1, "not_found"),
    (["read", "wrong"], "profile", 1, "url_mismatch"),
    (["leaderboard", "--date", "2026-11-20"], "board", 1, "url_mismatch"),
    (["search", "test"], (429, b""), 1, "rate_limited"),
    (["search", "test"], (403, b"<title>Just a moment...</title>"), 1, "blocked"),
    (["search", "test"], (200, b"<html>Login</html>"), 1, "blocked"),
    (["search", "test"], (200, b"{}"), 1, "upstream_error"),
    (["search", "test"], (401, b""), 1, "auth_failed"),
])
def test_failures_and_empty(capsys, args, reply, exitcode, error):
    reply = recording(reply) if isinstance(reply, str) else reply
    responses = [recording("empty"), reply] if args[0] == "read" else [reply]
    code, out, calls = command(capsys, args, *responses)
    assert code == exitcode and not out["items"] and len(calls) == len(responses)
    assert (out["error"] or {}).get("code") == error


def test_bounded_requests(capsys):
    code, out, _ = command(capsys, ["read", "linear", "--max-requests", "1"], recording("search"))
    assert code == 1 and out["meta"]["truncated"] == "request_cap"


def test_broken_empty(capsys):
    reply = recording("empty")
    del reply["result"]["structuredContent"]["data"]["empty_reason"]
    code, out, _ = command(capsys, ["search", "linear"], reply)
    assert code == 1 and not out["ok"]


def test_forum_does_not_resolve_to_product(capsys):
    code, out, calls = command(capsys, ["read", "https://www.producthunt.com/p/linear/thread"])
    assert code == 2 and out["error"]["code"] == "usage" and not calls
    assert "forum discussions" in out["error"]["message"]


@pytest.mark.parametrize("field,value", [("rank", 2), ("rank", True), ("launched_at", "2026-11-20T23:59:00-08:00"),
                                          ("url", "https://other.example/posts/altercontacts")])
def test_board_rejects_wrong_launch(capsys, field, value):
    reply = recording("board")
    reply["result"]["structuredContent"]["data"]["results"][0]["launches"][0][field] = value
    code, out, _ = command(capsys, ["leaderboard", "--date", "2026-11-21"], reply)
    assert code == 1 and out["items"] == []


def test_rejects_changed_tool_contract(capsys):
    code, out, _ = command(capsys, ["search", "linear"],
                           {"jsonrpc": "2.0", "error": {"code": -32602, "message": "Unknown tool search_products"}})
    assert code == 1 and "Unknown tool search_products" in out["error"]["message"]


def test_launch_with_wrong_parent(capsys):
    code, out, _ = command(capsys, ["read", "https://www.producthunt.com/products/other/launches/linear-mobile-2"],
                           recording("search"))
    assert code == 1 and not out["items"]


def test_malformed_review_is_an_error(capsys):
    reply = recording("profile")
    reply["result"]["structuredContent"]["data"]["review_highlights"] = [{"body": "changed schema"}]
    code, out, _ = command(capsys, ["read", "linear"], recording("search"), reply)
    assert code == 1 and out["error"]["code"] == "upstream_error"


def test_event_stream_with_notification(capsys):
    reply = json.dumps(recording("search")).encode()
    stream = (b": keepalive\r\n\r\nevent: message\r\ndata: "
              b'{"jsonrpc":"2.0","method":"notifications/message"}\r\n\r\nevent: message\r\ndata: ' + reply + b"\r\n\r\n")
    code, out, _ = command(capsys, ["search", "linear"], (200, stream))
    assert code == 0 and out["items"][0]["id"] == "linear"


def test_response_id_mismatch(capsys):
    reply = recording("search")
    reply["id"] = 82
    code, out, _ = command(capsys, ["search", "linear"], (200, json.dumps(reply).encode()))
    assert code == 1 and not out["items"]


def test_leaderboard_help_shows_budget(capsys):
    assert cli.main(["producthunt", "leaderboard", "--help"]) == 0
    assert "total MCP requests (default 2)" in capsys.readouterr().out


@pytest.mark.parametrize("args,fixture", [(["search", "linear"], "search"),
                                         (["leaderboard", "--date", "2026-11-21"], "board")])
def test_limit_caps_returned_items(capsys, args, fixture):
    reply = recording(fixture)
    rows = reply["result"]["structuredContent"]["data"]["results"]
    rows.append(recording(fixture)["result"]["structuredContent"]["data"]["results"][0])
    code, out, _ = command(capsys, [*args, "--limit", "1"], reply)
    assert code == 0 and len(out["items"]) == 1
