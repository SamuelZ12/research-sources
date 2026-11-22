"""`rsrc reddit`: search and threads, read from real pages in a background Ego tab.

Reddit blocks every non-browser client, with or without a login cookie (and in-page `fetch()`
of its JSON and fragment endpoints), with "You've been blocked by network security" (tested
2026-11-06), so each command runs `reddit_page.js` through `ego-browser nodejs`: one page load
in a throwaway task space, fields copied out of the DOM, one `RSRC_REDDIT <json>` line back.
This module maps that JSON.

A share link (`/r/<sub>/s/<code>`) names no post, so `read` first asks reddit.com where it leads:
one plain HTTP request that is not followed, which Reddit answered with a 301 to the post for a
browser user agent and with a 403 for `Mozilla/5.0` alone (probed 2026-11-21).
"""

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urljoin, urlsplit, quote

from rsrc import checks, proc
from rsrc import envelope as env
from rsrc.http import Client, NetworkError

CHANNEL = "reddit"
BACKEND = "ego-browser"
SOURCE = "reddit"
BASE = "https://www.reddit.com"
TIMEOUT = 120
MAX_SCROLLS = 4
MARKER = "RSRC_REDDIT "
STDERR_TAIL = 200
SHARE_BACKEND = "reddit-redirect"
SHARE_HEADERS = {"user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
SCRIPT = Path(__file__).with_name("reddit_page.js")

TIMES = ("hour", "day", "week", "month", "year", "all")
SEARCH_SORTS = ("relevance", "hot", "top", "new", "comments")
READ_SORTS = ("top", "best", "new", "controversial", "old", "qa")
MAX_LIMIT = 100
MAX_COMMENTS = 200

# Read by name so the viewer's own fields (user-id, user-logged-in) never reach the output.
POST_ATTRS = ("id", "permalink", "post-title", "author", "created-timestamp", "score", "comment-count",
              "subreddit-prefixed-name", "post-type", "domain", "content-href", "upvote-ratio",
              "item-state", "post-language")
COMMENT_ATTRS = ("thingid", "parentid", "postid", "author", "created", "score", "depth", "permalink",
                 "collapsed")

RATE_MARKERS = ("whoa there, pardner", "too many requests")
NOT_FOUND_MARKERS = ("page not found", "nobody on reddit goes by that name")

_POST_URL = re.compile(r"^https?://(?:(?:www|old|new|np|m|sh)\.)?reddit\.com/(?:(?:r|u|user)/[^/]+/)?"
                       r"comments/([a-z0-9]+)", re.IGNORECASE)
_SHORT_URL = re.compile(r"^https?://redd\.it/([a-z0-9]+)/?(?:[?#].*)?$", re.IGNORECASE)
_SHARE_URL = re.compile(r"^https?://(?:(?:www|old|new|np|m|sh)\.)?reddit\.com(/(?:r|u|user)/[^/]+/s/[A-Za-z0-9]+)",
                        re.IGNORECASE)
_BARE_ID = re.compile(r"^(?:t3_)?([a-z0-9]{4,12})$", re.IGNORECASE)
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")


def register(subparsers) -> None:
    parser = subparsers.add_parser("reddit", help="Reddit search and threads, via a background Ego tab",
                                   description="Loads real reddit.com pages in Ego Lite (must be running).")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    search = actions.add_parser("search", help="search posts")
    search.add_argument("query")
    search.add_argument("--subreddit", help="search one subreddit (name without r/)")
    search.add_argument("--time", choices=TIMES, default="month", help="time range (default month)")
    search.add_argument("--sort", choices=SEARCH_SORTS, default="relevance", help="default relevance")
    search.add_argument("--limit", type=int, default=25, help=f"results, 1-{MAX_LIMIT} (default 25)")
    search.add_argument("--with-comments", type=int, nargs="?", const=3, metavar="N",
                        help="include the first three Best top-level comments on each of the first N threads (default 3, max 5)")
    search.set_defaults(run=run_search, action="search")

    read = actions.add_parser("read", help="one thread: the post and its top comments")
    read.add_argument("post", help="post URL, share link, redd.it link, t3_ID or ID")
    read.add_argument("--sort", choices=READ_SORTS, default="top", help="comment order (default top)")
    read.add_argument("--comments", type=int, default=50, help=f"comments, 0-{MAX_COMMENTS} (default 50)")
    read.set_defaults(run=run_read, action="read")


# --- runs ------------------------------------------------------------------------

def run_search(args, ctx) -> dict:
    comment_threads = getattr(args, "with_comments", None)
    input_ = {"query": args.query, "subreddit": args.subreddit, "time": args.time, "sort": args.sort,
              "limit": args.limit}
    if comment_threads is not None:
        if not 1 <= comment_threads <= 5:
            return env.usage(CHANNEL, "search", "--with-comments must be between 1 and 5")
        input_["with_comments"] = comment_threads
    if not args.query.strip():
        return env.usage(CHANNEL, "search", "QUERY must not be empty")
    if not 1 <= args.limit <= MAX_LIMIT:
        return env.usage(CHANNEL, "search", f"--limit must be between 1 and {MAX_LIMIT}, got {args.limit}")
    subreddit = (args.subreddit or "").strip().strip("/").removeprefix("r/").removeprefix("R/") or None
    if subreddit and not _SUBREDDIT.match(subreddit):
        return env.usage(CHANNEL, "search", f"--subreddit {args.subreddit!r} is not a subreddit name")
    url = search_url(args.query, subreddit, args.time, args.sort)
    request = {"action": "search", "url": url, "limit": args.limit, "max_scrolls": MAX_SCROLLS}
    if comment_threads:
        request["comment_search"] = True
    page, failure, attempt = _load(ctx, request)
    meta = {"attempts": [attempt]}
    if failure:
        return env.envelope(CHANNEL, "search", input_, BACKEND, meta=meta, error=env.error(*failure))
    records = page.get("posts") or []
    access_error = access_failure(page) if comment_threads else None
    if page.get("lost_results") and not access_error:
        access_error = page_failure(page, "search")
    if not records:
        code, message = access_error or page_failure(page, "search")
        if page.get("search_page") and (not comment_threads or code == "upstream_error"):
            attempt["outcome"] = "no_results"
            return env.envelope(CHANNEL, "search", input_, BACKEND, meta=meta)
        attempt["outcome"] = code
        return env.envelope(CHANNEL, "search", input_, BACKEND, meta=meta, error=env.error(code, message))
    items, seen = [], set()
    for record in records:
        item = search_item(record)
        if item and item["id"] not in seen:
            seen.add(item["id"])
            items.append(item)
    # Results on the page but none readable: a markup change must fail, not read as an empty search.
    if not items:
        code, message = page_failure(page, "search")
        attempt["outcome"] = code
        return env.envelope(CHANNEL, "search", input_, BACKEND, meta=meta, error=env.error(code, message))
    items = items[:args.limit]
    if len(items) < args.limit:
        why = (f"stopped after {MAX_SCROLLS} scrolls" if page.get("loads", 0) > MAX_SCROLLS
               else "Reddit loaded no more results")
        meta["notes"] = [f"{len(items)} of --limit {args.limit}: {why}"]
    if access_error:
        attempt["outcome"] = access_error[0]
        return env.envelope(CHANNEL, "search", input_, BACKEND, items, meta=meta, error=env.error(*access_error))
    if comment_threads:
        return with_comments(ctx, input_, items, meta, comment_threads)
    return env.envelope(CHANNEL, "search", input_, BACKEND, items, meta=meta)


def with_comments(ctx, input_, posts, meta, count):
    items, failed = [], []
    meta["failed"] = failed
    stop = None
    for index, post in enumerate(posts):
        post["raw"]["kind"] = "post"
        items.append(post)
        if index >= count or stop:
            continue
        # Only navigate to a validated Reddit post. Use the canonical path to avoid a redirect.
        url = post["url"] or ""
        match = _POST_URL.match(url)
        browser_failed = False
        if not match or "t3_" + match[1].lower() != post["id"]:
            problem = ("url_mismatch", "search result permalink does not identify the returned post")
        else:
            url = BASE + urlsplit(url).path + "?sort=confidence"
            page, problem, attempt = _load(ctx, {"action": "comments", "url": url,
                                                 "attrs": {"post": POST_ATTRS, "comment": COMMENT_ATTRS,
                                                           "preview": True}})
            meta["attempts"].append(attempt)
            browser_failed = bool(problem and problem[0] != "timeout")
            if not problem:
                comments, problem = best_comments(page, post["id"])
                items.extend(comments)
            if problem:
                attempt["outcome"] = problem[0]
        if problem:
            code, message = problem
            failed.append({"input": post["url"] or post["id"], "code": code, "message": message})
            if browser_failed or code in ("blocked", "rate_limited", "auth_failed", "config"):
                stop = env.error(code, message)
    if failed and not stop and len(failed) == min(len(posts), count):
        stop = env.error(failed[0]["code"], failed[0]["message"])
    return env.envelope(CHANNEL, "search", input_, BACKEND, items, meta=meta, error=stop)


def access_failure(page):
    if page.get("status") in (401, 403, 429):
        status = page["status"]
        return "rate_limited" if status == 429 else "blocked", f"Reddit returned HTTP {status} at {page['url']}"
    return None


def best_comments(page, post_id):
    denied = access_failure(page)
    if denied:
        return [], denied
    thread = page.get("post")
    if not thread:
        return [], page_failure(page, "read")
    attrs = thread.get("attrs") or {}
    if attrs.get("id") != post_id:
        return [], ("url_mismatch", f"asked for {post_id}, page shows {attrs.get('id')}")
    if thread.get("removal_notice"):
        return [], ("not_found", thread["removal_notice"])
    if not page.get("comments") and _int(attrs.get("comment-count")) == 0:
        return [], None
    if page.get("comment_sort") not in ("Sort by Best", "Sort by Best (Default)"):
        return [], ("upstream_error", f"could not confirm Reddit's Best comment order on {post_id}")
    comments = []
    for record in page.get("comments") or []:
        attrs = record.get("attrs") or {}
        if attrs.get("postid") != post_id:
            return [], ("url_mismatch", f"comment belongs to {attrs.get('postid')}, expected {post_id}")
        if attrs.get("depth") == "0" and record.get("text") and attrs.get("thingid"):
            item = comment_item(record)
            link = _POST_URL.match(item["url"] or "")
            if not link or "t3_" + link[1].lower() != post_id:
                return [], ("url_mismatch", f"comment permalink does not identify {post_id}")
            item["raw"]["kind"] = "comment"
            comments.append(item)
            if len(comments) == 3:
                break
    if not comments:
        return [], ("upstream_error", f"no readable top-level comments on {post_id}")
    return comments, None


def run_read(args, ctx) -> dict:
    input_ = {"post": args.post, "sort": args.sort, "comments": args.comments}
    if not 0 <= args.comments <= MAX_COMMENTS:
        return env.usage(CHANNEL, "read", f"--comments must be between 0 and {MAX_COMMENTS}, got {args.comments}")
    meta = {"attempts": []}
    share = _SHARE_URL.match(args.post.strip())
    if share:
        post_id, failure, attempt = share_post(ctx, BASE + share.group(1))
        meta["attempts"].append(attempt)
        if failure:
            return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta, error=env.error(*failure))
    else:
        post_id, problem = parse_post(args.post)
        if problem:
            return env.usage(CHANNEL, "read", problem)
    sort = "confidence" if args.sort == "best" else args.sort
    url = f"{BASE}/comments/{post_id}/?{urlencode({'sort': sort})}"
    page, failure, attempt = _load(ctx, {"action": "read", "url": url,
                                         "attrs": {"post": POST_ATTRS, "comment": COMMENT_ATTRS}})
    meta["attempts"].append(attempt)
    if failure:
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta, error=env.error(*failure))
    if not page.get("post"):
        code, message = page_failure(page, "read")
        attempt["outcome"] = code
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta, error=env.error(code, message))
    post = post_item(page["post"])
    if post["id"] != f"t3_{post_id}":
        attempt["outcome"] = "url_mismatch"
        meta["rejected"] = [{"url": post["url"], "reason": "url_mismatch"}]
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta,
                            error=env.error("url_mismatch", f"asked for t3_{post_id}, page shows {post['id']}"))
    if page["post"].get("removal_notice"):
        meta["notes"] = [page["post"]["removal_notice"]]
    comments = [comment_item(c) for c in page.get("comments") or []][:args.comments]
    return env.envelope(CHANNEL, "read", input_, BACKEND, [post, *comments], meta=meta)


