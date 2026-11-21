import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def page():
    return json.loads((FIXTURES / "x_fx_timeline.json").read_text())


def only_page():
    """The fixture as the account's whole timeline: no cursor to a next page."""
    return {k: v for k, v in page().items() if k != "cursor"}


def status(post_id, author, **extra):
    return {"type": "status", "id": post_id, "url": f"https://x.com/{author}/status/{post_id}",
            "text": f"post {post_id}", "author": {"screen_name": author, "id": f"id-{author}"}, **extra}


class FakeFx:
    def __init__(self, pages):
        self.pages = list(pages)
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        status, data = self.pages.pop(0)
        return status, json.dumps(data).encode()


@pytest.fixture
def run(run_x):
    """`rsrc x timeline ARGV`: returns (exit code, envelope)."""
    def run(argv, fake):
        code, env, _ = run_x(["timeline", *argv], fake)
        return code, env

    return run


def test_account_comes_first_then_its_posts_newest_first(run):
    fake = FakeFx([(200, only_page())])
    code, env = run(["mitchellh"], fake)
    assert code == 0 and env["action"] == "timeline" and env["backend"] == "fxtwitter"
    account, *posts = env["items"]
    assert account["url"] == "https://x.com/mitchellh"
    assert account["author"] == "mitchellh" and account["title"] == "Mitchell Hashimoto"
    assert account["text"].startswith("Co-founder of @Superlogical")
    assert account["published_at"] == "2008-01-29T07:56:05Z"
    assert account["engagement"] == {"followers": 243208, "following": 160, "posts": 41297}
    assert account["raw"]["location"] == "Los Angeles, CA"
    assert [p["id"] for p in posts] == [s["id"] for s in page()["results"]]
    assert sorted((p["published_at"] for p in posts), reverse=True) == [p["published_at"] for p in posts]
    assert all(p["source"] == "fxtwitter" and p["author"] == "mitchellh" for p in posts)
    assert posts[0]["url"] == "https://x.com/mitchellh/status/2123516770812387809"
    assert posts[0]["engagement"]["likes"] == 1325
    assert env["meta"]["attempts"] == [{"backend": "fxtwitter", "outcome": "ok", "requests": 1,
                                        "ms": env["meta"]["attempts"][0]["ms"]}]


def test_limit_met_by_the_first_page_costs_one_request(run):
    fake = FakeFx([(200, page())])
    code, env = run(["mitchellh", "--limit", "3"], fake)
    [url] = fake.urls
    assert urlsplit(url).path == "/2/profile/mitchellh/statuses"
    assert parse_qs(urlsplit(url).query) == {"count": ["3"]}
    assert code == 0 and len(env["items"]) == 4  # the account, then 3 posts


def test_follows_the_cursor_until_the_limit(run):
    first = page()
    second = {"code": 200, "results": [status("9", "mitchellh"), status("8", "mitchellh")], "cursor": {"bottom": "c2"}}
    fake = FakeFx([(200, first), (200, second)])
    code, env = run(["mitchellh", "--limit", "5"], fake)
    assert code == 0 and [i["id"] for i in env["items"][1:]] == [*[s["id"] for s in first["results"]], "9"]
    assert parse_qs(urlsplit(fake.urls[1]).query)["cursor"] == [first["cursor"]["bottom"]]
    assert env["meta"]["attempts"][0]["requests"] == 2


def test_stops_when_the_timeline_ends_before_the_limit(run):
    fake = FakeFx([(200, only_page())])
    code, env = run(["mitchellh", "--limit", "50"], fake)
    assert code == 0 and len(env["items"]) == 5 and len(fake.urls) == 1


def test_page_with_no_new_posts_stops_paging(run):
    fake = FakeFx([(200, page()), (200, page() | {"cursor": {"bottom": "another"}}), (200, page())])
    code, env = run(["mitchellh", "--limit", "50"], fake)
    assert code == 0 and len(env["items"]) == 5 and len(fake.urls) == 2


def test_failure_on_a_later_page_keeps_the_posts_read_so_far(run):
    fake = FakeFx([(200, page()), *[(500, {})] * 3])
    code, env = run(["mitchellh", "--limit", "50"], fake)
    assert code == 1 and env["error"]["code"] == "upstream_error" and len(env["items"]) == 5
    assert env["items"][0]["url"] == "https://x.com/mitchellh"


@pytest.mark.parametrize("given", ["@mitchellh", "https://x.com/mitchellh", "x.com/mitchellh/",
                                   "https://twitter.com/mitchellh/with_replies",
                                   "https://www.x.com/mitchellh/media?lang=en", "https://mobile.x.com/@mitchellh"])
