import argparse
import json
import stat
import time
from types import SimpleNamespace

from rsrc import checks, chrome, proc
from rsrc import envelope as env
from rsrc.channels import appstore, ashby, linkedin
from rsrc.channels import hn as hnc
from rsrc.channels import reddit as rc
from rsrc.channels import reddit_archive
from rsrc.channels import x as xc
from rsrc.channels import youtube as ytc
from rsrc.channels.x import auth, connection, fx, search
from rsrc.http import CapReached, Client, HttpError

CHANNEL = "check"
NAMES = ("x", "reddit", "hn", "youtube", "appstore", "ashby", "linkedin")
LIVE_X_POST = "20"  # jack's first post ("just setting up my twttr"), public since 2006
LIVE_X_QUERY = "hello"
LIVE_REDDIT_QUERY = "python"
ARCHIVE_REDDIT_POST = "1641krf"
HN_PROBE = "search?tags=front_page&hitsPerPage=1"
YOUTUBE_VIDEO = "jNQXAC9IVRw"  # "Me at the zoo", the first video uploaded to YouTube: 19 s, public since 2005


def register(subparsers) -> None:
    parser = subparsers.add_parser("check", help=f"probe each channel's setup ({', '.join(NAMES)})",
                                   description="By default check all seven channels. Probe HN, "
                                               "the App Store catalog and an Ashby board, and read YouTube "
                                               "metadata with yt-dlp. X setup and Ego Lite "
                                               "are checked locally unless their live flag is set. Without Ego Lite, "
                                               "Reddit check verifies one archived thread.")
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
    probes = {"linkedin": lambda attempts: probe_linkedin(ctx, attempts),
              "x": lambda attempts: probe_x(ctx, args.live_x, attempts),
              "ashby": lambda attempts: probe_ashby(ctx, attempts),
              "appstore": lambda attempts: probe_appstore(ctx, attempts),
              "reddit": lambda attempts: probe_reddit(ctx, args.live_reddit, attempts),
              "hn": lambda attempts: probe_hn(ctx, attempts),
              "youtube": lambda attempts: probe_youtube(ctx, attempts)}
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
    try:
        order = auth.load_search_order(environ)
        raw["search_order"] = list(order)
    except auth.CookieError as e:
        raise _Failed(e.code, str(e), raw) from None
    raw["x_chrome"] = {"configured": auth.chrome_settings_path(environ).exists(), "reachable": None}
    try:
        config = auth.chrome_settings(environ)
        if config:
            endpoint, managed = config
            raw["x_chrome"]["reachable"] = False
            if managed:
                if not connection.active(connection.profile_path(environ)):
                    raise chrome.ChromeError(chrome.UNREACHABLE)
            else:
                chrome.discover(endpoint)
            raw["x_chrome"]["reachable"] = True
    except (chrome.ChromeError, auth.CookieError) as e:
        if order[0] == "x-chrome":
            raise _Failed("config", str(e), raw) from None
    if order[0] == "x-chrome" and not raw["x_chrome"]["configured"]:
        raise _Failed("config", "Chrome is not configured. Run rsrc x connect.", raw)
    labels = {"x-chrome": "x-chrome with Google Chrome", "fxtwitter": "fxtwitter (no login)", "x-graphql": "x-graphql with your X session",
              "x-browser": "x-browser with the X account signed in to Ego Lite"}
    searching = f"search via {labels[order[0]]}"
    if len(order) > 1:
        searching += f", then {labels[order[1]]} on upstream_error or network_error"
    if order == ("x-chrome",):
        raw["x_graphql"] = "unchecked"
        text = searching + "; Chrome endpoint reachable"
        if live:
            raw["live"] = _live_read(ctx, attempts, raw)
            text += "; public post read succeeded; signed-in search unchecked"
        return text, raw
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
    if not raw["x_chrome"]["configured"]:
        text += "; Google Chrome is not configured. Run rsrc x connect."
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
    client = Client(min_interval=1, transport=ctx.transport, sleep=ctx.sleep)
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
    client = Client(min_interval=1, transport=ctx.transport, sleep=ctx.sleep, max_requests=1)
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


# --- reddit ------------------------------------------------------------------------

REDDIT_PROBE = 'const s = await listTaskSpaces(); console.log("RSRC_REDDIT_OK " + s.length);'
REDDIT_PROBE_TIMEOUT = 20  # listing task spaces is local; a hung Ego should not hold check for 2 min


def probe_reddit(ctx, live: bool, attempts: list) -> tuple[str, dict]:
    """Check local Ego Lite, or verify an archived thread when Ego Lite is missing.
    An explicit live check requires Ego Lite and reads one search result."""
    started = time.monotonic()
    try:
        result = (ctx.runner or proc.run)(["ego-browser", "nodejs", "-e", REDDIT_PROBE], REDDIT_PROBE_TIMEOUT)
    except proc.Missing:
        attempts.append(env.attempt(rc.BACKEND, "config"))
        if live:
            raise _Failed("config", "Reddit search requires Ego Lite; ego-browser was not found") from None
        read = reddit_archive.read(ctx, {"post": ARCHIVE_REDDIT_POST, "sort": "top", "comments": 1},
                                   ARCHIVE_REDDIT_POST)
        attempts.extend(read["meta"]["attempts"])
        raw = {"ego_browser": "missing", "thread_read": read["backend"] if read["ok"] else None}
        search_requirement = "Reddit search requires Ego Lite"
        if read["error"]:
            raise _Failed(read["error"]["code"], f"{read['error']['message']}. {search_requirement}. ", raw)
        return f"Thread reads work through the Arctic Shift archive. {search_requirement}.", raw
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

def probe_appstore(ctx, attempts):
    args = SimpleNamespace(action="search", query="Wikipedia", country="us", limit=1)
    answer = appstore.run(args, ctx)
    attempts.extend(answer["meta"]["attempts"])
    if answer["error"]:
        raise _Failed(answer["error"]["code"], answer["error"]["message"])
    if not answer["items"]:
        attempts[-1]["outcome"] = "upstream_error"
        raise _Failed("upstream_error", "Apple returned no apps for Wikipedia")
    return "App Store catalog answered (1 request, no key)", {"app_id": answer["items"][0]["id"]}


def probe_ashby(ctx, attempts):
    args = SimpleNamespace(action="search", board="linear", title=None, team=None,
                           location=None, since=None, limit=1)
    answer = ashby.run(args, ctx)
    attempts.extend(answer["meta"]["attempts"])
    if answer["error"]:
        raise _Failed(answer["error"]["code"], answer["error"]["message"])
    return "Linear's public Ashby board answered", {"board": "linear"}


LINKEDIN_POST = "https://www.linkedin.com/feed/update/urn:li:activity:7204256477832753153/"


def probe_linkedin(ctx, attempts):
    answer = linkedin.run(SimpleNamespace(url=LINKEDIN_POST), ctx)
    attempts.extend(answer["meta"]["attempts"])
    if answer["error"]:
        raise _Failed(answer["error"]["code"], answer["error"]["message"])
    return "LinkedIn returned a public post", {"post_id": answer["items"][0]["id"]}