# --- inputs ----------------------------------------------------------------------

def search_url(query, subreddit, time_range, sort) -> str:
    params = {"q": query, "type": "posts", "t": time_range, "sort": sort}
    if subreddit:
        params["restrict_sr"] = "1"
        return f"{BASE}/r/{quote(subreddit)}/search/?{urlencode(params)}"
    return f"{BASE}/search/?{urlencode(params)}"


def parse_post(value: str) -> tuple[str | None, str | None]:
    """(base36 post id, None) or (None, usage message)."""
    value = value.strip()
    for pattern in (_POST_URL, _SHORT_URL, _BARE_ID):
        match = pattern.match(value)
        if match:
            return match.group(1).lower(), None
    return None, f"not a Reddit post URL or ID: {value!r}"


def share_post(ctx, url: str) -> tuple[str | None, tuple[str, str] | None, dict]:
    """(post id, None, attempt) or (None, (code, message), attempt): the post a share link leads to."""
    client = Client(transport=ctx.redirect, sleep=ctx.sleep)
    started = time.monotonic()
    try:
        status, location = client.request("GET", url, SHARE_HEADERS)
    except NetworkError as e:
        return None, ("network_error", str(e)), env.attempt(SHARE_BACKEND, "network_error", 1, env.elapsed_ms(started))
    # Only a redirect to a post URL counts: the header is not user input, so no bare IDs.
    post = _POST_URL.match(urljoin(url, location.decode("utf-8", "replace"))) if 300 <= status < 400 else None
    if post:
        return post.group(1).lower(), None, env.attempt(SHARE_BACKEND, "ok", 1, env.elapsed_ms(started))
    if status == 429:
        failure = "rate_limited", "Reddit rate-limited the share-link request (HTTP 429); stop and retry later"
    elif status == 404:
        failure = "not_found", f"no such share link: {url}"
    else:
        failure = "upstream_error", (f"the share link did not redirect to a post (HTTP {status}); "
                                     "open it in a browser and pass the /comments/ URL")
    return None, failure, env.attempt(SHARE_BACKEND, failure[0], 1, env.elapsed_ms(started))


