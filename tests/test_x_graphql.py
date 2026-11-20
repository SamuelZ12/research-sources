import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from rsrc.channels.x.record import legacy_media
from rsrc.channels.x.search import check_status, fetch_page, parse_page
from rsrc.http import AuthError, Client, HttpError, RateLimited

FIXTURES = Path(__file__).parent / "fixtures"
COOKIES = ("TOKENSENTINEL", "CSRFSENTINEL")


def load(name):
    return json.loads((FIXTURES / name).read_text())


def by_id(records):
    return {r["id"]: r for r in records}


def tweet(rest_id="1", screen_name="alice", **extra):
    """Minimal GraphQL tweet result, shaped like the recorded fixtures."""
    result = {
        "__typename": "Tweet",
        "rest_id": rest_id,
        "core": {"user_results": {"result": {"core": {"screen_name": screen_name}}}},
        "legacy": {"full_text": "short text", "created_at": "Wed Nov 04 22:42:53 +0000 2026",
                   "conversation_id_str": rest_id},
    }
    result.update(extra)
    return result


def page(*results, cursor="next-cursor"):
    entries = [{"content": {"entryType": "TimelineTimelineItem", "itemContent": {"tweet_results": {"result": r}}}}
               for r in results]
    entries.append({"content": {"entryType": "TimelineTimelineCursor", "cursorType": "Bottom", "value": cursor}})
    return {"data": {"search_by_raw_query": {"search_timeline": {"timeline": {
        "instructions": [{"type": "TimelineAddEntries", "entries": entries}]}}}}}


# --- parsing recorded responses -------------------------------------------

def test_latest_page_parses_known_post_and_cursor():
    records, cursor = parse_page(load("x_graphql_latest.json"))
    assert len(records) == 20
    post = by_id(records)["2118111770803884396"]
    assert post["author"] == "kylelittle"
    assert post["url"] == "https://x.com/kylelittle/status/2118111770803884396"
    assert post["created_at"] == "2026-11-04T22:42:53Z"
    assert (post["likes"], post["reposts"], post["replies"]) == (4, 0, 0)  # as recorded; live counts drift
    assert isinstance(post["views"], int)
    assert post["source"] == "x-graphql"
    assert cursor


def test_replies_carry_parent_id():
    records, _ = parse_page(load("x_graphql_latest.json"))
    replies = [r for r in records if r["reply_to_id"]]
    assert len(replies) == 3
    assert all(r["conversation_id"] for r in replies)


def test_quoted_posts_are_attached():
    records, _ = parse_page(load("x_graphql_latest.json"))
    quoting = [r for r in records if r["quoted"]]
    assert len(quoting) == 2
    assert all(q["quoted"]["id"] and q["quoted"]["author"] and q["quoted"]["text"] for q in quoting)


def test_top_page_includes_conversation_module_posts_and_skips_prompts():
    records, _ = parse_page(load("x_graphql_top.json"))
    ids = [r["id"] for r in records]
    assert "2114300044249890906" in ids  # inside a search-conversation module
    assert len(ids) == len(set(ids)) == 22


# --- parsing edge cases ----------------------------------------------------

def test_long_post_uses_note_tweet_text():
    long = tweet(note_tweet={"note_tweet_results": {"result": {"text": "the full long text"}}})
    records, _ = parse_page(page(long))
    assert records[0]["text"] == "the full long text"


def test_visibility_wrapper_is_unwrapped():
    wrapped = {"__typename": "TweetWithVisibilityResults", "tweet": tweet("7", "bob")}
    records, _ = parse_page(page(wrapped))
    assert [(r["id"], r["author"]) for r in records] == [("7", "bob")]


def test_media_matches_what_fxtwitter_reports_for_the_same_post():
    records, _ = parse_page(load("x_graphql_latest.json"))
    assert by_id(records)["2115884941993283956"]["media"] == [
        {"type": "video", "duration": 63.6,
         "url": "https://video.twimg.com/amplify_video/2115884803920973824/vid/avc1/1920x1080/p06NTnUa24DU1xG8.mp4?tag=29"}]
    # Every media entity on the recorded page: 8 photos and 5 videos on the posts, 1 video on a quoted post.
    assert sorted(m["type"] for r in records for m in r["media"]) == ["photo"] * 8 + ["video"] * 5
    assert [m["type"] for r in records if r["quoted"] for m in r["quoted"]["media"]] == ["video"]