def test_handle_is_read_from_an_at_name_or_an_account_url(run, given):
    fake = FakeFx([(200, only_page())])
    code, env = run([given], fake)
    assert code == 0 and urlsplit(fake.urls[0]).path == "/2/profile/mitchellh/statuses"
    assert env["input"] == {"account": given, "limit": 20}


def test_post_url_is_a_usage_error_that_names_x_read(run):
    fake = FakeFx([])
    code, env = run(["https://x.com/mitchellh/status/2123516770812387809"], fake)
    assert code == 2 and env["error"]["code"] == "usage" and "rsrc x read" in env["error"]["message"]
    assert fake.urls == []


@pytest.mark.parametrize("given", ["https://x.com/search?q=cats", "https://x.com/i/communitynotes/1",
                                   "https://example.com/mitchellh", "two words", "a-dash"])
def test_anything_that_is_not_an_account_is_a_usage_error(run, given):
    fake = FakeFx([])
    code, env = run([given], fake)
    assert code == 2 and env["error"]["code"] == "usage" and fake.urls == []


def test_limit_above_the_maximum_is_a_usage_error(run):
    fake = FakeFx([])
    code, env = run(["mitchellh", "--limit", "201"], fake)
    assert code == 2 and env["error"]["code"] == "usage" and "200" in env["error"]["message"]
    assert fake.urls == []


def test_failure_after_a_page_of_reposts_does_not_blame_reposts(run):
    first = {"code": 200, "results": [status("3", "someoneelse")], "cursor": {"bottom": "c1"}}
    code, env = run(["mitchellh"], FakeFx([(200, first), *[(500, {})] * 3]))
    assert code == 1 and env["error"]["code"] == "upstream_error" and len(env["items"]) == 1
    assert env["meta"]["notes"] == []


def test_account_is_matched_case_insensitively_past_a_leading_repost(run):
    data = {"code": 200, "results": [status("3", "someoneelse"), status("2", "MitchellH", likes=7)]}
    code, env = run(["mitchellh", "--limit", "2"], FakeFx([(200, data)]))
    account, repost, own = env["items"]
    assert code == 0 and account["author"] == "MitchellH" and account["id"] == "id-MitchellH"
    assert repost["author"] == "someoneelse" and own["author"] == "MitchellH"
    assert env["meta"]["notes"] == []


def test_reposts_only_returns_the_posts_without_an_account_item(run):
    data = {"code": 200, "results": [status("3", "someoneelse"), status("2", "another")]}
    code, env = run(["mitchellh", "--limit", "2"], FakeFx([(200, data)]))
    assert code == 0 and [i["author"] for i in env["items"]] == ["someoneelse", "another"]
    assert env["meta"]["notes"] == ["none of the 2 posts is by @mitchellh (reposts only), so there is no account item"]


def test_no_posts_is_an_empty_result(run):
    code, env = run(["mitchellh"], FakeFx([(200, {"code": 200, "results": []})]))
    assert code == 66 and env["ok"] and env["items"] == []
    assert env["meta"]["attempts"][0]["outcome"] == "no_results"


def test_repost_names_the_account_that_reposted_it(run):
    page = {"code": 200, "results": [status("9", "bcherny", reposted_by={"screen_name": "mitchellh"}),
                                     status("8", "mitchellh")]}
    code, env = run(["mitchellh"], FakeFx([(200, page)]))
    account, repost, own = env["items"]
    assert (account["author"], repost["author"], repost["raw"]["reposted_by"]) == ("mitchellh", "bcherny", "mitchellh")
    assert "reposted_by" not in own["raw"]


def test_lasting_404_cannot_tell_a_missing_account_from_a_failing_fxtwitter(run):
    fake = FakeFx([(404, {"code": 404, "message": "User not found"})] * 3)
    code, env = run(["nosuchaccount"], fake)
    assert code == 1 and env["error"]["code"] == "upstream_error" and env["items"] == []
    assert "rsrc x search 'from:" in env["error"]["message"]
    assert env["meta"]["attempts"][0]["outcome"] == "upstream_error"


def test_a_404_followed_by_the_timeline_still_answers(run):
    fake = FakeFx([(404, {}), (200, only_page())])
    code, env = run(["mitchellh"], fake)
    assert code == 0 and len(env["items"]) == 5 and env["meta"]["attempts"][0]["requests"] == 2


def test_server_error_is_upstream_error(run):
    code, env = run(["mitchellh"], FakeFx([(500, {})] * 3))
    assert code == 1 and env["error"]["code"] == "upstream_error"


def test_rate_limit_stops_at_the_first_request(run):
    fake = FakeFx([(429, {})] * 3)
    code, env = run(["mitchellh"], fake)
    assert code == 1 and env["error"]["code"] == "rate_limited" and len(fake.urls) == 1