# --- Ego -------------------------------------------------------------------------

def script(input_: dict) -> str:
    """The page script with its input as a JSON literal (never string-interpolated)."""
    return f"const INPUT = {json.dumps(input_)};\n" + SCRIPT.read_text()


def _load(ctx, input_: dict) -> tuple[dict | None, tuple[str, str] | None, dict]:
    """(page JSON, None, attempt) or (None, (code, message), attempt)."""
    input_ = {**input_, "space": f"rsrc reddit {os.getpid()}-{time.time_ns()}"}
    started = time.monotonic()
    try:
        result = (ctx.runner or proc.run)(["ego-browser", "nodejs", "-e", script(input_)], TIMEOUT)
    except proc.Missing:
        return None, ("config", "ego-browser not found; install Ego Lite and keep it running"), \
            env.attempt(BACKEND, "config")
    except proc.Timeout:
        # The kill skips the script's `finally`, so its task space stays open in Ego. The script's
        # own waits total about 80 s, so only a hung Ego gets here.
        return None, ("timeout", f"ego-browser did not finish within {TIMEOUT} s"), \
            env.attempt(BACKEND, "timeout", ms=env.elapsed_ms(started))
    ms = env.elapsed_ms(started)
    line = result_line(result, MARKER)
    page = None
    if line is not None:
        try:
            page = json.loads(line[len(MARKER):])
        except ValueError:
            page = None
    if result.code != 0 or not isinstance(page, dict):
        tail = output_tail(result, MARKER)
        message = f"ego-browser exited with code {result.code}" if result.code else "ego-browser printed no result"
        return None, ("upstream_error", f"{message}: {tail}" if tail else message), \
            env.attempt(BACKEND, "upstream_error", ms=ms)
    return page, None, env.attempt(BACKEND, "ok", page.get("loads"), ms)


