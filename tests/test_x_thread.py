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


MISSING = "2100000000000000001"
MISSING_ENDPOINT = f"https://api.fxtwitter.com/2/thread/{MISSING}"
MISSING_STATUS = f"https://api.fxtwitter.com/2/status/{MISSING}"
SYNDICATION = f"https://cdn.syndication.twimg.com/tweet-result?id={MISSING}&token=a"


def test_thread_reports_a_deleted_post_from_its_tombstone(run_x):
    data = load("x_fx_thread_missing.json")
    data["status"] = {"type": "tombstone", "provider": "twitter", "reason": "deleted"}
    web = FakeWeb(fx=[(404, data)])
    code, result, _ = run_x(["thread", MISSING], web)
    assert code == 1 and not result["ok"] and not result["items"]
    assert result["error"]["code"] == "not_found" and "post deleted" in result["error"]["message"]
    assert web.urls == [MISSING_ENDPOINT]


def test_thread_404_is_not_found_once_the_post_routes_agree_it_is_missing(run_x):
    """The live answer for a post that never existed: the thread 404 carries no post state, so the
    two routes `x read` uses decide."""
    web = FakeWeb(fx=[(404, load("x_fx_thread_missing.json")), (404, {"code": 404, "status": None})],
                  syndication=[(404, b"")])
    code, result, _ = run_x(["thread", MISSING], web)
    assert code == 1 and not result["ok"] and not result["items"]
    assert result["error"]["code"] == "not_found"
    assert MISSING in result["error"]["message"]
    assert web.urls == [MISSING_ENDPOINT, MISSING_STATUS, SYNDICATION]
    assert result["meta"]["attempts"] == [
        {"backend": "fxtwitter", "outcome": "not_found", "requests": 3, "ms": result["meta"]["attempts"][0]["ms"]},
    ]


def test_thread_404_for_a_post_that_exists_is_an_upstream_error(run_x):
    status = load("x_fx_thread_single.json")["status"]
    status = {**status, "id": MISSING}
    web = FakeWeb(fx=[(404, load("x_fx_thread_missing.json")), (200, {"code": 200, "status": status})])
    code, result, _ = run_x(["thread", MISSING], web)
    assert code == 1 and not result["ok"] and not result["items"]
    assert result["error"]["code"] == "upstream_error"
    assert "the post exists" in result["error"]["message"]
    assert f"rsrc x read {MISSING}" in result["error"]["message"]
    assert web.urls == [MISSING_ENDPOINT, MISSING_STATUS]


@pytest.mark.parametrize("change, detail", [
    ({"id": "1917421753820930420"}, "different page"),
    ({"text": "Log in to continue"}, "different page"),
    ({"created_timestamp": "unknown"}, "unreadable post record"),
])
def test_thread_404_trusts_only_a_readable_record_of_the_requested_post(run_x, change, detail):
    status = {**load("x_fx_thread_single.json")["status"], "id": MISSING, **change}
    web = FakeWeb(fx=[(404, load("x_fx_thread_missing.json")), (200, {"code": 200, "status": status})])
    code, result, _ = run_x(["thread", MISSING], web)
    assert code == 1 and result["action"] == "thread" and result["error"]["code"] == "upstream_error"
    assert "could not confirm" in result["error"]["message"] and detail in result["error"]["message"]
    assert result["meta"]["attempts"][0]["requests"] == 2


def test_thread_404_stays_unconfirmed_when_the_post_routes_fail(run_x):
    web = FakeWeb(fx=[(404, load("x_fx_thread_missing.json")), (404, {"code": 404, "status": None})],
                  syndication=[(500, b"")])
    code, result, _ = run_x(["thread", MISSING], web)
    assert code == 1 and result["error"]["code"] == "upstream_error"
    assert "could not confirm" in result["error"]["message"]
    assert web.urls[:2] == [MISSING_ENDPOINT, MISSING_STATUS] and web.urls[2:] and set(web.urls[2:]) == {SYNDICATION}


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
