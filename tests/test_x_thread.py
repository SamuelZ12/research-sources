import pytest

from test_x_fx import FakeWeb, load


POST = "1917421753820930420"
URL = f"https://Twitter.com/spermracing/status/{POST}?s=20"
SERIES = "x_fx_thread_series.json"
POSTS = [
    "1917421746086633833", "1917421748179591468", "1917421750918472134",
    POST, "1917421756056494529", "1917421758036242715",
]
ENDPOINT = f"https://api.fxtwitter.com/2/thread/{POST}"


def test_thread_returns_posts_before_and_after_the_requested_post(run_x):
    data = load(SERIES)
    web = FakeWeb(fx=[(200, data)])
    code, result, _ = run_x(["thread", URL], web)
    assert code == 0 and result["ok"] and result["error"] is None
    assert (result["action"], result["backend"], result["input"]) == ("thread", "fxtwitter", {"post": URL})
    assert [item["id"] for item in result["items"]] == POSTS
    assert [item["author"] for item in result["items"]] == ["spermracing"] * 6
    assert [item["raw"]["reply_to_id"] for item in result["items"]] == [None, *POSTS[:-1]]
    assert result["meta"]["notes"] == ["6 posts returned"]
    assert web.urls == [ENDPOINT]

    _, read, _ = run_x(["read", URL], FakeWeb(fx=[(200, {"status": data["status"]})]))
    assert result["items"][3] == read["items"][0]


def test_thread_of_one_post_says_what_fxtwitter_found(run_x):
    post = "2039011345199403174"
    web = FakeWeb(fx=[(200, load("x_fx_thread_single.json"))])
    code, result, _ = run_x(["thread", post], web)
    assert code == 0 and result["ok"]
    assert [(item["id"], item["author"]) for item in result["items"]] == [(post, "gilad")]
    assert result["meta"]["notes"] == [
        "1 post returned", "FxTwitter shows no self-thread for this post",
    ]
    assert web.urls == [f"https://api.fxtwitter.com/2/thread/{post}"]


@pytest.mark.parametrize("start, end, requested_index", [(0, 5, 3), (0, 1, 0), (3, 4, 3)])
def test_thread_notes_a_likely_cut_when_recorded_numbering_continues(run_x, start, end, requested_index):
    data = load(SERIES)
    data["status"] = data["thread"][requested_index]
    data["thread"] = data["thread"][start:end]
    code, result, _ = run_x(["thread", POSTS[requested_index]], FakeWeb(fx=[(200, data)]))
    assert code == 0 and result["ok"]
    assert [item["id"] for item in result["items"]] == POSTS[start:end]
    assert result["meta"]["notes"] == [
        "1 post returned" if end - start == 1 else "5 posts returned",
        "FxTwitter may have cut the thread short. The numbering indicates 6 posts.",
    ]


@pytest.mark.parametrize("defect, detail", [
    ("missing", f"does not contain the requested post {POST}"),
    ("foreign author", f"post {POSTS[1]} has a different author"),
    ("broken chain", f"post {POSTS[1]} does not reply to an earlier post in the thread"),
    ("duplicate", f"repeated post {POSTS[0]}"),
    ("cycle", f"first post {POSTS[0]} replies within the thread"),
    ("bad shape", "unrecognized thread"),
])
def test_invalid_thread_cannot_pass_as_a_result(run_x, defect, detail):
    data = load(SERIES)
    posts = data["thread"]
    if defect == "missing":
        del posts[3:]
    elif defect == "foreign author":
        posts[1]["author"]["screen_name"] = "other"
    elif defect == "broken chain":
        posts[1]["replying_to"]["status"] = POSTS[2]
    elif defect == "duplicate":
        posts.insert(1, posts[0])
    elif defect == "cycle":
        posts[0]["replying_to"] = {"status": POSTS[-1]}
    else:
        data["thread"] = {"posts": posts}
    web = FakeWeb(fx=[(200, data)])
    code, result, _ = run_x(["thread", POST], web)
    assert code == 1 and not result["ok"] and result["items"] == []
    assert result["backend"] == "fxtwitter" and result["error"]["code"] == "upstream_error"
    assert detail in result["error"]["message"]
    assert web.urls == [ENDPOINT]


@pytest.mark.parametrize("tombstone, expected", [
    (None, "upstream_error"),
    ({"type": "tombstone", "provider": "twitter", "reason": "deleted"}, "not_found"),
])
def test_thread_reports_missing_only_when_fxtwitter_confirms_it(run_x, tombstone, expected):
    post = "2100000000000000001"
    data = load("x_fx_thread_missing.json")
    if tombstone:
        data["status"] = tombstone
    web = FakeWeb(fx=[(404, data)])
    code, result, _ = run_x(["thread", post], web)
    assert code == 1 and not result["ok"] and not result["items"]
    assert result["error"]["code"] == expected
    assert ("post deleted" if tombstone else "HTTP 404") in result["error"]["message"]
    assert web.urls == [f"https://api.fxtwitter.com/2/thread/{post}"]


def test_thread_retries_a_transient_server_failure(run_x):
    web = FakeWeb(fx=[(503, b""), (200, load(SERIES))])
    code, result, _ = run_x(["thread", POST], web)
    assert code == 0 and result["ok"]
    assert web.urls == [ENDPOINT, ENDPOINT]
    assert result["meta"]["attempts"][0]["requests"] == 2


@pytest.mark.parametrize("args, detail", [
    ([], "arguments are required: URL_OR_ID"),
    ([POST, POST], "unrecognized arguments"),
    ([POST, "--backend", "fxtwitter"], "unrecognized arguments"),
    (["hello"], "not a post URL or ID"),
    ([f"https://example.org/?next={URL}"], "not a post URL or ID"),
    ([f"https://x.com/spermracing/article/{POST}"], "not a post URL or ID"),
    ([f"https://x.com/i/article/{POST}"], "not a post URL or ID"),
])
def test_thread_usage_fails_before_a_request(run_x, args, detail):
    web = FakeWeb()
    code, result, _ = run_x(["thread", *args], web)
    assert code == 2 and not result["ok"] and result["action"] == "thread"
    assert result["error"]["code"] == "usage" and detail in result["error"]["message"]
    assert web.urls == []
