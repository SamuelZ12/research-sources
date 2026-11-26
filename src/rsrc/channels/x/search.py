"""X web GraphQL SearchTimeline with session cookies.

Request shape, headers and feature flags are ported from @steipete/bird v0.8.0
(lib/twitter-client-base.js, lib/twitter-client-search.js, lib/twitter-client-features.js):
Copyright (c) 2025 Peter Steinberger. MIT License.
"""

import json
import re
import secrets
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from rsrc.channels.x.record import legacy_media, make_record, to_int
from rsrc.http import AuthError, HttpError, RateLimited

API = "https://x.com/i/api/graphql"
QUERY_IDS = ("6AAys3t42mosm_yTI_QENg", "M1jEez78PEfVfbQLvlWMvQ", "5h0kNbk3ii97rmfY6CdgAA", "Tp1sewRU1AsZpBWhqCZicQ")
PAGE_SIZE = 20
# X's public web-client bearer token (the same for every logged-out and logged-in browser).
BEARER = "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
FEATURES = {
    "rweb_video_screen_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": True,
    "verified_phone_label_enabled": False,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "responsive_web_graphql_exclude_directive_enabled": True,
    "responsive_web_graphql_skip_user_profile_image_extensions_enabled": False,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": False,
    "responsive_web_grok_annotations_enabled": False,
    "responsive_web_jetfuel_frame": True,
    "post_ctas_fetch_enabled": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "tweet_awards_web_tipping_enabled": False,
    "responsive_web_grok_show_grok_translated_post": False,
    "responsive_web_grok_analysis_button_from_backend": True,
    "creator_subscriptions_quote_tweet_preview_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "rweb_video_timestamps_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": True,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": False,
    "articles_preview_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}


class SearchError(HttpError):
    def __init__(self, message, query_id_rejected=False):
        super().__init__(message)
        self.query_id_rejected = query_id_rejected


class Searcher:
    """Pages through SearchTimeline, surviving X's query-ID rotation.

    ID order: cached, then baked. The ID that works is cached. If all are rejected, the
    error explains how to copy the current ID from a browser into the cache file: x.com's
    logged-out page no longer ships the bundles that Bird's automatic discovery scraped.
    """

    page_size = PAGE_SIZE

    def __init__(self, client, cookies, cache_path: Path):
        self.client = client
        self.cookies = cookies
        self.cache_path = cache_path
        self.query_id = None

    def _fetch(self, query, product, cursor, query_id, count):
        return fetch_page(self.client, self.cookies, query, product, cursor, query_id, count)

    def page(self, query, product="Latest", cursor=None, count=PAGE_SIZE):
        cached = _read_cache(self.cache_path)
        candidates = [self.query_id] if self.query_id else list(dict.fromkeys(filter(None, (cached, *QUERY_IDS))))
        for query_id in self.client.tries(candidates):
            try:
                result = self._fetch(query, product, cursor, query_id, count)
            except SearchError as e:
                if not e.query_id_rejected:
                    raise
                rejected = e
                continue
            self._remember(query_id, cached)
            return result
        if query_id != candidates[-1]:
            raise SearchError(
                f"{rejected}. --max-requests stopped rotation before all SearchTimeline query IDs were tried."
            ) from rejected
        raise SearchError(
            f"{rejected}; every SearchTimeline query ID was rejected (X rotated it). Fix: in a browser "
            "logged in to x.com, open DevTools > Network, run a search, copy <id> from the request "
            f"/i/api/graphql/<id>/SearchTimeline, and write {{\"SearchTimeline\": \"<id>\"}} to {self.cache_path}")

    def _remember(self, query_id, cached):
        self.query_id = query_id
        if query_id != cached:
            try:
                self.cache_path.parent.mkdir(parents=True, exist_ok=True)
                self.cache_path.write_text(json.dumps({"SearchTimeline": query_id}) + "\n")
            except OSError:
                pass  # the cache only saves requests; a read-only home must not break search


def windows(since: date, until: date, days: int) -> list[tuple[date, date]]:
    """Newest-first [start, end) windows covering [since, until)."""
    out, end = [], until
    while end > since:
        start = max(since, end - timedelta(days=days))
        out.append((start, end))
        end = start
    return out


def default_until(now: datetime | None = None) -> date:
    """Tomorrow in UTC: `until` is exclusive, so this includes today everywhere."""
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).date() + timedelta(days=1)


def window_queries(query, since, until, days) -> list[tuple[str, date | None, date | None]]:
    """(query, start, end) per window; `end` is exclusive, either bound may be None.

    Bounds go to X as `since_time:`/`until_time:` (UTC midnight, Unix seconds): X ignored `until:`
    live but honored `until_time:` to the second.
    """
    if since is None:
        return [(f"{query} until_time:{_epoch(until)}", None, until) if until else (query, None, None)]
    return [(f"{query} since_time:{_epoch(start)} until_time:{_epoch(end)}", start, end)
            for start, end in windows(since, until, days)]


