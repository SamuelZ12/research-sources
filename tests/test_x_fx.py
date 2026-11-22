import json
from pathlib import Path

import pytest

from rsrc.channels.x.fx import parse_id, read, status_record, syndication_record
from rsrc.http import Client

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text())


class FakeWeb:
    """Routes by host: `fx` and `syndication` are lists of (status, body) served in order; an
    exception in a list is raised instead."""

    def __init__(self, fx=(), syndication=()):
        self.fx = list(fx)
        self.syndication = list(syndication)
        self.urls = []

    def __call__(self, method, url, headers, body):
        self.urls.append(url)
        queue = self.fx if "api.fxtwitter.com" in url else self.syndication
        response = queue.pop(0)
        if isinstance(response, Exception):
            raise response
        status, data = response
        return status, json.dumps(data).encode() if isinstance(data, dict) else data


# --- ids -----------------------------------------------------------------------

@pytest.mark.parametrize("arg", [
    "https://x.com/bkdgiffug/status/2117770299454177696",
    "https://x.com/bkdgiffug/status/2117770299454177696?s=20&t=abc",
    "https://twitter.com/bkdgiffug/status/2117770299454177696/photo/1",
    "https://mobile.twitter.com/bkdgiffug/statuses/2117770299454177696",
    "https://x.com/i/web/status/2117770299454177696",
    "2117770299454177696",
])
def test_post_urls_and_bare_ids_resolve(arg):
    assert parse_id(arg) == "2117770299454177696"


@pytest.mark.parametrize("arg", ["hello", "https://x.com/bkdgiffug", "1"])
def test_non_post_arguments_are_rejected(arg):
    assert parse_id(arg) is None


# --- records -------------------------------------------------------------------

def test_fxtwitter_status_maps_to_record():
    post = status_record(load("x_fx_status.json")["status"])
    assert post["id"] == "2117770299454177696"
    assert post["author"] == "bkdgiffug"
    assert post["url"] == "https://x.com/bkdgiffug/status/2117770299454177696"
    assert post["created_at"] == "2026-11-04T00:06:00Z"
    assert (post["likes"], post["reposts"], post["replies"], post["views"]) == (9, 3, 14, 1140)
    assert post["lang"] == "zh" and post["reply_to_id"] is None and post["source"] == "fxtwitter"


def test_fxtwitter_reply_carries_parent_id():
    post = status_record(load("x_fx_status_reply.json")["status"])
    assert post["reply_to_id"] == "2114277761355305339"


def test_fxtwitter_quote_is_attached():
    status = load("x_fx_status.json")["status"]
    status["quote"] = {"id": "42", "text": "quoted text", "author": {"screen_name": "bob"}}
    assert status_record(status)["quoted"] == {"id": "42", "author": "bob", "text": "quoted text", "media": []}


# The recorded search page and the recorded x-graphql page both hold this post and its one video.
VIDEO = {"type": "video", "duration": 63.6,
         "url": "https://video.twimg.com/amplify_video/2115884803920973824/vid/avc1/1920x1080/p06NTnUa24DU1xG8.mp4?tag=29"}


def test_fxtwitter_photo_is_listed_in_media():
    post = status_record(load("x_fx_status.json")["status"])
    assert post["media"] == [{"type": "photo", "duration": None,
                              "url": "https://pbs.twimg.com/media/HWMgzhNaEAAwev1.png?name=orig"}]


def test_fxtwitter_video_is_listed_with_its_file_and_length():
    [status] = [s for s in load("x_fx_search_latest.json")["results"] if s["id"] == "2115884941993283956"]
    assert status_record(status)["media"] == [VIDEO]


def test_fxtwitter_post_without_media_has_an_empty_list():
    assert status_record(load("x_fx_status_reply.json")["status"])["media"] == []


def test_fxtwitter_quote_carries_its_own_media():
    status = load("x_fx_status.json")["status"]
    status["quote"] = {"id": "42", "text": "watch this", "author": {"screen_name": "bob"},
                       "media": {"all": [{"type": "video", "url": "https://video.twimg.com/v.mp4", "duration": 15.597}]}}
    assert status_record(status)["quoted"]["media"] == [
        {"type": "video", "url": "https://video.twimg.com/v.mp4", "duration": 15.597}]


@pytest.mark.parametrize("media", [None, [], "x", {"all": None}, {"all": [None, {"type": "photo"}, {"url": "u"}]}])
def test_fxtwitter_media_of_an_unexpected_shape_lists_nothing(media):
    status = load("x_fx_status.json")["status"]
    status["media"] = media
    assert status_record(status)["media"] == []


