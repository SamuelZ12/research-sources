"""Search, read posts, read replies and read an account's timeline without login: FxTwitter API
v2; `read` falls back to X's syndication endpoint.

API: https://github.com/FxEmbed/FxEmbed/blob/main/docs/specs/fxtwitter-openapi.json
"""

import json
import re
from datetime import datetime, timezone
from typing import NamedTuple
from urllib.parse import quote, urlencode

from rsrc.channels.x.record import legacy_media, make_record, media_entry, to_int
from rsrc.channels.x.search import check_status
from rsrc.http import HttpError, NetworkError, NotFound

FX_API = "https://api.fxtwitter.com/2"
SYNDICATION = "https://cdn.syndication.twimg.com/tweet-result?id={id}&token=a"
HEADERS = {"user-agent": "Mozilla/5.0"}
FX_TRIES = 3  # FxTwitter intermittently 404s valid posts
SEARCH_PAGE_MAX = 100  # FxTwitter's `count` limit for /2/search
FEEDS = {"Latest": "latest", "Top": "top"}
POST_URL_RE = re.compile(r"(?:x|twitter)\.com/(?:[^/?#]+/status(?:es)?|i/web/status)/(\d{2,20})")
BARE_ID_RE = re.compile(r"^\d{2,20}$")
TIMELINE_PAGE_MAX = 100  # FxTwitter's `count` limit for /2/profile/{handle}/statuses
TIMELINE_MAX = 200  # posts per `x timeline`: about 10 requests
ACCOUNT_RE = re.compile(r"^(?:(?:https?://)?(?:www\.|mobile\.)?(?:x|twitter)\.com/)?@?([A-Za-z0-9_]{1,15})"
                        r"(?:/(?:with_replies|media|highlights|articles|about))?/?(?:[?#].*)?$", re.IGNORECASE)
# First path segments of x.com pages that are not accounts.
NOT_ACCOUNTS = frozenset({"i", "home", "explore", "search", "settings", "notifications", "messages",
                          "hashtag", "intent", "share", "compose", "login", "signup", "tos", "privacy"})


def parse_id(arg: str) -> str | None:
    match = POST_URL_RE.search(arg)
    if match:
        return match.group(1)
    return arg if BARE_ID_RE.match(arg) else None


def parse_handle(arg: str) -> str | None:
    """The handle in `handle`, `@handle` or an x.com/twitter.com account URL, without the @."""
    match = ACCOUNT_RE.match(arg.strip())
    if not match or match.group(1).lower() in NOT_ACCOUNTS:
        return None
    return match.group(1)


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


def read(client, arg, *, syndication_client=None) -> dict:
    """Try the independent syndication endpoint immediately when FxTwitter reports absence."""
    post_id = parse_id(arg)
    if not post_id:
        return {"id": arg, "error": "not a post URL or ID", "code": "usage"}
    # Snowflake IDs place the millisecond timestamp above 22 worker/sequence bits.
    timestamp_ms = (int(post_id) >> 22) + 1288834974657
    if timestamp_ms > datetime.now(timezone.utc).timestamp() * 1000 + 86_400_000:
        return {"id": post_id, "code": "not_found",
                "error": "The post ID encodes a creation date in the future according to the system UTC clock"}
    url = f"{FX_API}/status/{post_id}"
    data, fx_failure = fx_json(client, url, "status", stop_on_missing=True)
    tombstone = None
    if data:
        status = data["status"]
        if not isinstance(status, dict):
            fx_failure = Failure("unrecognized status")
        elif status.get("type") != "tombstone":
            return status_record(status)
        else:
            if status.get("id") in (None, post_id) and status.get("provider") == "twitter":
                tombstone = status.get("reason")
            fx_failure = Failure("post unavailable", status=404 if tombstone else None)
    data, failure = get_json(syndication_client or client, SYNDICATION.format(id=post_id))
    if data and data.get("id_str"):
        return syndication_record(data)
    reason = failure.reason if failure else "no post"
    message = f"fxtwitter: {fx_failure.reason}; syndication: {reason}"
    if fx_failure.status == 404 and (data == {} or (failure and failure.status == 404)):
        detail = {"deleted": "post deleted", "suspended": "author suspended", "private": "protected account"}
        return {"id": post_id, "code": "not_found", "error": detail.get(tombstone, message)}
    code = "network_error" if fx_failure.network or (failure and failure.network) else "upstream_error"
    return {"id": post_id, "error": message, "code": code}


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


