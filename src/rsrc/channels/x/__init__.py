"""`rsrc x`: search, read posts, read replies and read an account's timeline on X, wrapped in the
envelope.

`fx` (FxTwitter, no login) and `search` (X's web GraphQL, session cookies) do the fetching; this
module parses the arguments, runs the action and maps records and exceptions into envelopes.
A default search tries the backends of `auth.load_search_order` in turn.
Their exception messages never contain cookies or request headers, so they are passed through
as `error.message` unchanged.
"""

import argparse
import re
import shlex
import shutil
from contextlib import nullcontext
import time
from datetime import date

from rsrc import checks
from rsrc import envelope as env
from rsrc.channels.x import browser, fx, search
from rsrc.channels.x.auth import BACKENDS, FX_BACKEND, CookieError, load_cookies, load_search_order
from rsrc.channels.x.record import to_int
from rsrc.http import CapReached, Client, HttpError

CHANNEL = "x"
# Outcomes of one backend's search (as in meta.attempts) that hand the search to the next backend
# of the order. Never `rate_limited` (a rate limit stops the run), never `auth_failed` or `config`
# (the user has to fix the session) and never `no_results` (an empty search is an answer; an
# FxTwitter outage is a 404 or 503, so `upstream_error`).
FALLBACK_ON = frozenset({"upstream_error", "network_error"})
ENGAGEMENT = ("likes", "reposts", "replies", "quotes", "views")


class UsageError(Exception):
    code = "usage"


def register(subparsers) -> None:
    parser = subparsers.add_parser("x", help="X posts: search, read, replies, timeline (no login by default)",
                                   description="X: search, read posts, replies, an account's timeline.")
    commands = parser.add_subparsers(dest="x_command", required=True, metavar="ACTION")
    runners = {"search": run_search, "read": run_read, "replies": run_replies, "timeline": run_timeline}
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
    """Add the actions to a subparsers object; return {action name: its parser}."""
    parsers = {}
    parsers["search"] = p = commands.add_parser(
        "search", help="search X posts in RSRC_X_SEARCH order (default: configured cookie pair, else public search)")
    p.add_argument("query", help='X search query, e.g. \'"Letta Code" -filter:replies min_faves:5\'')
    p.add_argument("--since", type=iso_date, help="start date YYYY-MM-DD (inclusive); enables date windows")
    p.add_argument("--until", type=iso_date,
                   help="end date YYYY-MM-DD (exclusive); default: tomorrow UTC when --since is given")
    p.add_argument("--window-days", type=positive_int, default=7, help="days per window (default 7)")
    p.add_argument("--limit", type=positive_int, help="max unique posts in total, across all date windows")
    p.add_argument("--per-window", type=positive_int,
                   help="max unique posts per date window (defaults to --limit, otherwise 20)")
    p.add_argument("--backend", choices=BACKENDS, default=None,
                   help="use only this backend, ignoring RSRC_X_SEARCH. fxtwitter needs no login; "
                        "x-graphql uses your configured cookies; x-browser uses the X account signed in to Ego Lite")
    p.add_argument("--top", action="store_true", help="X's Top ranking instead of Latest")
    p.add_argument("--max-requests", type=positive_int, default=25,
                   help="hard cap on HTTP requests for this command (default 25)")

    parsers["read"] = p = commands.add_parser("read", help="read posts by URL or ID (no login)")
    p.add_argument("posts", nargs="+", metavar="URL_OR_ID", help="x.com/twitter.com status URL or numeric ID")

    parsers["replies"] = p = commands.add_parser("replies", help="replies to a post (no login)")
    p.add_argument("post", metavar="URL_OR_ID", help="x.com/twitter.com status URL or numeric ID")
    p.add_argument("--rank", choices=("likes", "recency"), default="likes", help="reply order (default likes)")
    p.add_argument("--limit", type=positive_int, default=100, help="max replies (default 100)")

    parsers["timeline"] = p = commands.add_parser(
        "timeline", help="one account: its profile, then its newest posts (no login)")
    p.add_argument("account", metavar="HANDLE_OR_URL", help="handle, @handle or x.com/twitter.com account URL")
    p.add_argument("--limit", type=positive_int, default=20,
                   help=f"max posts, 1-{fx.TIMELINE_MAX} (default 20); about 20 per request")
    return parsers