def test_fxtwitter_media_keeps_the_posts_order_and_names_a_gif():
    status = load("x_fx_status.json")["status"]
    status["media"] = {"all": [{"type": "video", "url": "https://video.twimg.com/a.mp4", "duration": 15.597},
                               {"type": "photo", "url": "https://pbs.twimg.com/media/b.jpg"},
                               {"type": "gif", "url": "https://video.twimg.com/tweet_video/c.mp4"}]}
    assert status_record(status)["media"] == [
        {"type": "video", "url": "https://video.twimg.com/a.mp4", "duration": 15.597},
        {"type": "photo", "url": "https://pbs.twimg.com/media/b.jpg", "duration": None},
        {"type": "gif", "url": "https://video.twimg.com/tweet_video/c.mp4", "duration": None}]


@pytest.mark.parametrize("entry", [
    {"type": "mosaic", "url": "https://pbs.twimg.com/m.jpg"},  # not a photo, video or GIF
    {"type": ["photo"], "url": "https://pbs.twimg.com/m.jpg"},
    {"type": "photo", "url": ""},
    {"type": "photo", "url": "/media/b.jpg"},
    {"type": "video", "url": "ftp://video.twimg.com/a.mp4"},
    {"type": "video", "url": None},
])
def test_fxtwitter_entry_of_another_type_or_without_a_web_address_is_left_out(entry):
    status = load("x_fx_status.json")["status"]
    status["media"] = {"all": [entry]}
    assert status_record(status)["media"] == []


@pytest.mark.parametrize("kind, duration", [
    ("photo", 5), ("video", float("inf")), ("video", float("nan")), ("video", True), ("video", "15"),
    ("video", 10 ** 400),  # an integer no float holds
])
def test_fxtwitter_length_is_null_on_a_photo_and_when_it_is_not_a_finite_number(kind, duration):
    status = load("x_fx_status.json")["status"]
    status["media"] = {"all": [{"type": kind, "url": "https://video.twimg.com/a", "duration": duration}]}
    assert status_record(status)["media"] == [{"type": kind, "url": "https://video.twimg.com/a", "duration": None}]


def test_read_with_an_infinite_length_still_prints_strict_json(run_x):
    body = json.dumps(load("x_fx_status.json")).replace('"width": 855', '"duration": 1e999, "width": 855').encode()
    assert b"1e999" in body
    code, env, out = run_x(["read", "2117770299454177696"], FakeWeb(fx=[(200, body)]))
    assert code == 0 and "Infinity" not in out
    assert env["items"][0]["raw"]["media"][0]["duration"] is None


def test_syndication_video_and_quoted_media_are_listed():
    data = load("x_syndication.json")
    video = {"type": "video", "media_url_https": "https://pbs.twimg.com/amplify_video_thumb/1/img/t.jpg",
             "video_info": {"duration_millis": 15597, "variants": [
                 {"content_type": "application/x-mpegURL", "url": "https://video.twimg.com/v.m3u8"},
                 {"bitrate": 832000, "content_type": "video/mp4", "url": "https://video.twimg.com/v360.mp4"}]}}
    data["mediaDetails"].append(video)
    data["quoted_tweet"] = {"id_str": "42", "text": "look", "user": {"screen_name": "bob"},
                            "mediaDetails": [{"type": "photo", "media_url_https": "https://pbs.twimg.com/media/q.jpg"}]}
    post = syndication_record(data)
    assert post["media"] == [
        {"type": "photo", "url": "https://pbs.twimg.com/media/HWMgzhNaEAAwev1.png", "duration": None},
        {"type": "video", "url": "https://video.twimg.com/v360.mp4", "duration": 15.597}]
    assert post["quoted"]["media"] == [{"type": "photo", "url": "https://pbs.twimg.com/media/q.jpg", "duration": None}]


def test_syndication_photo_is_listed_in_media():
    post = syndication_record(load("x_syndication.json"))
    assert post["media"] == [{"type": "photo", "duration": None,
                              "url": "https://pbs.twimg.com/media/HWMgzhNaEAAwev1.png"}]


def test_read_puts_media_in_the_item_raw(run_x):
    code, env, _ = run_x(["read", "2117770299454177696"], FakeWeb(fx=[(200, load("x_fx_status.json"))]))
    assert code == 0
    assert [m["type"] for m in env["items"][0]["raw"]["media"]] == ["photo"]


def test_syndication_maps_to_record_without_guessing_reply_count():
    post = syndication_record(load("x_syndication.json"))
    assert post["id"] == "2117770299454177696"
    assert post["author"] == "bkdgiffug"
    assert post["created_at"] == "2026-11-04T00:06:00Z"
    assert post["likes"] == 9 and post["replies"] is None
    assert post["source"] == "syndication"


# --- read via `rsrc x read` -----------------------------------------------------

