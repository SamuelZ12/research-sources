"""`rsrc x`: search, read posts and read replies on X, wrapped in the envelope.

`fx` (FxTwitter, no login) and `search` (X's web GraphQL, session cookies) do the fetching; this
module parses the arguments, runs the action and maps records and exceptions into envelopes.
Their exception messages never contain cookies or request headers, so they are passed through
as `error.message` unchanged.
"""

import argparse
import re
import time
from datetime import date

from rsrc import checks
from rsrc import envelope as env
from rsrc.channels.x import fx, search
from rsrc.channels.x.auth import CookieError, load_cookies
from rsrc.http import CapReached, Client, HttpError

CHANNEL = "x"
FX_BACKEND = "fxtwitter"
BACKENDS = (FX_BACKEND, "x-graphql")
ENGAGEMENT = ("likes", "reposts", "replies", "quotes", "views")


class UsageError(Exception):
    code = "usage"


def register(subparsers) -> None:
    parser = subparsers.add_parser("x", help="X posts: search, read, replies (no login by default)",
                                   description="X: search, read posts, replies.")
    commands = parser.add_subparsers(dest="x_command", required=True, metavar="ACTION")
    runners = {"search": run_search, "read": run_read, "replies": run_replies}
    for name, sub in add_commands(commands).items():
        sub.set_defaults(run=runners[name], action=name)


# --- arguments -----------------------------------------------------------------

def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def iso_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from None


def add_commands(commands) -> dict[str, argparse.ArgumentParser]:
    """Add search/read/replies to a subparsers object; return {"search": p, "read": p, "replies": p}."""
    parsers = {}
    parsers["search"] = p = commands.add_parser(
        "search", help="search X posts (FxTwitter, no login; --backend x-graphql uses X session cookies)")
    p.add_argument("query", help='X search query, e.g. \'"Letta Code" -filter:replies min_faves:5\'')
    p.add_argument("--since", type=iso_date, help="start date YYYY-MM-DD (inclusive); enables date windows")
    p.add_argument("--until", type=iso_date,
                   help="end date YYYY-MM-DD (exclusive); default: tomorrow UTC when --since is given")
    p.add_argument("--window-days", type=positive_int, default=7, help="days per window (default 7)")
    p.add_argument("--per-window", type=positive_int, default=20,
                   help="max posts per window, or in total without --since (default 20)")
    p.add_argument("--backend", choices=BACKENDS, default="fxtwitter",
                   help="fxtwitter: FxTwitter's public API, no login (default); "
                        "x-graphql: X's web search with your AUTH_TOKEN/CT0 cookies")
    p.add_argument("--top", action="store_true", help="X's Top ranking instead of Latest")
    p.add_argument("--max-requests", type=positive_int, default=25,
                   help="hard cap on HTTP requests for this command (default 25)")

    parsers["read"] = p = commands.add_parser("read", help="read posts by URL or ID (no login)")
    p.add_argument("posts", nargs="+", metavar="URL_OR_ID", help="x.com/twitter.com status URL or numeric ID")

    parsers["replies"] = p = commands.add_parser("replies", help="replies to a post (no login)")
    p.add_argument("post", metavar="URL_OR_ID", help="x.com/twitter.com status URL or numeric ID")
    p.add_argument("--rank", choices=("likes", "recency"), default="likes", help="reply order (default likes)")
    p.add_argument("--limit", type=positive_int, default=100, help="max replies (default 100)")
    return parsers


# --- runs ----------------------------------------------------------------------

def run_search(args, ctx) -> dict:
    backend = args.backend
    input = {"query": args.query, "since": _iso(args.since), "until": _iso(args.until),
             "window_days": args.window_days, "per_window": args.per_window, "top": args.top,
             "backend": backend, "max_requests": args.max_requests}
    client = Client(transport=ctx.transport, max_requests=args.max_requests, sleep=ctx.sleep)
    items = []
    started = time.monotonic()
    try:
        dropped, capped = search_posts(args, client, ctx.environ, lambda record: items.append(to_item(record)))
    except (UsageError, CookieError, HttpError) as e:
        code = error_code(e)
        # Usage and cookie errors happen before any request: no backend call to report.
        attempts = [] if code in ("usage", "config") else [_attempt(backend, code, client, started)]
        return env.envelope(CHANNEL, "search", input, backend, items,
                            meta={"attempts": attempts}, error=env.error(code, str(e)))
    meta = {"attempts": [_attempt(backend, "ok" if items else "no_results", client, started)]}
    if capped:
        meta["truncated"] = "request_cap"
    if dropped:
        meta["notes"] = [f"{dropped} post(s) outside their date window dropped"]
    return env.envelope(CHANNEL, "search", input, backend, items, meta=meta)