# --- runs ----------------------------------------------------------------------

def run_search(args, ctx) -> dict:
    input = {"query": args.query, "since": _iso(args.since), "until": _iso(args.until),
             "window_days": args.window_days, "per_window": args.per_window or args.limit or 20, "top": args.top,
             "backend": args.backend, "max_requests": args.max_requests}
    if args.limit is not None:
        input["limit"] = args.limit
    try:
        order = (args.backend,) if args.backend else load_search_order(ctx.environ)
    except CookieError as e:
        return env.envelope(CHANNEL, "search", input, None, [], meta={"attempts": []},
                            error=env.error(e.code, str(e)))
    input["backend"] = order[0]
    runs, remaining = [], args.max_requests
    for backend in order:
        runs.append(_search(args, backend, ctx, remaining))
        remaining -= sum(a["requests"] for a in runs[-1]["attempts"])
        if runs[-1]["outcome"] not in FALLBACK_ON or remaining <= 0:
            break
    # The result is the last run: the one that answered, or that stopped for a reason the user must
    # act on. When it too only failed to answer, the result is the first run's, whose backend the
    # user put first and whose partial items would otherwise be lost. Every other run goes in a note.
    result = runs[0] if runs[-1]["outcome"] in FALLBACK_ON else runs[-1]
    at = runs.index(result)
    notes = [f"{run['backend']} was tried {'first' if i < at else 'next'} and failed: {run['error']['message']}"
             for i, run in enumerate(runs) if i != at]
    meta = {**result["meta"], "attempts": [a for run in runs for a in run["attempts"]],
            "notes": notes + result["meta"].get("notes", [])}
    if (not args.backend and order == (FX_BACKEND,) and result["outcome"] in FALLBACK_ON):
        result["error"]["message"] += ". Public X search is unavailable. " + browser_remedy(args)
    return env.envelope(CHANNEL, "search", input, result["backend"], result["items"], meta=meta,
                        error=result["error"])


def browser_remedy(args):
    if not shutil.which("ego-browser"):
        return "ego-browser is unavailable. To install Ego Lite on macOS, run: open https://lite.ego.app"
    command = ["rsrc", "x", "search", "--backend", "x-browser"]
    for flag, default in (("since", None), ("until", None), ("window_days", 7),
                          ("per_window", None), ("limit", None), ("max_requests", 25)):
        value = getattr(args, flag)
        if flag == "max_requests":
            value = max(2, value)
        if value != default:
            command.extend(["--" + flag.replace("_", "-"), str(value)])
    if args.top:
        command.append("--top")
    if args.max_requests < 2:
        return "To use your signed-in Ego Lite account and allow two requests, run: " + shlex.join([*command, "--", args.query])
    return "To use the X account signed in to Ego Lite, run: " + shlex.join([*command, "--", args.query])


def _search(args, backend, ctx, max_requests) -> dict:
    """One backend's search: {"backend", "outcome", "items", "attempts", "meta", "error"}."""
    client = Client(transport=ctx.transport, max_requests=max_requests, sleep=ctx.sleep)
    items = []
    started = time.monotonic()
    run = {"backend": backend, "items": items, "meta": {}, "error": None}
    try:
        stats, capped = search_posts(args, backend, client, ctx.environ,
                                       lambda record: items.append(to_item(record)))
    except (UsageError, CookieError, HttpError) as e:
        code = error_code(e)
        # Usage and cookie errors happen before any request: no backend call to report.
        attempts = [] if code in ("usage", "config") else [_attempt(backend, code, client, started)]
        return {**run, "outcome": code, "attempts": attempts, "error": env.error(code, str(e))}
    outcome = "ok" if items else "no_results"
    if capped:
        run["meta"]["truncated"] = "request_cap"
    notes = []
    if dropped := stats.get("dropped"):
        notes.append(f"{dropped} post(s) outside their date window dropped")
    if args.limit and len(items) < args.limit:
        reasons = list(dict.fromkeys(stats.get("ends", [])))
        if capped:
            reasons.append("request cap reached")
        notes.append(f"Returned {len(items)} of {args.limit} requested posts: " + ", ".join(reasons))
    run["meta"]["notes"] = notes
    return {**run, "outcome": outcome, "attempts": [_attempt(backend, outcome, client, started)]}


