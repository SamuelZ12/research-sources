"""Search, read posts and read replies without login: FxTwitter API v2; `read` falls back to X's
syndication endpoint.

API: https://github.com/FxEmbed/FxEmbed/blob/main/docs/specs/fxtwitter-openapi.json
"""

import json
import re
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import urlencode

from rsrc.channels.x.record import legacy_media, make_record, media_entry, to_int
from rsrc.channels.x.search import check_status
from rsrc.http import CapReached, HttpError, NetworkError, NotFound

FX_API = "https://api.fxtwitter.com/2"
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result?id={id}&token=a"
HEADERS = {"user-agent": "Mozilla/5.0"}
FX_TRIES = 3  # FxTwitter intermittently 404s valid posts
SEARCH_PAGE_MAX = 100  # FxTwitter's `count` limit for /2/search
FEEDS = {"Latest": "latest", "Top": "top"}
POST_URL_RE = re.compile(r"(?:x|twitter)\.com/(?:[^/?#]+/status(?:es)?|i/web/status)/(\d{2,20})")
BARE_ID_RE = re.compile(r"^\d{2,20}$")


def parse_id(arg: str) -> str | None:
    match = POST_URL_RE.search(arg)
    if match:
        return match.group(1)
    return arg if BARE_ID_RE.match(arg) else None


class Failure(NamedTuple):
    """Why a GET gave no usable JSON object. `reason` is the text shown to the agent; error codes
    and retry rules are decided from `status` (the HTTP status, when the server answered with one
    other than 200) and `network` (no response arrived), never from the text."""

    reason: str
    status: int | None = None
    network: bool = False

    def error(self, what: str, missing: bool = False) -> HttpError:
        """The exception for failing to fetch `what`; `missing` makes a 404 mean it doesn't exist."""
        message = f"{what}: {self.reason}"
        if self.network:
            return NetworkError(message)
        if missing and self.status == 404:
            return NotFound(message)
        return HttpError(message)


def read(client, arg) -> dict:
    """One record for a post URL or ID, or {"id", "error", "code"} if it can't be resolved.

    `code` is the envelope error code. Syndication is the deciding fallback: if it answered 404
    or 200 without a post, the post doesn't exist or isn't public (`not_found`); if no response
    arrived, `network_error`; anything else (5xx, non-JSON, unexpected shape) is `upstream_error`.
    """
    post_id = parse_id(arg)
    if not post_id:
        return {"id": arg, "error": "not a post URL or ID", "code": "usage"}
    data, fx_failure = fx_json(client, f"{FX_API}/status/{post_id}", "status")
    if data:
        return status_record(data["status"])
    data, failure = get_json(client, SYNDICATION.format(id=post_id))
    if data and data.get("id_str"):
        return syndication_record(data)
    reason = failure.reason if failure else "no post"  # no failure: a 200 without a post
    if failure is None or failure.status == 404:
        code = "not_found"
    elif failure.network:
        code = "network_error"
    else:
        code = "upstream_error"
    return {"id": post_id, "error": f"fxtwitter: {fx_failure.reason}; syndication: {reason}", "code": code}


def replies(client, post_id, rank="likes", limit=100):
    """Yield up to `limit` replies to a post, following FxTwitter's conversation cursor.

    Stops on a missing or repeated cursor or a page with no new replies. A 404 that outlasts the
    retries raises NotFound: no such post.
    """
    seen, cursor = {post_id}, None
    while True:
        params = {"ranking_mode": rank} | ({"cursor": cursor} if cursor else {})
        data, failure = fx_json(client, f"{FX_API}/conversation/{post_id}?{urlencode(params)}", "replies")
        if not data:
            raise failure.error(f"fxtwitter conversation {post_id}", missing=True)
        added = 0
        for status in data["replies"]:
            record = status_record(status)
            if record["id"] in seen:
                continue
            seen.add(record["id"])
            added += 1
            yield record
            if len(seen) - 1 >= limit:
                return
        next_cursor = (data.get("cursor") or {}).get("bottom")
        if not next_cursor or next_cursor == cursor or added == 0:
            return
        cursor = next_cursor


