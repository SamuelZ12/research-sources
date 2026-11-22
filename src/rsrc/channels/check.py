"""`rsrc check`: one cheap probe per channel (x, gh, reddit, hn, polymarket, youtube), one item
per channel.

x checks the search order (RSRC_X_SEARCH) and the session cookies locally. Missing or malformed
cookies fail x only when x-graphql comes first in the order; a cookie file readable by other users
always does. `--live-x` adds one `fx.read` of a fixed public post (one request, up to four when it
has to retry) and, when FxTwitter is in the order, one FxTwitter search request, both without
login. A failing FxTwitter search fails x unless x-graphql is in the order and its cookies load;
x-graphql is reported, never run, because it would search from the user's own X session. Cookie
values are never read into the output: only which keys are set and where. gh runs `gh auth status` and `gh api rate_limit`; its token is never read.
reddit runs a local ego-browser script (no Reddit request) unless `--live-reddit`, which also
loads one Reddit search page in Ego and reads one result from it. hn makes one keyless request for the top front-page story. polymarket makes one
keyless request for the first page of a fixed search. youtube looks for `yt-dlp` and `deno`, then
makes one yt-dlp metadata run for a fixed public video.
"""

import argparse
import json
import stat
import time
from types import SimpleNamespace

from rsrc import checks, proc
from rsrc import envelope as env
from rsrc.channels import gh as ghc
from rsrc.channels import bluesky
from rsrc.channels import hn as hnc
from rsrc.channels import polymarket as pmc
from rsrc.channels import producthunt
from rsrc.channels import reddit as rc
from rsrc.channels import x as xc
from rsrc.channels import youtube as ytc
from rsrc.channels.x import auth, fx, search
from rsrc.http import CapReached, Client, HttpError

CHANNEL = "check"
NAMES = ("x", "gh", "reddit", "hn", "polymarket", "youtube", "bluesky", "producthunt")
LIVE_X_POST = "20"  # jack's first post ("just setting up my twttr"), public since 2006
LIVE_X_QUERY = "hello"
LIVE_REDDIT_QUERY = "python"
HN_PROBE = "search?tags=front_page&hitsPerPage=1"
POLYMARKET_QUERY = "bitcoin"
YOUTUBE_VIDEO = "jNQXAC9IVRw"  # "Me at the zoo", the first video uploaded to YouTube: 19 s, public since 2005


def register(subparsers) -> None:
    parser = subparsers.add_parser("check", help=f"probe each channel's setup ({', '.join(NAMES)})",
                                   description="One cheap probe per channel. No FxTwitter request "
                                               "unless --live-x, no Reddit request unless --live-reddit.")
    parser.add_argument("channels", nargs="*", type=_channel_name, metavar="CHANNEL",
                        help=f"channels to check: {', '.join(NAMES)} (default: all)")
    parser.add_argument("--live-x", action="store_true",
                        help=f"also read one public post ({LIVE_X_POST}) and, when fxtwitter is in the search order, "
                             "run one FxTwitter search (no login; 1 request for the read, up to 4 when it "
                             "has to retry, and 1 for the search)")
    parser.add_argument("--live-reddit", action="store_true",
                        help="also load one Reddit search page in Ego (1 page load, in your browser)")
    parser.set_defaults(run=run, action="check")


def _channel_name(value: str) -> str:
    # `choices` with nargs="*" rejects the empty default on Python 3.11, so validate per word.
    if value not in NAMES:
        raise argparse.ArgumentTypeError(f"invalid channel {value!r} (choose from {', '.join(NAMES)})")
    return value