def search_posts(args, client, environ, sink) -> tuple[int, bool]:
    """Run a search from parsed `search` args, passing each record to `sink(record)`.

    Returns (posts dropped for falling outside their date window, whether the request cap stopped the search).
    Raises UsageError (bad arguments), CookieError (missing cookies, x-graphql only) and HttpError
    (incl. AuthError, RateLimited) as they occur.
    """
    if (args.since or args.until) and re.search(r"\b(since|until)(_time)?:", args.query):
        raise UsageError("the query already contains since:/until:/since_time:/until_time:; "
                         "drop them or drop --since/--until")
    until = args.until or (search.default_until() if args.since else None)
    if args.since and args.since >= until:
        raise UsageError("--since must be before --until")
    windowed = search.window_queries(args.query, args.since, until, args.window_days)
    if args.backend == "x-graphql":
        cookies = load_cookies(environ)
        searcher = search.Searcher(client, cookies, search.query_id_cache(environ))
    else:
        searcher = fx.Searcher(client)
    stats, capped = {}, False
    try:
        for record in search.collect(searcher, windowed, args.per_window, "Top" if args.top else "Latest", stats):
            sink(record)
    except CapReached:
        capped = True
    return stats.get("dropped", 0), capped


def run_read(args, ctx) -> dict:
    input = {"posts": list(args.posts)}
    client = Client(transport=ctx.transport, sleep=ctx.sleep)
    items, attempts, rejected, failed = [], [], [], []
    failures = []  # (code, message) in input order, for the all-failed error
    meta = {"attempts": attempts, "rejected": rejected, "failed": failed}
    for arg in args.posts:
        started, before = time.monotonic(), client.requests_made
        try:
            record = fx.read(client, arg)
        except (HttpError, CapReached) as e:  # RateLimited (429) or the request cap: stop now
            code = error_code(e)
            attempts.append(env.attempt(FX_BACKEND, code, client.requests_made - before, env.elapsed_ms(started)))
            if code == "truncated":
                meta["truncated"] = "request_cap"
                break
            return env.envelope(CHANNEL, "read", input, FX_BACKEND, items, meta=meta,
                                error=env.error(code, str(e)))
        requests = client.requests_made - before
        if "error" in record:
            code = record["code"]
            attempts.append(env.attempt(FX_BACKEND, code, requests, env.elapsed_ms(started)))
            failed.append({"input": arg, "code": code, "message": record["error"]})
            failures.append((code, f"{arg}: {record['error']}"))
            continue
        attempts.append(env.attempt(record["source"], "ok", requests, env.elapsed_ms(started)))
        kept, rejection = checks.screen_read(to_item(record), fx.parse_id(arg))
        if rejection:
            rejected.append(rejection)
            if rejection["reason"] == "blocked_page":
                failures.append(("blocked", f"{arg}: the returned post is a block or login page"))
            else:
                failures.append(("url_mismatch", f"{arg}: returned post {record['id']} is not the requested post"))
            continue
        items.append(kept)
    error = None
    if not items and failures and meta.get("truncated") is None:
        error = env.error(*failures[0])
    return env.envelope(CHANNEL, "read", input, FX_BACKEND, items, meta=meta, error=error)


def run_replies(args, ctx) -> dict:
    input = {"post": args.post, "rank": args.rank, "limit": args.limit}
    post_id = fx.parse_id(args.post)
    if not post_id:
        return env.envelope(CHANNEL, "replies", input, FX_BACKEND,
                            error=env.error("usage", f"not a post URL or ID: {args.post}"))
    client = Client(transport=ctx.transport, sleep=ctx.sleep)
    items, meta = [], {}
    started = time.monotonic()
    try:
        for record in fx.replies(client, post_id, args.rank, args.limit):
            items.append(to_item(record))
    except (HttpError, CapReached) as e:
        code = error_code(e)
        if code != "truncated":
            meta["attempts"] = [_attempt(FX_BACKEND, code, client, started)]
            return env.envelope(CHANNEL, "replies", input, FX_BACKEND, items, meta=meta,
                                error=env.error(code, str(e)))
        meta["truncated"] = "request_cap"
    meta["attempts"] = [_attempt(FX_BACKEND, "ok" if items else "no_results", client, started)]
    return env.envelope(CHANNEL, "replies", input, FX_BACKEND, items, meta=meta)


# --- mapping -------------------------------------------------------------------

def to_item(record: dict) -> dict:
    counts = {k: record[k] for k in ENGAGEMENT if record.get(k) is not None}
    return env.item(id=record["id"], url=record["url"], title=None, author=record["author"],
                    published_at=record["created_at"], text=record["text"], source=record["source"],
                    engagement=counts or None, raw=record)


def error_code(e: Exception) -> str:
    """Envelope error code the exception carries ("truncated" for the request cap, not an error)."""
    return "truncated" if isinstance(e, CapReached) else e.code


def _attempt(backend, outcome, client, started) -> dict:
    return env.attempt(backend, outcome, client.requests_made, env.elapsed_ms(started))


def _iso(day):
    return day.isoformat() if day else None