def search_posts(args, backend, client, environ, sink) -> tuple[dict, bool]:
    """Run a search from parsed `search` args on `backend`, passing each record to `sink(record)`.

    Returns collection statistics and whether the request cap stopped the search.
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
    if backend == "x-graphql":
        cookies = load_cookies(environ)
        backend_session = nullcontext(search.Searcher(client, cookies, search.query_id_cache(environ)))
    elif backend == "x-browser":
        backend_session = browser.Searcher(client, environ)
    else:
        backend_session = nullcontext(fx.Searcher(client))
    stats, capped = {}, False
    try:
        with backend_session as searcher:
            per_window = args.per_window or args.limit or 20
            for record in search.collect(searcher, windowed, per_window, "Top" if args.top else "Latest",
                                         stats, limit=args.limit):
                sink(record)
    except CapReached:
        capped = True
    return stats, capped


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


def run_timeline(args, ctx) -> dict:
    input = {"account": args.account, "limit": args.limit}
    handle = fx.parse_handle(args.account)
    problem = None
    if fx.POST_URL_RE.search(args.account):
        problem = f"{args.account} is a post; read it with `rsrc x read`"
    elif not handle:
        problem = f"not a handle or account URL: {args.account}"
    elif args.limit > fx.TIMELINE_MAX:
        problem = f"--limit must be between 1 and {fx.TIMELINE_MAX}, got {args.limit}"
    if problem:
        return env.envelope(CHANNEL, "timeline", input, FX_BACKEND, error=env.error("usage", problem))
    client = Client(transport=ctx.transport, sleep=ctx.sleep)
    posts, account, error = [], None, None
    started = time.monotonic()
    try:
        for record, status in fx.timeline(client, handle, args.limit):
            posts.append(to_item(record))
            author = status.get("author")
            if not account and isinstance(author, dict) and str(author.get("screen_name")).lower() == handle.lower():
                account = account_item(author)
    except HttpError as e:
        error = env.error(error_code(e), str(e))
    outcome = error["code"] if error else "ok" if posts else "no_results"
    meta = {"attempts": [_attempt(FX_BACKEND, outcome, client, started)]}
    if posts and not account and not error:
        meta["notes"] = [f"none of the {len(posts)} posts is by @{handle} (reposts only), so there is no account item"]
    return env.envelope(CHANNEL, "timeline", input, FX_BACKEND, [account, *posts] if account else posts,
                        meta=meta, error=error)


# --- mapping -------------------------------------------------------------------

def to_item(record: dict) -> dict:
    counts = {k: record[k] for k in ENGAGEMENT if record.get(k) is not None}
    return env.item(id=record["id"], url=record["url"], title=None, author=record["author"],
                    published_at=record["created_at"], text=record["text"], source=record["source"],
                    engagement=counts or None, raw=record)


def account_item(author: dict) -> dict:
    """The account an `x timeline` was asked for, from FxTwitter's `author` object."""
    handle = author.get("screen_name")
    counts = {k: to_int(author.get(field))
              for k, field in (("followers", "followers"), ("following", "following"), ("posts", "statuses"))}
    counts = {k: v for k, v in counts.items() if v is not None}
    return env.item(id=author.get("id"), url=f"https://x.com/{handle}", title=author.get("name"), author=handle,
                    published_at=fx.joined_iso(author.get("joined")), text=author.get("description"),
                    source="fxtwitter", engagement=counts or None, raw=author)


def error_code(e: Exception) -> str:
    """Envelope error code the exception carries ("truncated" for the request cap, not an error)."""
    return "truncated" if isinstance(e, CapReached) else e.code


def _attempt(backend, outcome, client, started) -> dict:
    return env.attempt(backend, outcome, client.requests_made, env.elapsed_ms(started))


def _iso(day):
    return day.isoformat() if day else None