class Searcher:
    """FxTwitter `GET /2/search`, with the same `page()` contract as `search.Searcher`.

    The query (operators included) goes to X through FxTwitter's own accounts, so no cookies.
    """

    page_size = SEARCH_PAGE_MAX

    def __init__(self, client):
        self.client = client

    def page(self, query, product="Latest", cursor=None, count=SEARCH_PAGE_MAX):
        """One results page → (records, next_cursor).

        429 raises RateLimited. 404 is retried like a 5xx: no results is a 200 with `results: []`
        (probed 2026-11-11), so a lasting 404 is an outage, not an empty search. Any other 4xx
        (rejected query, 401/403) fails at once. A retry the request cap cuts off raises the
        failure it was retrying, so the outage never reads as an empty, capped search.
        """
        params = {"q": query, "feed": FEEDS[product], "count": count} | ({"cursor": cursor} if cursor else {})
        url = f"{FX_API}/search?{urlencode(params)}"
        failure = None
        for _ in range(FX_TRIES):
            try:
                data, failure = get_json(self.client, url)
            except CapReached:
                if failure is None:
                    raise
                break
            if data and isinstance(data.get("results"), list):
                records = [status_record(s) for s in data["results"] if isinstance(s, dict)]
                return records, (data.get("cursor") or {}).get("bottom")
            failure = failure or Failure("response has no results")
            # FxTwitter is sent no cookies, so its 401/403 is upstream_error, never auth_failed.
            if failure.status is not None and 400 <= failure.status < 500 and failure.status != 404:
                break
        raise failure.error("fxtwitter search")


def fx_json(client, url, key) -> tuple[dict | None, Failure | None]:
    """FxTwitter GET with up to FX_TRIES attempts until the response has `key`."""
    failure = None
    for _ in range(FX_TRIES):
        data, failure = get_json(client, url)
        if data and data.get(key) is not None:
            return data, None
        failure = failure or Failure(f"response has no {key}")
    return None, failure


def get_json(client, url) -> tuple[dict | None, Failure | None]:
    """(data, None) on HTTP 200 with a JSON object, else (None, Failure). Raises RateLimited on 429."""
    try:
        status, body = client.request("GET", url, HEADERS)
    except NetworkError as e:
        return None, Failure(str(e), network=True)
    if status == 429:
        check_status(status, body)
    if status != 200:
        return None, Failure(f"HTTP {status}", status=status)
    try:
        data = json.loads(body)
    except ValueError:
        return None, Failure("non-JSON response")
    return (data, None) if isinstance(data, dict) else (None, Failure("unexpected response"))


def status_record(status: dict) -> dict:
    author = (status.get("author") or {}).get("screen_name")
    quote = status.get("quote") if isinstance(status.get("quote"), dict) else None
    timestamp = status.get("created_timestamp")
    return make_record(
        id=status.get("id"),
        url=status.get("url"),
        author=author,
        created_at=_iso_from_timestamp(timestamp) if timestamp else None,
        text=status.get("text"),
        likes=to_int(status.get("likes")),
        reposts=to_int(status.get("reposts")),
        replies=to_int(status.get("replies")),
        quotes=to_int(status.get("quotes")),
        views=to_int(status.get("views")),
        lang=status.get("lang"),
        reply_to_id=(status.get("replying_to") or {}).get("status"),
        quoted={"id": quote.get("id"), "author": (quote.get("author") or {}).get("screen_name"),
                "text": quote.get("text"), "media": fx_media(quote)} if quote else None,
        media=fx_media(status),
        source="fxtwitter",
    )


def fx_media(status: dict) -> list[dict]:
    """The post's photos, videos and GIFs from FxTwitter's `media.all`, in its order. An entry of
    another type, or without an http(s) address, is left out."""
    media = status.get("media") if isinstance(status.get("media"), dict) else {}
    entries = media.get("all") if isinstance(media.get("all"), list) else []
    records = [media_entry(e.get("type"), e.get("url"), e.get("duration")) for e in entries if isinstance(e, dict)]
    return [r for r in records if r]


def syndication_record(data: dict) -> dict:
    """Syndication truncates long posts' text; counts other than likes aren't provided."""
    quoted = data.get("quoted_tweet") if isinstance(data.get("quoted_tweet"), dict) else None
    created = data.get("created_at")
    return make_record(
        id=data["id_str"],
        author=(data.get("user") or {}).get("screen_name"),
        created_at=created[:19] + "Z" if created else None,
        text=data.get("text"),
        likes=to_int(data.get("favorite_count")),
        lang=data.get("lang"),
        reply_to_id=data.get("in_reply_to_status_id_str"),
        quoted={"id": quoted.get("id_str"), "author": (quoted.get("user") or {}).get("screen_name"),
                "text": quoted.get("text"), "media": legacy_media(quoted.get("mediaDetails"))} if quoted else None,
        media=legacy_media(data.get("mediaDetails")),
        source="syndication",
    )


def _iso_from_timestamp(timestamp) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