def timeline(client, handle, limit=20):
    """Yield (post record, FxTwitter's status object) for up to `limit` of an account's newest
    posts, following FxTwitter's timeline cursor.

    A page holds about 20 posts whatever `count` asks for (probed 2026-11-21: 19 for count=100).
    A repost carries its original author, and `reposted_by` names the account asked for; the
    record gets that handle as `reposted_by`. Stops on a missing or repeated cursor or a page
    with no new posts.

    A 404 that outlasts the retries is an HttpError (`upstream_error`), never NotFound: the same
    body comes back for an unknown handle and for an existing account, and on 2026-11-21 about
    two in three requests for existing accounts got it (20 of 30; 2 of 6 accounts failed all
    three tries). `/2/profile/{handle}` did the same, so nothing here can confirm an account.
    """
    seen, cursor = set(), None
    while True:
        params = {"count": min(limit, TIMELINE_PAGE_MAX)} | ({"cursor": cursor} if cursor else {})
        url = f"{FX_API}/profile/{quote(handle, safe='')}/statuses?{urlencode(params)}"
        data, failure = fx_json(client, url, "results")
        if not data:
            if failure.status == 404 and not seen:
                raise HttpError(f"fxtwitter timeline {handle}: HTTP 404, which FxTwitter sends for an unknown "
                                "account and, often, for a real one. Check the spelling, rerun once after a "
                                f"minute, then use `rsrc x search 'from:{handle}'`")
            raise failure.error(f"fxtwitter timeline {handle}")
        added = 0
        for status in data["results"] if isinstance(data["results"], list) else []:
            if not isinstance(status, dict) or status.get("id") in seen:
                continue
            seen.add(status.get("id"))
            added += 1
            reposter = status.get("reposted_by")
            reposter = reposter.get("screen_name") if isinstance(reposter, dict) else None
            yield status_record(status) | ({"reposted_by": reposter} if reposter else {}), status
            if len(seen) >= limit:
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
        (rejected query, 401/403) fails at once.
        """
        params = {"q": query, "feed": FEEDS[product], "count": count} | ({"cursor": cursor} if cursor else {})
        url = f"{FX_API}/search?{urlencode(params)}"
        for _ in self.client.tries(range(FX_TRIES)):
            data, failure = get_json(self.client, url)
            if data and isinstance(data.get("results"), list):
                records = [status_record(s) for s in data["results"] if isinstance(s, dict)]
                return records, (data.get("cursor") or {}).get("bottom")
            failure = failure or Failure("response has no results")
            # FxTwitter is sent no cookies, so its 401/403 is upstream_error, never auth_failed.
            if failure.status is not None and 400 <= failure.status < 500 and failure.status != 404:
                break
        raise failure.error("fxtwitter search")


def fx_json(client, url, key, *, stop_on_missing=False) -> tuple[dict | None, Failure | None]:
    """FxTwitter GET with up to FX_TRIES attempts until the response has `key`."""
    for _ in client.tries(range(FX_TRIES)):
        data, failure = get_json(client, url, read_tombstone=stop_on_missing)
        if data and data.get(key) is not None:
            return data, None
        failure = failure or Failure(f"response has no {key}")
        if stop_on_missing and failure.status == 404:
            break
    return None, failure


def get_json(client, url, *, read_tombstone=False) -> tuple[dict | None, Failure | None]:
    """(data, None) on HTTP 200 with a JSON object, else (None, Failure). Raises RateLimited on 429."""
    try:
        status, body = client.request("GET", url, HEADERS)
    except NetworkError as e:
        return None, Failure(str(e), network=True)
    if status == 429:
        check_status(status, body)
    if status != 200:
        if read_tombstone and status in (403, 404):
            try:
                data = json.loads(body)
            except ValueError:
                data = None
            item = data.get("status") if isinstance(data, dict) else None
            if isinstance(item, dict) and item.get("type") == "tombstone":
                return data, None
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


def joined_iso(joined) -> str | None:
    """FxTwitter's `Tue Jan 29 07:56:05 +0000 2008` as `2008-01-29T07:56:05Z`; None if unreadable."""
    try:
        moment = datetime.strptime(joined, "%a %b %d %H:%M:%S %z %Y")
    except (TypeError, ValueError):
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _iso_from_timestamp(timestamp) -> str:
    return datetime.fromtimestamp(int(timestamp), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
