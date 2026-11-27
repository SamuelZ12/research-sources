"""Anonymous Bluesky AppView reads. Search has a separate host and failure domain."""

import argparse
import json
import re
import time
from datetime import date, datetime, timezone
from urllib.parse import urlencode, urlsplit

from rsrc import envelope as env
from rsrc.http import CapReached, Client, HttpError, raise_refusal
from rsrc.html_markdown import label as escape_label, link

PUBLIC = "public.api.bsky.app"
SEARCH = "api.bsky.app"
COLLECTION = "app.bsky.feed.post"
THREAD = "app.bsky.feed.defs#"
ACTOR = r"(?:did:[a-z]+:[A-Za-z0-9.%:_-]+|(?:[a-zA-Z0-9-]+\.)+[a-zA-Z][a-zA-Z0-9-]*)"
KEY = r"[A-Za-z0-9.:_~-]{1,512}"
COUNTS = {"likes": "likeCount", "reposts": "repostCount", "replies": "replyCount", "quotes": "quoteCount"}


class Failure(HttpError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def post_ref(value):
    web = rf"https://bsky\.app/profile/({ACTOR})/post/({KEY})/?(?:[?#].*)?"
    at = rf"at://({ACTOR})/app\.bsky\.feed\.post/({KEY})"
    match = re.fullmatch(web, value) or re.fullmatch(at, value)
    if not match:
        raise argparse.ArgumentTypeError("expected a bsky.app post URL or at:// post URI")
    return f"at://{match[1]}/{COLLECTION}/{match[2]}"


def actor_ref(value):
    for pattern in (rf"@?({ACTOR})", rf"at://({ACTOR})/?", rf"https://bsky\.app/profile/({ACTOR})/?(?:[?#].*)?"):
        if match := re.fullmatch(pattern, value):
            return match[1]
    raise argparse.ArgumentTypeError("expected a handle, DID or profile URL")


def register(subparsers):
    channel = subparsers.add_parser("bluesky", help="Bluesky posts and conversations (no login)")
    commands = channel.add_subparsers(dest="action", required=True)
    search = commands.add_parser("search", help="search one page of posts, newest first")
    search.add_argument("query", nargs="?", help="keywords (optional with --url)")
    search.add_argument("--url", help="search posts linking to this exact URL")
    search.add_argument("--top", action="store_true", help="use Bluesky's Top ranking")
    search.add_argument("--since", type=date.fromisoformat, help="inclusive UTC date")
    search.add_argument("--until", type=date.fromisoformat, help="exclusive UTC date")
    search.add_argument("--author", type=actor_ref, help="only posts by this account")
    search.add_argument("--domain", help="only posts linking to this domain")
    read = commands.add_parser("read", help="read a post by URL or AT URI")
    replies = commands.add_parser("replies", help="read visible descendants, up to three levels deep")
    for parser in (read, replies):
        parser.add_argument("post", type=post_ref, metavar="URL_OR_AT_URI")
    timeline = commands.add_parser("timeline", help="profile followed by the account's recent feed")
    timeline.add_argument("account", type=actor_ref, metavar="HANDLE_OR_URL")
    for parser in (search, replies, timeline):
        parser.add_argument("--limit", type=int, default=20, help="maximum posts, 1-100 (default 20)")
    for parser in (read, search, replies, timeline):
        parser.add_argument("--max-requests", type=int, default=2 if parser is timeline else 1,
                            help="total requests (default %(default)s)")
        parser.set_defaults(run=run)


def fetch(client, host, method, **params):
    query = urlencode({k: v for k, v in params.items() if v is not None})
    status, body = client.request("GET", f"https://{host}/xrpc/app.bsky.{method}?{query}")
    label = f"{host} {method}"
    raise_refusal(status, body, label)
    try:
        data = json.loads(body)
    except (UnicodeError, ValueError):
        raise HttpError(f"{label} returned invalid JSON (HTTP {status})") from None
    if not isinstance(data, dict):
        raise HttpError(f"{label} returned an invalid response")
    if status != 200 or "error" in data:
        missing = data.get("error") in ("NotFound", "ProfileNotFound") or data.get("message") == "Profile not found"
        raise Failure("not_found" if missing else "upstream_error", f"{label}: HTTP {status} {data.get('error', '')}")
    return data


def linked_text(label, uri, *, after_bang=False):
    if not isinstance(uri, str):
        raise HttpError("invalid Bluesky link URI")
    if re.search(r"[\s\x00-\x1f\x7f<>\\]", uri):
        return label
    try:
        parsed = urlsplit(uri)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return label
    except ValueError:
        return label
    text = label.strip()
    if not text:
        return f"{label}<{uri}>"
    bare = uri.split("://", 1)[1]
    prefix = text.removesuffix("…").removesuffix("...")
    scheme, separator, rest = prefix.partition("://")
    shortened = prefix != text and (bare.startswith(prefix) and "." in prefix or
                                    separator and scheme.lower() == parsed.scheme and bare.startswith(rest))
    if shortened or text in (uri, bare):
        shown = uri
    elif after_bang:
        shown = escape_label(text.replace("\\", "\\\\")) + f" (<{uri}>)"
    else:
        shown = link(text.replace("\\", "\\\\"), uri)
    return label[:len(label) - len(label.lstrip())] + shown + label[len(label.rstrip()):]


def expanded_text(record):
    data = record["text"].encode()
    pieces, end = [], 0
    for facet in sorted(record.get("facets", []), key=lambda f: f["index"]["byteStart"]):
        for feature in facet["features"]:
            if feature["$type"] == "app.bsky.richtext.facet#link":
                start, stop = facet["index"]["byteStart"], facet["index"]["byteEnd"]
                if not end <= start < stop <= len(data):
                    raise HttpError("invalid Bluesky link facet")
                if start > end:
                    pieces.append(data[end:start].decode())
                after_bang = bool(pieces and pieces[-1].endswith("!"))
                pieces.append(linked_text(data[start:stop].decode(), feature["uri"], after_bang=after_bang))
                end = stop
                break
    return "".join(pieces) + data[end:].decode()


def post_item(post):
    actor, record, uri = post["author"], post["record"], post["uri"]
    key = uri.rsplit("/", 1)[-1]
    if (not re.fullmatch(KEY, key) or not re.fullmatch(ACTOR, actor["did"]) or
            not actor["did"].startswith("did:") or uri != f"at://{actor['did']}/{COLLECTION}/{key}"
            or not re.fullmatch(ACTOR, actor["handle"]) or record["$type"] != COLLECTION):
        raise HttpError("invalid Bluesky post identity")
    when = datetime.fromisoformat(record["createdAt"])
    if when.tzinfo is None:
        raise HttpError("Bluesky post timestamp has no timezone")
    embed = post.get("embed") or record.get("embed")
    raw = {"text": record["text"], "display_name": actor.get("displayName"), "embed": embed,
           "parent_uri": record.get("reply", {}).get("parent", {}).get("uri")}
    if embed:
        quote = embed.get("record", {})
        if embed.get("$type") == "app.bsky.embed.recordWithMedia#view":
            quote = quote.get("record", {})
        if quote.get("$type") == "app.bsky.embed.record#viewRecord" and quote["value"].get("$type") == COLLECTION:
            handle = quote["author"]["handle"]
            raw["quote"] = {"url": f"https://bsky.app/profile/{handle}/post/{quote['uri'].rsplit('/', 1)[-1]}",
                            "author": handle, "text": expanded_text(quote["value"])}
    return env.item(id=uri, url=f"https://bsky.app/profile/{actor['handle']}/post/{key}",
                    author=actor["handle"], published_at=when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    text=expanded_text(record), source="bluesky",
                    engagement={key: post[field] for key, field in COUNTS.items() if field in post},
                    raw=raw)


def entries(mapping, key):
    values = mapping[key]
    if not isinstance(values, list):
        raise HttpError(f"expected an array in Bluesky {key}")
    return values


def matches(actor, expected):
    return expected == actor["did"] or expected.lower() == actor["handle"].lower()


def conversation(client, args, meta):
    depth = 3 if args.action == "replies" else 0
    root = fetch(client, PUBLIC, "feed.getPostThread", uri=args.post, depth=depth, parentHeight=0)["thread"]
    kind = root["$type"]
    if kind in (THREAD + "notFoundPost", THREAD + "blockedPost"):
        raise Failure("not_found" if kind.endswith("notFoundPost") else "blocked", "post unavailable")
    if kind != THREAD + "threadViewPost":
        raise HttpError("unexpected Bluesky thread type")
    post = root["post"]
    item = post_item(post)
    _, _, actor, _, key = args.post.split("/")
    if not matches(post["author"], actor) or item["id"].rsplit("/", 1)[1] != key:
        raise Failure("url_mismatch", "Bluesky returned a different post")
    if not depth:
        return [item]
    items, unavailable = [], []

    def visit(node, parent, level):
        if "replies" not in node:
            return
        for child in entries(node, "replies"):
            if child["$type"] in (THREAD + "blockedPost", THREAD + "notFoundPost"):
                unavailable.append(child["$type"])
                continue
            if child["$type"] != THREAD + "threadViewPost":
                raise HttpError("unexpected Bluesky reply type")
            reply = post_item(child["post"])
            if reply["raw"]["parent_uri"] != parent:
                raise Failure("url_mismatch", "Bluesky returned a reply to a different post")
            reply["raw"]["depth"] = level
            items.append(reply)
            if level < depth and len(items) < args.limit:
                visit(child, reply["id"], level + 1)
            if len(items) >= args.limit:
                break

    visit(root, item["id"], 1)
    if unavailable:
        if not items:
            code = "blocked" if THREAD + "blockedPost" in unavailable else "not_found"
            raise Failure(code, "all returned replies are unavailable")
        meta["notes"] = [f"{len(unavailable)} unavailable replies omitted"]
    return items


def timeline(client, args, items):
    profile = fetch(client, PUBLIC, "actor.getProfile", actor=args.account)
    if not profile["did"].startswith("did:") or not re.fullmatch(ACTOR, profile["handle"]):
        raise HttpError("invalid Bluesky profile identity")
    if not matches(profile, args.account):
        raise Failure("url_mismatch", "Bluesky returned a different profile")
    handle = profile["handle"]
    items.append(env.item(id=profile["did"], url=f"https://bsky.app/profile/{handle}", author=handle,
                          title=profile.get("displayName"), text=profile.get("description"), source="bluesky",
                          engagement={"followers": profile.get("followersCount"),
                               "following": profile.get("followsCount"), "posts": profile.get("postsCount")},
                          raw={"kind": "profile"}))
    feed = fetch(client, PUBLIC, "feed.getAuthorFeed", actor=profile["did"], limit=args.limit, filter="posts_no_replies")
    for entry in entries(feed, "feed")[:args.limit]:
        post = entry["post"]
        reason = entry.get("reason", {})
        repost = reason.get("$type") == THREAD + "reasonRepost"
        if not matches(reason["by"] if repost else post["author"], profile["did"]):
            raise Failure("url_mismatch", "Bluesky returned another account's feed entry")
        item = post_item(post)
        item["raw"]["reposted_by"] = handle if repost else None
        items.append(item)


def run(args, ctx):
    action = args.action
    host = SEARCH if action == "search" else PUBLIC
    fields = {k: str(v) if isinstance(v, date) else v for k, v in vars(args).items()
              if k not in ("action", "channel", "run")}
    client = Client(max_requests=args.max_requests, min_interval=0, transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    items, meta, error = [], {}, None
    try:
        if args.max_requests < 1 or not 1 <= getattr(args, "limit", 1) <= 100:
            raise Failure("usage", "expected --max-requests >= 1 and --limit between 1 and 100")
        if action == "search":
            if args.url and not re.fullmatch(r"https?://[^/\s?#]+[^\s]*", args.url):
                raise Failure("usage", "--url requires an HTTP or HTTPS URL")
            if (not (args.query or args.url) or (args.query is not None and not args.query.strip())
                    or (args.since and args.until and args.since >= args.until)):
                raise Failure("usage", "expected a nonempty query and --since earlier than --until")
            data = fetch(client, host, "feed.searchPosts", q=args.query or "*", url=args.url, sort="top" if args.top else "latest",
                         limit=args.limit, since=args.since, until=args.until, author=args.author, domain=args.domain)
            for post in entries(data, "posts")[:args.limit]:
                if args.author and not matches(post["author"], args.author):
                    raise Failure("url_mismatch", "Bluesky search returned a different author")
                items.append(post_item(post))
        elif action == "timeline":
            timeline(client, args, items)
        else:
            items = conversation(client, args, meta)
    except CapReached:
        meta["truncated"] = "request_cap"
        error = env.error("upstream_error", "request cap reached before completing timeline")
    except HttpError as exc:
        error = env.error(exc.code, str(exc))
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError):
        error = env.error("upstream_error", f"malformed Bluesky {action} response")
    outcome = error["code"] if error else "ok" if items else "no_results"
    meta["attempts"] = [env.attempt(host, outcome, client.requests_made, env.elapsed_ms(started))]
    return env.envelope("bluesky", action, fields, host, items, meta=meta, error=error)


def probe(ctx, attempts, failure_type):
    args = argparse.Namespace(action="search", query="python", url=None, top=False, author=None, domain=None,
                              since=None, until=None, max_requests=1, limit=1)
    result = run(args, ctx)
    attempts.extend(result["meta"]["attempts"])
    if result["error"]:
        raise failure_type(result["error"]["code"], result["error"]["message"])
    if not result["items"]:
        raise failure_type("upstream_error", "Bluesky probe search returned no posts")
    return "Anonymous Bluesky search answered (1 request)", {"search": "ok"}