class _Failed(Exception):
    def __init__(self, code: str, message: str, raw: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.raw = raw


def run(args, ctx) -> dict:
    names = list(dict.fromkeys(args.channels)) or list(NAMES)
    input_ = {"channels": names, "live_x": args.live_x, "live_reddit": args.live_reddit}
    probes = {"bluesky": lambda attempts: bluesky.probe(ctx, attempts, _Failed),
              "x": lambda attempts: probe_x(ctx, args.live_x, attempts),
              "gh": lambda attempts: probe_gh(ctx, attempts),
              "reddit": lambda attempts: probe_reddit(ctx, args.live_reddit, attempts),
              "hn": lambda attempts: probe_hn(ctx, attempts),
              "polymarket": lambda attempts: probe_polymarket(ctx, attempts),
              "youtube": lambda attempts: probe_youtube(ctx, attempts),
              "producthunt": lambda attempts: probe_producthunt(ctx, attempts)}
    items, sources, attempts, failures = [], {}, [], []
    for name in names:
        try:
            text, raw = probes[name](attempts)
        except _Failed as failure:
            sources[name] = failure.code
            failures.append(env.error(failure.code, f"{name}: {failure.message}"))
            text, raw = failure.message, failure.raw
        else:
            sources[name] = "ok"
        items.append(env.item(id=name, source=name, text=text, raw=raw))
    error = failures[0] if len(failures) == len(names) else None
    return env.envelope(CHANNEL, "check", input_, None, items,
                        meta={"attempts": attempts, "sources": sources}, error=error)


# --- x ---------------------------------------------------------------------------

def probe_x(ctx, live: bool, attempts: list) -> tuple[str, dict]:
    environ = ctx.environ
    path = auth.cookie_file(environ)
    raw = {"cookie_file": str(path), "file_exists": path.exists(), "file_mode": None,
           "env_keys": [k for k in auth.KEYS if environ.get(k, "").strip()],
           "x_graphql": "ready", "query_id_cache": _query_id_cache(environ), "search_order": [], "live": None}
    if raw["file_exists"]:
        mode = stat.S_IMODE(path.stat().st_mode)
        raw["file_mode"] = f"{mode:o}"
        if mode & 0o077:
            raise _Failed("config", f"{path} is accessible by other users; run: chmod 600 {path}", raw)
    try:
        order = auth.load_search_order(environ)
        raw["search_order"] = list(order)
    except auth.CookieError as e:
        raise _Failed(e.code, str(e), raw) from None
    labels = {"fxtwitter": "fxtwitter (no login)", "x-graphql": "x-graphql with your X session",
              "x-browser": "x-browser with the X account signed in to Ego Lite"}
    searching = f"search via {labels[order[0]]}"
    if len(order) > 1:
        searching += f", then {labels[order[1]]} on upstream_error or network_error"
    try:
        auth.load_cookies(environ)  # the values are discarded; only success matters
    except auth.CookieError as e:
        raw["x_graphql"] = "unavailable"
        x_graphql = f"x-graphql unavailable: {e}"
        if order[0] == "x-graphql":
            raise _Failed(e.code, f"{searching}; {x_graphql}", raw) from None
    else:
        where = "environment" if len(raw["env_keys"]) == len(auth.KEYS) else str(path)
        x_graphql = (f"x-graphql: cookies {', '.join(auth.KEYS)} present ({where}); "
                     f"query-ID cache {raw['query_id_cache']['state']}")
    text = f"{searching}; {x_graphql}"
    if live:
        raw["live"] = _live_read(ctx, attempts, raw)
        text += f"; fxtwitter read of post {LIVE_X_POST} ok"
        if xc.FX_BACKEND in order:
            text += "; " + _live_search(ctx, attempts, raw, x_graphql)
        else:
            text += f"; searches run on {labels[order[0]]}, which this check did not try"
    return text, raw


def _query_id_cache(environ) -> dict:
    """Missing is fine: x-graphql search falls back to its baked query IDs."""
    path = search.query_id_cache(environ)
    try:
        query_id = json.loads(path.read_text()).get("SearchTimeline")
    except FileNotFoundError:
        return {"path": str(path), "state": "absent (baked query IDs used)"}
    except (OSError, ValueError, AttributeError):
        return {"path": str(path), "state": "unreadable (baked query IDs used)"}
    state = "readable" if isinstance(query_id, str) and query_id else "has no SearchTimeline ID"
    return {"path": str(path), "state": state}


def _live_read(ctx, attempts: list, raw: dict) -> dict:
    client = Client(transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    try:
        record = fx.read(client, LIVE_X_POST)
    except (HttpError, CapReached) as e:  # RateLimited (429) stops the probe
        code = xc.error_code(e)
        attempts.append(env.attempt(xc.FX_BACKEND, code, client.requests_made, env.elapsed_ms(started)))
        raise _Failed(code, str(e), raw) from None
    if "error" in record:
        code = record["code"]
        attempts.append(env.attempt(xc.FX_BACKEND, code, client.requests_made, env.elapsed_ms(started)))
        raise _Failed(code, f"post {LIVE_X_POST}: {record['error']}", raw)
    attempts.append(env.attempt(record["source"], "ok", client.requests_made, env.elapsed_ms(started)))
    _, rejection = checks.screen_read(xc.to_item(record), LIVE_X_POST)
    if rejection:
        code = "blocked" if rejection["reason"] == "blocked_page" else "url_mismatch"
        raise _Failed(code, f"post {LIVE_X_POST}: {rejection['reason']}", raw)
    return {"post": LIVE_X_POST, "source": record["source"], "requests": client.requests_made}


def _live_search(ctx, attempts: list, raw: dict, x_graphql: str) -> str:
    """One FxTwitter search request; returns the sentence for the item text. `x_graphql` is the
    sentence on the session cookies, repeated in the failure when x-graphql is in the order.

    The request cap of 1 turns the retries a real search makes into the failure they would retry.
    A failing search fails x, except when searches have somewhere else to go: x-graphql in the
    order with loadable cookies. A rate limit always fails x.
    """
    client = Client(transport=ctx.transport, sleep=ctx.sleep, max_requests=1)
    started = time.monotonic()
    code, message = "ok", None
    try:
        fx.Searcher(client).page(LIVE_X_QUERY, count=1)
    except HttpError as e:
        code, message = e.code, str(e)
    attempts.append(env.attempt(xc.FX_BACKEND, code, client.requests_made, env.elapsed_ms(started)))
    raw["live"] |= {"search": code, "search_error": message, "requests": raw["live"]["requests"] + client.requests_made}
    if code == "ok":
        return "fxtwitter search ok"
    order = raw["search_order"]
    if code in xc.FALLBACK_ON and "x-graphql" in order and raw["x_graphql"] == "ready":
        if order[0] == xc.FX_BACKEND:
            return (f"fxtwitter search is failing ({message}), so searches fall back to x-graphql with your "
                    "X session, which this check did not try")
        return (f"fxtwitter search, the fallback here, is failing ({message}); searches run on x-graphql "
                "with your X session, which this check did not try")
    fix = ""
    if code in xc.FALLBACK_ON and "x-graphql" in order:
        fix = f"; x-graphql is in RSRC_X_SEARCH but cannot take over ({x_graphql})"
    elif code in xc.FALLBACK_ON:
        fix = ("; `rsrc x search` fails until FxTwitter recovers, or put x-graphql in RSRC_X_SEARCH with your "
               "X session cookies (README)")
    raise _Failed(code, f"{message}{fix}", raw)


# --- gh ----------------------------------------------------------------------------

def probe_gh(ctx, attempts: list) -> tuple[str, dict]:
    runner = ctx.runner or proc.run
    _gh(runner, ["gh", "auth", "status"], attempts, auth_status=True)
    stdout = _gh(runner, ["gh", "api", "rate_limit"], attempts)
    try:
        resources = json.loads(stdout)["resources"]
        core, search_limit = resources["core"], resources["search"]
        counts = (core["remaining"], core["limit"], search_limit["remaining"])
    except (ValueError, KeyError, TypeError):
        attempts[-1]["outcome"] = "upstream_error"
        raise _Failed("upstream_error", "gh api rate_limit returned unexpected JSON") from None
    text = f"logged in; core {counts[0]}/{counts[1]} remaining, search {counts[2]} remaining"
    return text, {"auth": "logged in", "rate_limit": {"core": core, "search": search_limit}}


def _gh(runner, argv: list[str], attempts: list, auth_status: bool = False) -> str:
    started = time.monotonic()
    try:
        result = runner(argv, ghc.TIMEOUT)
    except proc.Missing:
        attempts.append(env.attempt(ghc.BACKEND, "config"))
        raise _Failed("config", "gh not installed (brew install gh)") from None
    except proc.Timeout:
        attempts.append(env.attempt(ghc.BACKEND, "timeout", ms=env.elapsed_ms(started)))
        raise _Failed("timeout", f"{' '.join(argv)} did not answer within {ghc.TIMEOUT} s") from None
    if result.code != 0:
        code, message = ghc.classify(result)
        if auth_status and code == "upstream_error":
            # `gh auth status` fails only when no host has a working login; its output names
            # accounts and token scopes, so it is not echoed.
            code, message = "config", f"gh auth status exited with code {result.code}; run `gh auth login`"
        attempts.append(env.attempt(ghc.BACKEND, code, ms=env.elapsed_ms(started)))
        raise _Failed(code, message)
    attempts.append(env.attempt(ghc.BACKEND, "ok", ms=env.elapsed_ms(started)))
    return result.stdout


# --- reddit ------------------------------------------------------------------------

REDDIT_PROBE = 'const s = await listTaskSpaces(); console.log("RSRC_REDDIT_OK " + s.length);'
REDDIT_PROBE_TIMEOUT = 20  # listing task spaces is local; a hung Ego should not hold check for 2 min


def probe_reddit(ctx, live: bool, attempts: list) -> tuple[str, dict]:
    """ego-browser answers and reaches the running app. Unless `live`, no page is opened and
    nothing hits Reddit; with it, one search page is loaded and one result read from it."""
    started = time.monotonic()
    try:
        result = (ctx.runner or proc.run)(["ego-browser", "nodejs", "-e", REDDIT_PROBE], REDDIT_PROBE_TIMEOUT)
    except proc.Missing:
        attempts.append(env.attempt(rc.BACKEND, "config"))
        raise _Failed("config", "ego-browser not found; install Ego Lite and keep it running") from None
    except proc.Timeout:
        attempts.append(env.attempt(rc.BACKEND, "timeout", ms=env.elapsed_ms(started)))
        raise _Failed("timeout", f"ego-browser did not answer within {REDDIT_PROBE_TIMEOUT} s; "
                                 "is Ego Lite running?") from None
    if result.code != 0 or rc.result_line(result, "RSRC_REDDIT_OK") is None:
        attempts.append(env.attempt(rc.BACKEND, "upstream_error", ms=env.elapsed_ms(started)))
        tail = rc.output_tail(result, "RSRC_REDDIT_OK")
        what = f"ego-browser exited with code {result.code}" if result.code else "ego-browser printed no answer"
        raise _Failed("upstream_error", what + (f": {tail}" if tail else "") + "; is Ego Lite running?")
    attempts.append(env.attempt(rc.BACKEND, "ok", ms=env.elapsed_ms(started)))
    if not live:
        return "ego-browser answered (no Reddit request made)", {"ego_browser": "ok"}
    search = rc.run_search(SimpleNamespace(query=LIVE_REDDIT_QUERY, subreddit=None, time="month",
                                           sort="relevance", limit=1), ctx)
    attempts.extend(search["meta"]["attempts"])
    raw = {"ego_browser": "ok", "live": {"query": LIVE_REDDIT_QUERY, "posts": len(search["items"])}}
    if search["error"]:
        raise _Failed(search["error"]["code"], search["error"]["message"], raw)
    if not search["items"]:
        # A common word with no results in the past month is a page rsrc could not read.
        attempts[-1]["outcome"] = "upstream_error"
        raise _Failed("upstream_error", f"Reddit search for {LIVE_REDDIT_QUERY!r} returned no results", raw)
    return "ego-browser answered; one Reddit search page read (1 page load in Ego)", raw


# --- hn ----------------------------------------------------------------------------

def probe_hn(ctx, attempts: list) -> tuple[str, dict]:
    """One request to the Algolia HN API: the top front-page story."""
    try:
        hits = hnc.fetch(ctx, HN_PROBE, hnc.parse_hits, attempts)
    except HttpError as e:
        raise _Failed(e.code, str(e)) from None
    if not hits:
        attempts[-1]["outcome"] = "upstream_error"
        raise _Failed("upstream_error", "Hacker News API answered with an empty front page")
    return "Algolia HN API answered (1 request, no key)", {"front_page_story": hits[0]["objectID"]}


# --- polymarket --------------------------------------------------------------------

def probe_polymarket(ctx, attempts: list) -> tuple[str, dict]:
    """One request to the gamma API: the first page of a fixed search. Any list of events will do."""
    client = Client(transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    try:
        events, _ = pmc.fetch_page(client, POLYMARKET_QUERY, 1)
    except HttpError as e:
        attempts.append(env.attempt(pmc.BACKEND, e.code, client.requests_made, env.elapsed_ms(started)))
        raise _Failed(e.code, str(e)) from None
    attempts.append(env.attempt(pmc.BACKEND, "ok", client.requests_made, env.elapsed_ms(started)))
    return "Polymarket gamma API answered (1 request, no key)", {"events": len(events)}


# --- youtube -----------------------------------------------------------------------

def probe_youtube(ctx, attempts: list) -> tuple[str, dict]:
    """`yt-dlp` and `deno` on PATH, then one yt-dlp run: the metadata of a fixed public video."""
    found, attempt, failure = ytc.probe(ctx, YOUTUBE_VIDEO)
    attempts.append(attempt)
    if failure:
        raise _Failed(*failure, found)
    vidscribe = "found" if found["vidscribe"] else "not found (no speech recognition for videos without captions)"
    return (f"yt-dlp {found['yt_dlp_version'] or 'of unknown version'} read video {YOUTUBE_VIDEO} without a warning "
            f"(1 request, no key); deno found; vidscribe {vidscribe}"), found

def probe_producthunt(ctx, attempts):
    result = producthunt.run(SimpleNamespace(action="search", query="linear", limit=1, max_requests=1), ctx)
    attempts.extend(result["meta"]["attempts"])
    if result["error"]:
        raise _Failed(result["error"]["code"], result["error"]["message"])
    if not result["items"]:
        attempts[-1]["outcome"] = "upstream_error"
        raise _Failed("upstream_error", "Product Hunt returned no products for its probe")
    return "Product Hunt MCP answered an anonymous product search", {"product": result["items"][0]["id"]}