def test_post_without_media_entities_has_an_empty_list():
    records, _ = parse_page(page(tweet("3")))
    assert records[0]["media"] == []


def test_quoted_post_carries_its_own_media():
    photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/a.jpg"}
    quoted = tweet("8", "bob", legacy={"full_text": "look", "extended_entities": {"media": [photo]}})
    records, _ = parse_page(page(tweet("9", quoted_status_result={"result": quoted})))
    assert records[0]["quoted"]["media"] == [
        {"type": "photo", "url": "https://pbs.twimg.com/media/a.jpg", "duration": None}]


def mp4(url, bitrate):
    return {"bitrate": bitrate, "content_type": "video/mp4", "url": url}


def video(*variants, millis=63600):
    return {"type": "video", "media_url_https": "https://pbs.twimg.com/thumb.jpg",
            "video_info": {"duration_millis": millis, "variants": list(variants)}}


def test_video_file_is_the_highest_bitrate_mp4_wherever_it_is_listed():
    entry = video(mp4("https://video.twimg.com/hi.mp4", 10368000), mp4("https://video.twimg.com/lo.mp4", 256000))
    assert legacy_media([entry]) == [{"type": "video", "url": "https://video.twimg.com/hi.mp4", "duration": 63.6}]


@pytest.mark.parametrize("broken", [None, "", "/hi.mp4", 7])
def test_mp4_without_a_web_address_does_not_hide_a_playable_one(broken):
    entry = video(mp4(broken, 10368000), mp4("https://video.twimg.com/lo.mp4", 256000))
    assert legacy_media([entry]) == [{"type": "video", "url": "https://video.twimg.com/lo.mp4", "duration": 63.6}]


@pytest.mark.parametrize("bitrate", [float("inf"), 10 ** 400, "high", None, True])
def test_unusable_bitrate_ranks_last_instead_of_failing(bitrate):
    entry = video(mp4("https://video.twimg.com/odd.mp4", bitrate), mp4("https://video.twimg.com/lo.mp4", 256000))
    assert legacy_media([entry])[0]["url"] == "https://video.twimg.com/lo.mp4"


@pytest.mark.parametrize("millis", [float("inf"), 10 ** 400, "63600", None, True])
def test_unusable_length_is_null_instead_of_failing(millis):
    entry = video(mp4("https://video.twimg.com/lo.mp4", 256000), millis=millis)
    assert legacy_media([entry]) == [{"type": "video", "url": "https://video.twimg.com/lo.mp4", "duration": None}]


def test_media_entities_keep_the_posts_order():
    photo = {"type": "photo", "media_url_https": "https://pbs.twimg.com/media/a.jpg"}
    kinds = [m["type"] for m in legacy_media([video(mp4("https://video.twimg.com/v.mp4", 1)), photo])]
    assert kinds == ["video", "photo"]


@pytest.mark.parametrize("entities", [["media"], "media", 7, {"media": "x"}])
def test_media_block_of_an_unexpected_shape_lists_nothing_and_keeps_the_page(entities):
    odd = {"full_text": "short text", "extended_entities": entities}
    quoted = tweet("8", "bob", legacy=odd)
    records, _ = parse_page(page(tweet("9", legacy=odd, quoted_status_result={"result": quoted}), tweet("10")))
    assert [r["id"] for r in records] == ["9", "10"]
    assert records[0]["media"] == [] and records[0]["quoted"]["media"] == []


def test_gif_is_listed_by_its_mp4_without_a_length():
    gif = {"type": "animated_gif", "media_url_https": "https://pbs.twimg.com/tweet_video_thumb/a.jpg",
           "video_info": {"variants": [{"bitrate": 0, "content_type": "video/mp4",
                                        "url": "https://video.twimg.com/tweet_video/a.mp4"}]}}
    assert legacy_media([gif]) == [{"type": "gif", "url": "https://video.twimg.com/tweet_video/a.mp4", "duration": None}]


@pytest.mark.parametrize("entries", [
    None, "x", [None],
    [{"type": "poll"}],
    [{"type": "photo"}],  # no file URL
    [{"type": "video", "video_info": {"variants": [{"content_type": "application/x-mpegURL", "url": "u.m3u8"}]}}],
])
def test_media_entities_without_a_usable_file_are_left_out(entries):
    assert legacy_media(entries) == []