def _lines(result: proc.Result) -> list[str]:
    """Output lines of both streams: ego-browser prints the script's console output on stderr.
    Split on "\\n" only: JSON.stringify leaves U+2028/U+2029/U+0085 unescaped, and
    str.splitlines() would cut a result line at them."""
    return [line.rstrip("\r") for line in (result.stdout + "\n" + result.stderr).split("\n")]


def result_line(result: proc.Result, marker: str) -> str | None:
    """The last output line starting with `marker`."""
    return next((line for line in reversed(_lines(result)) if line.startswith(marker)), None)


def output_tail(result: proc.Result, marker: str) -> str:
    """Last STDERR_TAIL chars of output, without `marker` lines (a page's worth of JSON)."""
    lines = [line for line in _lines(result) if line.strip() and not line.startswith(marker)]
    return "\n".join(lines)[-STDERR_TAIL:]


def page_failure(page: dict, action: str) -> tuple[str, str]:
    """Code and message for a page without the expected structure."""
    title, text = page.get("title") or "", page.get("page_text") or ""
    haystack = f"{title}\n{text[:checks.TEXT_WINDOW]}".lower()
    where = page.get("url") or "the page"
    # Reddit also puts its rate-limit greeting on the network-security block page.
    if "blocked by network security" in haystack:
        return "blocked", f"Reddit blocked access at {where}"
    if any(marker in haystack for marker in RATE_MARKERS):
        return "rate_limited", f"Reddit rate-limited the session at {where}; stop and retry later"
    if action == "read" and page.get("community_gate"):
        return "blocked", f"Reddit restricted access at {where}: {page['community_gate']}"
    if action == "read" and page.get("login_wall"):
        return "blocked", f"Reddit requires login at {where}"
    if checks.is_block_page(title, text):
        return "blocked", f"Reddit showed a block or login page at {where}"
    if action == "read" and any(marker in haystack for marker in NOT_FOUND_MARKERS):
        return "not_found", f"no such post at {where}"
    return "upstream_error", f"unrecognized {action} page at {where} (title {title!r}); Reddit's markup may have changed"


