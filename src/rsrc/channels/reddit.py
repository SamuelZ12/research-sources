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
from shutil import which
from urllib.parse import urlencode, urljoin, urlsplit, quote

from rsrc import checks, proc
from rsrc import envelope as env
from rsrc.channels import reddit_archive
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
CLEANUP = Path(__file__).with_name("reddit_cleanup.js")

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
                 "collapsed", "is-mod-distinguished")

RATE_MARKERS = ("whoa there, pardner", "too many requests")
NOT_FOUND_MARKERS = ("page not found", "nobody on reddit goes by that name")

_POST_URL = re.compile(r"^https?://(?:(?:www|old|new|np|m|sh)\.)?reddit\.com/(?:(?:r|u|user)/[^/]+/)?"
                       r"comments/([a-z0-9]+)(?:/[^/?#]+(?:/([a-z0-9]+))?)?(?:/?\.(?:json|rss))?/?(?:[?#].*)?$",
                       re.IGNORECASE | re.ASCII)
_SHORT_URL = re.compile(r"^https?://redd\.it/([a-z0-9]+)/?(?:[?#].*)?$", re.IGNORECASE)
_SHARE_URL = re.compile(r"^https?://(?:(?:www|old|new|np|m|sh)\.)?reddit\.com(/(?:r|u|user)/[^/]+/s/[A-Za-z0-9]+)",
                        re.IGNORECASE)
_BARE_ID = re.compile(r"^(?:t3_)?([a-z0-9]{4,12})$", re.IGNORECASE)
_SUBREDDIT = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_COMMENT_TOMBSTONE = re.compile(
    r"\[(?:removed|deleted| removed by reddit )\]|comment (?:removed by moderator|deleted by user)", re.I)