def _epoch(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def collect(searcher, windowed, per_window, product="Latest", stats=None, limit=None):
    """Yield up to `per_window` new posts per window, deduplicated across all windows.

    `searcher` is this module's Searcher or `fx.Searcher`: `page(query, product, cursor, count)`
    returns (records, next_cursor), and `page_size` caps `count`.

    Posts whose UTC date falls outside the window (should X stop honoring the date operators) are
    dropped and counted in `stats["dropped"]`; paging continues past them.
    A window stops paging on a missing or repeated cursor or a page with no posts it hasn't seen
    (Bird's rules). Dropped posts are not marked seen, so a later window can still keep them.
    """
    stats = stats if stats is not None else {}
    stats.setdefault("dropped", 0)
    seen = set()
    for query, start, end in windowed:
        got, cursor, window_seen = 0, None, set()
        while got < per_window:
            count = min(searcher.page_size, per_window - got)
            if limit:
                count = min(count, limit - len(seen))
            records, next_cursor = searcher.page(query, product, cursor, count)
            fresh = False
            for record in records:
                if record["id"] in window_seen:
                    continue
                window_seen.add(record["id"])
                fresh = True
                if not _in_window(record["created_at"], start, end):
                    stats["dropped"] += 1
                    continue
                if record["id"] in seen:
                    continue
                seen.add(record["id"])
                got += 1
                yield record
                if limit and len(seen) >= limit:
                    return
                if got >= per_window:
                    break
            if got >= per_window:
                reason = "per-window limit reached"
            elif not next_cursor:
                reason = "no more result pages"
            elif next_cursor == cursor:
                reason = "backend repeated its cursor"
            elif not fresh:
                reason = "page contained no new posts"
            else:
                cursor = next_cursor
                continue
            if limit:
                stats.setdefault("ends", []).append(reason)
            break


def _in_window(created_at, start, end) -> bool:
    if not created_at:
        return True  # undated: nothing to judge by
    day = date.fromisoformat(created_at[:10])
    return (start is None or day >= start) and (end is None or day < end)


QUERY_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def query_id_cache(environ) -> Path:
    """Where the working SearchTimeline query ID is cached: $XDG_CACHE_HOME (else ~/.cache)/rsrc/x-query-ids.json."""
    return Path(environ.get("XDG_CACHE_HOME") or "~/.cache").expanduser() / "rsrc" / "x-query-ids.json"


def _read_cache(path: Path) -> str | None:
    try:
        query_id = json.loads(path.read_text()).get("SearchTimeline")
    except (OSError, ValueError, AttributeError):
        return None
    return query_id if isinstance(query_id, str) and QUERY_ID_RE.match(query_id) else None


def headers(cookies) -> dict:
    auth_token, ct0 = cookies
    return {
        "accept": "*/*",
        "accept-language": "en-US,en;q=0.9",
        "authorization": f"Bearer {BEARER}",
        "x-csrf-token": ct0,
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "x-client-uuid": str(uuid.uuid4()),
        "x-twitter-client-deviceid": str(uuid.uuid4()),
        "x-client-transaction-id": secrets.token_hex(16),
        "cookie": f"auth_token={auth_token}; ct0={ct0}",
        "content-type": "application/json",
        "user-agent": USER_AGENT,
        "origin": "https://x.com",
        "referer": "https://x.com/",
    }


def fetch_page(client, cookies, query, product="Latest", cursor=None, query_id=QUERY_IDS[0], count=PAGE_SIZE):
    """One SearchTimeline page → (records, next_cursor)."""
    variables = {"rawQuery": query, "count": count, "querySource": "typed_query", "product": product}
    if cursor:
        variables["cursor"] = cursor
    url = f"{API}/{query_id}/SearchTimeline?" + urlencode({"variables": json.dumps(variables)})
    body = json.dumps({"features": FEATURES, "queryId": query_id}).encode()
    status, raw = client.request("POST", url, headers(cookies), body)
    if status == 404:
        raise SearchError(f"HTTP 404 for query ID {query_id}", query_id_rejected=True)
    data = _json(raw) if status in (400, 422) else None
    if data and _validation_failed(data):
        raise SearchError(f"HTTP {status}: query ID {query_id} rejected", query_id_rejected=True)
    check_status(status, raw)
    data = _json(raw)
    if data.get("errors"):
        message = ", ".join(e.get("message", "unknown error") for e in data["errors"])
        raise SearchError(message, query_id_rejected=_validation_failed(data))
    return parse_page(data)


def parse_page(data) -> tuple[list[dict], str | None]:
    timeline = (((data.get("data") or {}).get("search_by_raw_query") or {}).get("search_timeline") or {}).get("timeline") or {}
    records, seen, cursor = [], set(), None
    for instruction in timeline.get("instructions") or []:
        # Later pages may deliver the Bottom cursor in a TimelineReplaceEntry's single `entry`.
        entries = (instruction.get("entries") or []) + ([instruction["entry"]] if instruction.get("entry") else [])
        for entry in entries:
            content = entry.get("content") or {}
            if content.get("cursorType") == "Bottom" and content.get("value"):
                cursor = content["value"]
            for result in _entry_results(content):
                record = to_record(result)
                if record and record["id"] not in seen:
                    seen.add(record["id"])
                    records.append(record)
    return records, cursor


SHORT_LINK = re.compile(r"https?://t\.co/[A-Za-z0-9_-]+")
MEDIA_PERMALINK = re.compile(r"https?://(?:www\.)?(?:x|twitter)\.com/(?:[^/]+|i/web)/status/"
                            r"(\d+)/(?:photo|video)/\d+")


def expand_links(text, urls, media=(), post_id=None):
    """Use only mappings X supplied. A media permalink can belong to a different post."""
    if not text or "t.co/" not in text:
        return text
    replacements = {}
    for entity in urls:
        target = entity.get("expanded_url")
        if isinstance(target, str) and target.startswith(("https://", "http://")):
            replacements[entity.get("url")] = target
    for entity in media:
        target = entity.get("expanded_url")
        if isinstance(target, str) and target.startswith(("https://", "http://")):
            attachment = MEDIA_PERMALINK.fullmatch(target)
            replacements[entity.get("url")] = "" if attachment and attachment[1] == post_id else target
    expanded = SHORT_LINK.sub(lambda m: replacements.get(m[0], m[0]), text)
    if any(link and not target and text.rstrip().endswith(link) for link, target in replacements.items()):
        return expanded.rstrip()
    return expanded


def to_record(result):
    result = _unwrap(result)
    if not result or not result.get("rest_id"):
        return None
    user = ((result.get("core") or {}).get("user_results") or {}).get("result") or {}
    author = (user.get("core") or {}).get("screen_name") or (user.get("legacy") or {}).get("screen_name")
    legacy = result.get("legacy") or {}
    note = ((result.get("note_tweet") or {}).get("note_tweet_results") or {}).get("result") or {}
    text = note.get("text") or legacy.get("full_text")
    if not author or not text:
        return None
    text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    original = _unwrap((legacy.get("retweeted_status_result") or {}).get("result")) or {}
    original_legacy = original.get("legacy") or {}
    original_links = (original_legacy.get("entities") or {}).get("urls") or []
    links = (legacy.get("entities") or {}).get("urls") or []
    note_links = (note.get("entity_set") or {}).get("urls") or []
    quoted = to_record((result.get("quoted_status_result") or {}).get("result"))
    entities = legacy.get("extended_entities") if isinstance(legacy.get("extended_entities"), dict) else {}
    return make_record(
        id=result["rest_id"],
        author=author,
        created_at=_iso(legacy.get("created_at")),
        text=expand_links(text, [*original_links, *links, *note_links], entities.get("media") or [], result["rest_id"]),
        text_truncated=bool(result.get("note_tweet")) and not note.get("text"),
        likes=to_int(legacy.get("favorite_count")),
        reposts=to_int(legacy.get("retweet_count")),
        replies=to_int(legacy.get("reply_count")),
        quotes=to_int(legacy.get("quote_count")),
        views=to_int((result.get("views") or {}).get("count")),
        lang=legacy.get("lang"),
        reply_to_id=legacy.get("in_reply_to_status_id_str"),
        conversation_id=legacy.get("conversation_id_str"),
        quoted={"id": quoted["id"], "author": quoted["author"], "text": quoted["text"],
                "media": quoted["media"], "text_truncated": False} if quoted else None,
        media=legacy_media(entities.get("media")),
        source="x-graphql",
    )


def _entry_results(content):
    yield ((content.get("itemContent") or {}).get("tweet_results") or {}).get("result")
    for item in content.get("items") or []:
        yield (((item.get("item") or {}).get("itemContent") or {}).get("tweet_results") or {}).get("result")


def _unwrap(result):
    if result and result.get("__typename") == "TweetWithVisibilityResults":
        return result.get("tweet")
    return result


def _iso(created_at):
    if not created_at:
        return None
    parsed = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _validation_failed(data) -> bool:
    for error in data.get("errors") or []:
        if (error.get("extensions") or {}).get("code") == "GRAPHQL_VALIDATION_FAILED":
            return True
        if "rawQuery" in (error.get("path") or []) and "must be defined" in (error.get("message") or "").lower():
            return True
    return False


def check_status(status: int, body: bytes) -> None:
    if 200 <= status < 300:
        return
    if status in (401, 403):
        raise AuthError(f"HTTP {status}: X rejected the cookies; refresh AUTH_TOKEN/CT0 from a logged-in browser")
    if status == 429:
        raise RateLimited("HTTP 429: rate limited; stop and retry later")
    raise HttpError(f"HTTP {status}: {body[:200].decode('utf-8', 'replace')}")


def _json(raw: bytes) -> dict:
    try:
        data = json.loads(raw)
    except ValueError:
        raise HttpError(f"unexpected non-JSON response: {raw[:200].decode('utf-8', 'replace')}") from None
    return data if isinstance(data, dict) else {}