def test_read_returns_one_item_per_argument_in_order(run_x):
    fake = FakeWeb(fx=[(200, load("x_fx_status.json")), (200, load("x_fx_status_reply.json"))])
    code, env, _ = run_x(["read", "2117770299454177696", "https://x.com/d/status/2114300044249890906"], fake)
    assert code == 0
    assert [i["id"] for i in env["items"]] == ["2117770299454177696", "2114300044249890906"]


def test_read_reports_unresolvable_post_and_continues(run_x):
    fake = FakeWeb(fx=[(404, b""), (200, load("x_fx_status_reply.json"))], syndication=[(404, b"")])
    code, env, _ = run_x(["read", "111111111", "2114300044249890906"], fake)
    assert code == 0 and env["error"] is None
    [failure] = env["meta"]["failed"]
    assert set(failure) == {"input", "code", "message"} and failure["input"] == "111111111"
    assert "fxtwitter" in failure["message"] and "syndication" in failure["message"]
    assert [i["id"] for i in env["items"]] == ["2114300044249890906"]


def test_dropped_connection_becomes_a_failure_and_read_continues(run_x):
    import http.client

    calls = []

    def transport(method, url, headers, body):
        calls.append(url)
        if len(calls) <= 4:
            raise http.client.IncompleteRead(b"partial")
        return 200, json.dumps(load("x_fx_status_reply.json")).encode()

    code, env, _ = run_x(["read", "111111111", "2114300044249890906"], transport)
    assert code == 0
    assert [f["input"] for f in env["meta"]["failed"]] == ["111111111"]
    assert [i["id"] for i in env["items"]] == ["2114300044249890906"]


def test_unicode_line_separators_in_text_keep_one_envelope_line(run_x):
    status = load("x_fx_status.json")
    status["status"]["text"] = "before\u2028middle\u2029after"
    code, env, _ = run_x(["read", "2117770299454177696"], FakeWeb(fx=[(200, status)]))  # run_x asserts one line
    assert code == 0
    assert env["items"][0]["text"] == env["items"][0]["raw"]["text"] == "before\u2028middle\u2029after"


# --- failed reads carry their error code ---------------------------------------

DOWN = OSError("connection reset")


@pytest.mark.parametrize("fx, syndication, code", [
    ([(404, b"")] * 3, [(404, b"")], "not_found"),
    ([(404, b"")] * 3, [(200, {})], "not_found"),  # 200 without a post
    ([DOWN] * 3, [(404, b"")], "network_error"),
    ([(404, b"")] * 3, [DOWN], "network_error"),
    ([(500, b"")] * 3, [(503, b"")], "upstream_error"),
    ([(500, b"")] * 3, [(200, b"<html>")], "upstream_error"),
])
def test_failed_read_carries_the_code_its_fallback_decided(fx, syndication, code):
    client = Client(min_interval=1, transport=FakeWeb(fx, syndication), sleep=lambda s: None)
    record = read(client, "2117770299454177696")
    assert record["id"] == "2117770299454177696" and record["code"] == code
    assert record["error"].startswith("fxtwitter: ") and "; syndication: " in record["error"]


def test_read_of_a_non_post_argument_is_a_usage_failure_without_requests():
    web = FakeWeb()
    record = read(Client(min_interval=1, transport=web, sleep=lambda s: None), "hello")
    assert record == {"id": "hello", "error": "not a post URL or ID", "code": "usage"}
    assert web.urls == []


@pytest.mark.parametrize("reason, label", [
    ("deleted", "post deleted"), ("suspended", "author suspended"), ("private", "protected account")])
def test_corroborated_tombstone(reason, label):
    fx = {"status": {"type": "tombstone", "provider": "twitter", "reason": reason}}
    record = read(Client(min_interval=1, transport=FakeWeb([(404, fx)], [(404, b"")]), sleep=lambda s: None), "12345")
    assert record == {"id": "12345", "code": "not_found", "error": label}


def test_syndication_post_wins_over_false_missing():
    doc = load("x_syndication.json")
    web = FakeWeb([(404, {})], [(200, doc)])
    assert read(Client(min_interval=1, transport=web, sleep=lambda s: None), doc["id_str"]) == syndication_record(doc)
    assert len(web.urls) == 2


def test_unrecognized_syndication_object_is_not_missing():
    web = FakeWeb([(404, {})], [(200, {"errors": ["temporary failure"]})])
    assert read(Client(min_interval=1, transport=web, sleep=lambda s: None), "12345")["code"] == "upstream_error"


def test_future_post_ids_are_impossible_without_network():
    import time
    future = int(time.time() * 1000) + 7 * 86_400_000
    for sequence in (0, 4194303):
        post = str(((future - 1288834974657) << 22) + sequence)
        assert read(Client(min_interval=1, transport=FakeWeb()), post)["code"] == "not_found"