def register(subparsers) -> None:
    parser = subparsers.add_parser("reddit", help="Reddit search and threads",
                                   description="Uses Ego Lite for search and live reads, or Arctic Shift for archived reads.")
    actions = parser.add_subparsers(dest="action", metavar="ACTION", required=True)

    search = actions.add_parser("search", help="search posts")
    search.add_argument("query")
    search.add_argument("--subreddit", help="search one subreddit (name without r/)")
    search.add_argument("--time", choices=TIMES, default="month", help="time range (default month)")
    search.add_argument("--sort", choices=SEARCH_SORTS, default="relevance", help="default relevance")
    search.add_argument("--limit", type=int, default=25, help=f"results, 1-{MAX_LIMIT} (default 25)")
    search.add_argument("--with-comments", type=int, nargs="?", const=3, metavar="N",
                        help="include the first three unpinned Best top-level comments on each of the first N threads (default 3, max 5)")
    search.set_defaults(run=run_search, action="search")

    read = actions.add_parser("read", help="one thread: the post and its top comments")
    read.add_argument("post", help="post or comment URL, share link, redd.it link, t3_ID or ID")
    read.add_argument("--sort", choices=READ_SORTS, default="top", help="comment order (default top)")
    read.add_argument("--comments", type=int, default=50, help=f"comments, 0-{MAX_COMMENTS} (default 50)")
    read.add_argument("--backend", choices=(BACKEND, reddit_archive.BACKEND),
                      help="default: use Ego Lite when installed, otherwise read the Arctic Shift archive")
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
    selected = posts[:count]
    rows = {}
    urls, indexes = [], []
    for index, post in enumerate(selected):
        url = post["url"] or ""
        match = _POST_URL.match(url)
        if not match or "t3_" + match[1].lower() != post["id"]:
            rows[index] = {"error": "url_mismatch"}
        else:
            urls.append(BASE + urlsplit(url).path + "?sort=confidence")
            indexes.append(index)

    items, failed = [], []
    meta["failed"] = failed
    stop = None
    if urls:
        batch, problem, attempt = _load(ctx, {
            "action": "batch", "urls": urls, "block_markers": checks.BLOCK_MARKERS,
            "rate_markers": RATE_MARKERS, "attrs": {"post": POST_ATTRS, "comment": COMMENT_ATTRS, "preview": True},
        })
        if problem:
            meta["attempts"].append(attempt)
            stop = env.error(*problem)
            for index in indexes:
                rows[index] = {"error": problem[0], "message": problem[1]}
        else:
            rows.update(zip(indexes, batch["rows"], strict=True))

    for index, post in enumerate(posts):
        post["raw"]["kind"] = "post"
        items.append(post)
        if index not in rows:
            continue
        row = rows[index]
        if row.get("cancelled"):
            meta["attempts"].append(env.attempt(BACKEND, "cancelled", 1))
            continue
        if "error" in row:
            reason = row["error"]
            problem = reason, row.get("message") or f"{reason} reading comments for {post['id']}"
        else:
            comments, problem = best_comments(row["page"], post["id"])
            items.extend(comments)
            skipped = row["page"].get("skipped_pinned", 0)
            if not problem and skipped:
                noun = "comment" if skipped == 1 else "comments"
                meta.setdefault("notes", []).append(f"{post['id']}: skipped {skipped} pinned {noun}")
            if not problem and len(comments) < 3:
                reason = ("all comments accounted for" if row["page"].get("shortfall") == "complete"
                          else "completeness unconfirmed at the 15 s deadline")
                meta.setdefault("notes", []).append(
                    f"{post['id']}: {len(comments)} of 3 readable top-level comments ({reason})")
        if index in indexes and "ms" in row:
            meta["attempts"].append(env.attempt(BACKEND, problem[0] if problem else "ok", 1, row["ms"]))
        if problem:
            code, message = problem
            failed.append({"input": post["url"] or post["id"], "code": code, "message": message})
            if code in ("blocked", "rate_limited", "auth_failed"):
                stop = env.error(code, message)
                meta.setdefault("notes", []).append(
                    f"{len(urls)} thread pages had already been requested when Reddit stopped the batch")
    if failed and not stop and len(failed) == len(selected):
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
    if not page.get("comments") and (page.get("shortfall") or _int(attrs.get("comment-count")) == 0):
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
    backend = args.backend or (BACKEND if which("ego-browser") else reddit_archive.BACKEND)
    if backend == reddit_archive.BACKEND:
        if share:
            return env.usage(CHANNEL, "read", "Arctic Shift read needs a /comments/ URL or post ID; "
                             "share links are only supported by the Ego Lite backend")
        if args.sort != "top":
            return env.usage(CHANNEL, "read", "Arctic Shift supports only --sort top (archived score)")
    if share:
        target, failure, attempt = share_post(ctx, BASE + share.group(1))
        meta["attempts"].append(attempt)
        if failure:
            return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta, error=env.error(*failure))
    else:
        target = parse_post(args.post)
        if target is None:
            return env.usage(CHANNEL, "read", f"not a Reddit post URL or ID: {args.post.strip()!r}")
    post_id, comment_id = target
    if comment_id:
        input_["comment_id"] = comment_id
    if backend == reddit_archive.BACKEND:
        return reddit_archive.read(ctx, input_, post_id)

    sort = "confidence" if args.sort == "best" else args.sort
    comment = f"t1_{comment_id}" if comment_id else None
    path = f"/comments/{post_id}/"
    if comment:
        path += f"comment/{comment_id}/"
    url = f"{BASE}{path}?{urlencode({'sort': sort})}"
    page, failure, attempt = _load(ctx, {"action": "read", "url": url, "comment": comment,
                                         "attrs": {"post": POST_ATTRS, "comment": COMMENT_ATTRS}})
    meta["attempts"].append(attempt)
    if not failure and comment:
        failure = access_failure(page)
    if failure:
        attempt["outcome"] = failure[0]
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta, error=env.error(*failure))
    if not page.get("post"):
        code, message = page_failure(page, "read")
        if comment and code == "not_found":
            if page.get("status") == 200:
                code, message = "url_mismatch", f"Reddit did not resolve {comment} under t3_{post_id}"
            else:
                message = f"no such Reddit comment: {comment}"
        attempt["outcome"] = code
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta, error=env.error(code, message))
    post = post_item(page["post"])
    if post["id"] != f"t3_{post_id}":
        attempt["outcome"] = "url_mismatch"
        meta["rejected"] = [{"url": post["url"], "reason": "url_mismatch"}]
        asked = f"{comment} under t3_{post_id}" if comment else f"t3_{post_id}"
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta,
                            error=env.error("url_mismatch", f"asked for {asked}, page shows {post['id']}"))
    if page["post"].get("removal_notice"):
        meta["notes"] = [page["post"]["removal_notice"]]
    post["raw"]["kind"] = "post"
    records = page.get("comments") or []
    limit = args.comments
    if comment:
        records, failure = focused_comments(records, post["id"], comment, page.get("comment_tree"))
        if failure:
            attempt["outcome"] = failure[0]
            # The post is verified, so it stays in `items` unless the comment is another post's.
            items = [] if failure[0] == "url_mismatch" else [post]
            return env.envelope(CHANNEL, "read", input_, BACKEND, items, meta=meta, error=env.error(*failure))
        limit = max(1, limit)
        meta.setdefault("notes", []).append(f"Focused on comment {comment}.")
    comments = [comment_item(record) for record in records[:limit]]
    for item in comments:
        item["raw"]["kind"] = "comment"
    if comment:
        comments[0]["raw"]["linked"] = True
    return env.envelope(CHANNEL, "read", input_, BACKEND, [post, *comments], meta=meta)


