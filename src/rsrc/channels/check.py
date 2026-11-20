"""`rsrc check`: one cheap probe per channel (x, gh, reddit, hn, polymarket, youtube), one item
per channel.

x is checked locally unless `--live-x`, which makes one `fx.read` of a fixed public post. Search
defaults to FxTwitter (no login), so missing or malformed cookies only make `--backend x-graphql`
unavailable; a cookie file readable by other users still fails x. Cookie values are never read
into the output: only which keys are set and where. gh runs `gh auth status` and
`gh api rate_limit`; its token is never read. reddit runs a local ego-browser script (no
Reddit request). hn makes one keyless request for the top front-page story. polymarket makes one
keyless request for the first page of a fixed search. youtube looks for `yt-dlp` and `deno`, then
makes one yt-dlp metadata run for a fixed public video.
"""

import argparse
import json
import stat
import time

from rsrc import checks, proc
from rsrc import envelope as env
from rsrc.channels import gh as ghc
from rsrc.channels import hn as hnc
from rsrc.channels import polymarket as pmc
from rsrc.channels import reddit as rc
from rsrc.channels import x as xc
from rsrc.channels import youtube as ytc
from rsrc.channels.x import auth, fx, search
from rsrc.http import CapReached, Client, HttpError

CHANNEL = "check"
NAMES = ("x", "gh", "reddit", "hn", "polymarket", "youtube")
LIVE_X_POST = "20"  # jack's first post ("just setting up my twttr"), public since 2006
HN_PROBE = "search?tags=front_page&hitsPerPage=1"
POLYMARKET_QUERY = "bitcoin"
YOUTUBE_VIDEO = "jNQXAC9IVRw"  # "Me at the zoo", the first video uploaded to YouTube: 19 s, public since 2005


def register(subparsers) -> None:
    parser = subparsers.add_parser("check", help=f"probe each channel's setup ({', '.join(NAMES)})",
                                   description="One cheap probe per channel. No x.com request "
                                               "unless --live-x.")
    parser.add_argument("channels", nargs="*", type=_channel_name, metavar="CHANNEL",
                        help=f"channels to check: {', '.join(NAMES)} (default: all)")
    parser.add_argument("--live-x", action="store_true",
                        help=f"also read one public post ({LIVE_X_POST}) through FxTwitter")
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
    input_ = {"channels": names, "live_x": args.live_x}
    probes = {"x": lambda attempts: probe_x(ctx, args.live_x, attempts),
              "gh": lambda attempts: probe_gh(ctx, attempts),
              "reddit": lambda attempts: probe_reddit(ctx, attempts),
              "hn": lambda attempts: probe_hn(ctx, attempts),
              "polymarket": lambda attempts: probe_polymarket(ctx, attempts),
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
           "x_graphql": "ready", "query_id_cache": _query_id_cache(environ), "fallback": None, "live": None}
    if raw["file_exists"]:
        mode = stat.S_IMODE(path.stat().st_mode)
        raw["file_mode"] = f"{mode:o}"
        if mode & 0o077:
            raise _Failed("config", f"{path} is accessible by other users; run: chmod 600 {path}", raw)
    try:
        raw["fallback"] = auth.load_fallback(environ)
    except auth.CookieError as e:
        raise _Failed(e.code, str(e), raw) from None
    try:
        auth.load_cookies(environ)  # the values are discarded; only success matters
    except auth.CookieError as e:
        raw["x_graphql"] = "unavailable"
        x_graphql = f"x-graphql unavailable: {e}"
    else:
        where = "environment" if len(raw["env_keys"]) == len(auth.KEYS) else str(path)
        x_graphql = (f"x-graphql: cookies {', '.join(auth.KEYS)} present ({where}); "
                     f"query-ID cache {raw['query_id_cache']['state']}")
    searching = "search via fxtwitter (no login)"
    if raw["fallback"]:
        searching += f", falls back to {raw['fallback']} on upstream_error or network_error"
    text = f"{searching}; {x_graphql}"
    if live:
        raw["live"] = _live_read(ctx, attempts, raw)
        text += f"; fxtwitter read of post {LIVE_X_POST} ok"
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


def probe_reddit(ctx, attempts: list) -> tuple[str, dict]:
    """ego-browser answers and reaches the running app; no page is opened, nothing hits Reddit."""
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
    return "ego-browser answered (no Reddit request made)", {"ego_browser": "ok"}


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