# --- mapping ---------------------------------------------------------------------

def search_item(record: dict) -> dict | None:
    context = record.get("context") or {}
    post = context.get("post") or {}
    if not post.get("id"):
        return None
    counts = {}
    for number, label in record.get("counts") or []:
        key = {"votes": "score", "vote": "score", "comments": "comments", "comment": "comments"}.get(label)
        value = _int(number)
        if key and value is not None:
            counts[key] = value
    href = record.get("href")
    return env.item(id=post["id"], url=_absolute(href), title=post.get("title"),
                    author=(context.get("profile") or {}).get("name"), published_at=_utc(record.get("ts")),
                    text=record.get("snippet"), source=SOURCE, engagement=counts or None, raw=record)


def post_item(post: dict) -> dict:
    attrs = post.get("attrs") or {}
    counts = {k: v for k, v in (("score", _int(attrs.get("score"))),
                                ("comments", _int(attrs.get("comment-count")))) if v is not None}
    return env.item(id=attrs.get("id"), url=_absolute(attrs.get("permalink")), title=attrs.get("post-title"),
                    author=attrs.get("author"), published_at=_utc(attrs.get("created-timestamp")),
                    text=post.get("text") or None, source=SOURCE, engagement=counts or None, raw=post)


def comment_item(comment: dict) -> dict:
    attrs = comment.get("attrs") or {}
    score = _int(attrs.get("score"))
    return env.item(id=attrs.get("thingid"), url=_absolute(attrs.get("permalink")), title=None,
                    author=attrs.get("author"), published_at=_utc(attrs.get("created")),
                    text=comment.get("text") or None, source=SOURCE,
                    engagement={"score": score} if score is not None else None, raw=comment)


def _absolute(path):
    if not path:
        return None
    return path if path.startswith("http") else BASE + path


def _utc(stamp):
    """Reddit's `2026-11-05T17:34:19.282000+0000` as `2026-11-05T17:34:19Z`; None if unreadable."""
    if not stamp:
        return None
    try:
        moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        try:
            moment = datetime.fromisoformat(stamp)
        except ValueError:
            return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