def focused_comments(records, post_id, comment_id, tree_rendered=None):
    """The linked comment, then the readable comments above and below it in page order, or a failure.
    `tree_rendered` says whether the page had a comment section when the wait for the comment ended."""
    by_id = {}
    replies = {}
    for record in records:
        attrs = record.get("attrs") or {}
        id_ = attrs.get("thingid")
        if id_ == comment_id and attrs.get("postid") != post_id:
            return [], ("url_mismatch", f"{comment_id} does not belong to {post_id} on the returned page")
        if id_ and attrs.get("postid") == post_id and id_ not in by_id:
            by_id[id_] = record
            replies.setdefault(attrs.get("parentid"), []).append(id_)
    linked = by_id.get(comment_id)
    text = (linked.get("text") or "").strip() if linked else ""
    if not linked and tree_rendered:
        # A missing comment's page has no comment section at all, so this one was still loading.
        return [], ("timeout", f"Reddit showed the post but had not rendered {comment_id} within 15 s")
    if not linked or _COMMENT_TOMBSTONE.fullmatch(text):
        return [], ("not_found", f"Reddit did not show a readable comment for {comment_id}")
    if not text:
        return [], ("upstream_error", f"Reddit showed {comment_id} without a readable body")

    context = {comment_id}
    parent = linked["attrs"].get("parentid")
    while parent in by_id and parent not in context:
        context.add(parent)
        parent = by_id[parent]["attrs"].get("parentid")
    pending = list(replies.get(comment_id, []))
    while pending:
        reply = pending.pop()
        if reply not in context:
            context.add(reply)
            pending.extend(replies.get(reply, []))
    # A comment whose body has not rendered links its replies above but is not worth a --comments slot.
    return [linked, *(record for id_, record in by_id.items()
                      if id_ != comment_id and id_ in context and (record.get("text") or "").strip())], None


# --- inputs ----------------------------------------------------------------------

def search_url(query, subreddit, time_range, sort) -> str:
    params = {"q": query, "type": "posts", "t": time_range, "sort": sort}
    if subreddit:
        params["restrict_sr"] = "1"
        return f"{BASE}/r/{quote(subreddit)}/search/?{urlencode(params)}"
    return f"{BASE}/search/?{urlencode(params)}"


def parse_post(value: str) -> tuple[str, str | None] | None:
    value = value.strip()
    post = _POST_URL.match(value)
    if post:
        return _post_target(post)
    for pattern in (_SHORT_URL, _BARE_ID):
        match = pattern.match(value)
        if match:
            return match.group(1).lower(), None
    return None


def _post_target(match) -> tuple[str, str | None]:
    post_id, comment_id = match.groups()
    return post_id.lower(), comment_id.lower() if comment_id else None


def share_post(ctx, url: str) -> tuple[tuple[str, str | None] | None, tuple[str, str] | None, dict]:
    """Return the target, error and attempt for a share link."""
    client = Client(min_interval=1, transport=ctx.redirect, sleep=ctx.sleep)
    started = time.monotonic()
    try:
        status, location = client.request("GET", url, SHARE_HEADERS)
    except NetworkError as e:
        return None, ("network_error", str(e)), env.attempt(SHARE_BACKEND, "network_error", 1, env.elapsed_ms(started))
    # Only a redirect to a post URL counts: the header is not user input, so no bare IDs.
    post = _POST_URL.match(urljoin(url, location.decode("utf-8", "replace"))) if 300 <= status < 400 else None
    if post:
        return _post_target(post), None, env.attempt(SHARE_BACKEND, "ok", 1, env.elapsed_ms(started))
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
    guarded = input_.get("comment_search") or input_["action"] == "batch"
    if guarded:
        input_["owner_pid"] = os.getpid()
    started = time.monotonic()
    try:
        result = (ctx.runner or proc.run)(["ego-browser", "nodejs", "-e", script(input_)], TIMEOUT)
    except KeyboardInterrupt:
        if guarded:
            _finish_abandoned(ctx, input_["space"])
        raise
    except proc.Missing:
        return None, ("config", "ego-browser not found; install Ego Lite and keep it running"), \
            env.attempt(BACKEND, "config")
    except proc.Timeout:
        if guarded:
            _finish_abandoned(ctx, input_["space"])
        return None, ("timeout", f"ego-browser did not finish within {TIMEOUT} s"), \
            env.attempt(BACKEND, "timeout", ms=env.elapsed_ms(started))
    ms = env.elapsed_ms(started)
    if guarded and result.code != 0:
        _finish_abandoned(ctx, input_["space"])
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


def _finish_abandoned(ctx, name):
    cleanup = f"const INPUT = {json.dumps({'space': name})};\n" + CLEANUP.read_text()
    (ctx.runner or proc.run)(["ego-browser", "nodejs", "-e", cleanup], 15)


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