def test_tombstones_are_skipped():
    records, _ = parse_page(page({"__typename": "TweetTombstone"}, tweet("2")))
    assert [r["id"] for r in records] == ["2"]


def test_missing_fields_are_null_not_guessed():
    records, _ = parse_page(page(tweet("3")))
    post = records[0]
    assert post["views"] is None and post["lang"] is None and post["quoted"] is None
    assert post["likes"] is None and post["reply_to_id"] is None


def test_page_without_cursor_returns_none():
    data = page(tweet("4"))
    data["data"]["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"][0]["entries"].pop()
    assert parse_page(data)[1] is None


def test_later_pages_cursor_in_replace_entry_instruction_is_found():
    data = page(tweet("5"), cursor=None)
    timeline = data["data"]["search_by_raw_query"]["search_timeline"]["timeline"]
    timeline["instructions"].append({"type": "TimelineReplaceEntry", "entry_id_to_replace": "cursor-bottom-0",
                                     "entry": {"content": {"cursorType": "Bottom", "value": "NEXT"}}})
    records, cursor = parse_page(data)
    assert [r["id"] for r in records] == ["5"] and cursor == "NEXT"


# --- request shape (Bird v0.8.0) --------------------------------------------

def capture_request(response_body):
    seen = {}

    def transport(method, url, headers, body):
        seen.update(method=method, url=url, headers=headers, body=body)
        return 200, json.dumps(response_body).encode()

    return Client(transport=transport, min_interval=0), seen


def test_request_matches_bird_search_timeline_call():
    client, seen = capture_request(page(tweet("5")))
    fetch_page(client, COOKIES, '"Letta Code"', product="Top", cursor="abc", query_id="QID")

    url = urlsplit(seen["url"])
    assert seen["method"] == "POST"
    assert url.netloc == "x.com" and url.path == "/i/api/graphql/QID/SearchTimeline"
    variables = json.loads(parse_qs(url.query)["variables"][0])
    assert variables == {"rawQuery": '"Letta Code"', "count": 20, "querySource": "typed_query",
                         "product": "Top", "cursor": "abc"}
    body = json.loads(seen["body"])
    assert body["queryId"] == "QID" and body["features"]["view_counts_everywhere_api_enabled"] is True
    headers = seen["headers"]
    assert headers["x-csrf-token"] == "CSRFSENTINEL"
    assert headers["cookie"] == "auth_token=TOKENSENTINEL; ct0=CSRFSENTINEL"
    assert headers["authorization"].startswith("Bearer ")
    assert headers["x-twitter-auth-type"] == "OAuth2Session"


def test_graphql_errors_in_200_response_raise():
    client, _ = capture_request({"errors": [{"message": "Something broke"}]})
    with pytest.raises(Exception, match="Something broke"):
        fetch_page(client, COOKIES, "q", query_id="QID")


# --- HTTP status handling ----------------------------------------------------

def test_401_and_403_tell_the_user_to_refresh_cookies():
    for status in (401, 403):
        with pytest.raises(AuthError, match="refresh AUTH_TOKEN/CT0"):
            check_status(status, b"")


def test_429_says_stop_and_retry_later():
    with pytest.raises(RateLimited, match="rate limited"):
        check_status(429, b"")


def test_other_errors_include_status_and_body_excerpt():
    with pytest.raises(HttpError, match=r"HTTP 500: boom"):
        check_status(500, b"boom" + b"x" * 1000)


def test_2xx_passes():
    check_status(200, b"{}")


# --- `rsrc x search` ----------------------------------------------------------

def test_search_top_flag_requests_top_product(run_x, tmp_path):
    calls = []

    def transport(method, url, headers, body):
        calls.append(url)
        return 200, json.dumps(page(tweet("1"), cursor=None)).encode()

    environ = {"AUTH_TOKEN": COOKIES[0], "CT0": COOKIES[1], "RSRC_X_ENV": "/nonexistent",
               "XDG_CACHE_HOME": str(tmp_path)}
    code, env, _ = run_x(["search", "q", "--top", "--backend", "x-graphql"], transport, environ)
    assert code == 0 and env["input"]["top"] is True and len(calls) == 1
    assert json.loads(parse_qs(urlsplit(calls[0]).query)["variables"][0])["product"] == "Top"
