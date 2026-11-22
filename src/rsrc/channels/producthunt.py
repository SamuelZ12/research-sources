"""Anonymous Product Hunt discovery over its stateless Streamable HTTP MCP endpoint."""

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from rsrc import envelope as env
from rsrc.http import CapReached, Client, HttpError

CHANNEL = "producthunt"
BACKEND = "producthunt-mcp"
ENDPOINT = "https://mcp.producthunt.com/mcp"
VERSION = "2026-07-28"
ZONE = "America/Los_Angeles"
SLUG = r"[a-z0-9][a-z0-9_-]*"


class Failure(HttpError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def register(subparsers):
    parser = subparsers.add_parser(CHANNEL, help="Product Hunt products and official launch rankings")
    actions = parser.add_subparsers(dest="action", required=True)
    search = actions.add_parser("search", help="find products by name or keyword")
    search.add_argument("query")
    read = actions.add_parser("read", help="read a product or an exact launch")
    read.add_argument("product", metavar="URL_OR_SLUG")
    board = actions.add_parser("leaderboard", help="ranked launches, Pacific time")
    board.add_argument("--date", help="day, ISO week, month or year (default today in Pacific time)")
    for command in (search, board):
        command.add_argument("--limit", type=int, choices=range(1, 26), metavar="1-25", default=10)
    for command in (search, read, board):
        command.add_argument("--max-requests", type=int, default=2)
        command.set_defaults(run=run)


def rpc_reply(body, request_id):
    text = body.decode("utf-8-sig").strip()
    if text.startswith("{"):
        replies = [json.loads(text)]
    else:
        replies = []
        for event in text.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
            data = [line[5:].removeprefix(" ") for line in event.split("\n") if line.startswith("data:")]
            if data:
                message = json.loads("\n".join(data))
                if "id" in message:
                    replies.append(message)
    reply, = replies
    if reply["jsonrpc"] != "2.0" or type(reply["id"]) is not int or reply["id"] != request_id:
        raise ValueError
    return reply


def tool(client, name, **arguments):
    request_id = client.requests_made + 1
    params = {"name": name, "arguments": arguments, "_meta": {
        "io.modelcontextprotocol/protocolVersion": VERSION,
        "io.modelcontextprotocol/clientInfo": {"name": "rsrc", "version": "0.1"},
        "io.modelcontextprotocol/clientCapabilities": {},
    }}
    payload = {"jsonrpc": "2.0", "method": "tools/call", "id": request_id, "params": params}
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
               "MCP-Protocol-Version": VERSION, "Mcp-Method": "tools/call", "Mcp-Name": name}
    status, body = client.request("POST", ENDPOINT, headers, json.dumps(payload).encode())
    if status in (401, 403, 429) or body.lstrip().startswith(b"<"):
        code = {401: "auth_failed", 429: "rate_limited"}.get(status, "blocked")
        raise Failure(code, f"Product Hunt MCP HTTP {status} ({code}) during {name}")
    if status != 200:
        raise HttpError(f"Product Hunt MCP HTTP {status} during {name} (protocol {VERSION})")
    try:
        reply = rpc_reply(body, request_id)
        if "error" in reply:
            raise HttpError(f"MCP refused {name}: {reply['error'].get('message', 'RPC error')}")
        result = reply["result"]
    except (KeyError, ValueError, TypeError, AttributeError):
        raise HttpError(f"unexpected MCP response to {name}") from None
    if result.get("_meta", {}).get("mcp/www_authenticate"):
        raise Failure("auth_failed", f"Product Hunt MCP requires authentication for {name}")
    structured = result.get("structuredContent", {})
    if structured.get("status") == "error":
        error = structured["error"]
        code = {"product_not_found": "not_found", "invalid_input": "usage",
                "rate_limited": "rate_limited", "unauthorized": "auth_failed"}.get(error["code"], "upstream_error")
        raise Failure(code, f"{name}: {error['message']}")
    if result.get("isError") is not False or structured.get("status") != "ok":
        raise HttpError(f"Product Hunt MCP tool {name} failed or its response changed")
    return structured["data"]


def target(value):
    if re.search(r"[\x00-\x20\x7f]", value):
        raise Failure("usage", "invalid whitespace in Product Hunt URL or slug")
    if re.fullmatch(SLUG, value):
        return value, None
    try:
        url = urlsplit(value)
    except ValueError:
        raise Failure("usage", "malformed Product Hunt URL") from None
    if url.scheme != "https" or url.netloc.lower() not in ("www.producthunt.com", "producthunt.com"):
        raise Failure("usage", "expected a Product Hunt URL or product slug")
    parts = url.path.strip("/").split("/")
    if not all(re.fullmatch(SLUG, part) for part in parts):
        raise Failure("usage", "invalid Product Hunt URL path")
    match parts:
        case ["products", product] | ["products", product, "reviews"]:
            launches = parse_qs(url.query, keep_blank_values=True).get("launch")
            if launches and (len(launches) != 1 or not re.fullmatch(SLUG, launches[0])):
                raise Failure("usage", "invalid launch selector")
            return product, launches[0] if launches else None
        case ["products", product, "launches", launch]:
            return product, launch
        case ["posts", launch]:
            return None, launch
        case ["p", _, _]:
            raise Failure("upstream_error", "Product Hunt MCP does not expose forum discussions or replies")
    raise Failure("usage", "unrecognized Product Hunt URL path")


def identity(row, kind):
    slug = row["slug"]
    canonical = f"https://www.producthunt.com/{kind}/{slug}"
    if not re.fullmatch(SLUG, slug) or row["url"].split("?")[0] != canonical or not row["name"].strip():
        raise ValueError
    return slug, canonical


def stamp(value):
    if value is None:
        return None
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        raise ValueError
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def counts(row, rating_key):
    rating = row[rating_key]
    count = row["reviews_count"]
    if (rating is not None and (type(rating) not in (float, int) or not 0 <= rating <= 5)
            or type(count) is not int or count < 0):
        raise ValueError
    return {"rating": rating, "reviews": count}


def rows(data):
    found = data["results"]
    if not isinstance(found, list) or type(data["has_more"]) is not bool:
        raise ValueError
    if not found and (data["has_more"] or data.get("empty_reason") not in ("no_match", "filtered_out")):
        raise HttpError("Product Hunt returned an unexplained empty result")
    for row in found:
        identity(row, "products")
        if not isinstance(row["launches"], list) or not isinstance(row["tagline"], str):
            raise ValueError
        counts(row, "rating")
        if type(row["top_launch_points"]) is not int or not isinstance(row["makers"], list):
            raise ValueError
        for launch in row["launches"]:
            identity(launch, "posts")
            if (type(launch["points"]) is not int or not isinstance(launch["tagline"], str)
                    or not stamp(launch["launched_at"])):
                raise ValueError
    return found


def search_rows(client, query, limit):
    return rows(tool(client, "search_products", query=query, limit=limit))


def product_item(product, raw, latest, engagement):
    slug, url = identity(product, "products")
    return env.item(id=slug, url=url, title=product["name"], text=product.get("description") or product["tagline"],
                    published_at=stamp(latest), source=CHANNEL, engagement=engagement, raw=raw)


def launch_item(launch, raw, engagement):
    slug, url = identity(launch, "posts")
    return env.item(id=slug, url=url, title=launch["name"], text=launch["tagline"], source=CHANNEL,
                    published_at=stamp(launch["launched_at"]), engagement={**engagement, "points": launch["points"]},
                    raw=raw)


def read(client, product, launch, notes):
    if launch:
        query = re.sub(r"-[0-9]+$", "", launch).replace("-", " ")
        found = search_rows(client, query, 10)
        matches = [(row, post) for row in found if product in (None, row["slug"])
                   for post in row["launches"] if post["slug"] == launch]
        if len(matches) != 1:
            raise HttpError("could not verify the exact launch in Product Hunt's bounded search results")
        row, post = matches[0]
        product = row["slug"]
    profile = tool(client, "get_product_profile", product_name=product)
    detail = profile["product"]
    if identity(detail, "products")[0] != product:
        raise Failure("url_mismatch", "Product Hunt returned a different product slug")
    if not isinstance(profile["makers"], list) or not isinstance(profile["review_highlights"], list):
        raise ValueError
    if (not isinstance(detail["description"], str) or not isinstance(detail["tagline"], str)
            or any(not isinstance(m["name"], str) for m in profile["makers"])
            or any(type(h["rating"]) is not int or not isinstance(h["excerpt"], str)
                   for h in profile["review_highlights"])):
        raise ValueError
    engagement = counts(detail, "reviews_rating")
    notes.append("Review highlights are selected product excerpts. Launch discussion is unavailable.")
    if launch:
        return launch_item(post, {"launch": post, "profile": profile}, engagement)
    found = search_rows(client, product, 10)
    row = next((r for r in found if r["slug"] == product), None)
    if row is None:
        notes.append("No exact search record for launch dates and points.")
    else:
        engagement["top_launch_points"] = row["top_launch_points"]
    return product_item(detail, {"profile": profile, "launches": row["launches"] if row else []},
                        row["latest_launch_at"] if row else None, engagement)


def leaderboard(client, period, limit):
    data = tool(client, "get_product_leaderboard", date=period, limit=limit)
    window = data["period"]
    if window["date"] != period:
        raise Failure("url_mismatch", "Product Hunt returned a different leaderboard period")
    items = []
    for rank, row in enumerate(rows(data), 1):
        post, = row["launches"]
        local = datetime.fromisoformat(post["launched_at"]).astimezone(ZoneInfo(ZONE)).date().isoformat()
        if type(post["rank"]) is not int or post["rank"] != rank or not window["launched_after"] <= local <= window["launched_before"]:
            raise HttpError("Product Hunt returned an inconsistent launch rank or date")
        items.append(launch_item(post, {"rank": rank, "period": window, "product": row}, {}))
    return items


def run(args, ctx):
    action = args.action
    inputs = {k: v for k, v in vars(args).items() if k in ("query", "limit", "product", "date", "max_requests")}
    if action == "leaderboard":
        inputs["date"] = args.date or datetime.now(ZoneInfo(ZONE)).date().isoformat()
        inputs["timezone"] = ZONE
    if args.max_requests < 1 or (action == "search" and not args.query.strip()):
        return env.usage(CHANNEL, action, "expected a positive request cap and a nonempty query")
    client = Client(max_requests=args.max_requests, transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    items, notes, error, truncated = [], [], None, None
    try:
        if action == "read":
            product, launch = target(args.product)
        if action == "read":
            items = [read(client, product, launch, notes)]
        elif action == "leaderboard":
            items = leaderboard(client, inputs["date"], args.limit)
        else:
            found = search_rows(client, args.query, args.limit)
            items = [product_item(r, r, r["latest_launch_at"],
                                  {**counts(r, "rating"), "top_launch_points": r["top_launch_points"]})
                     for r in found]
            if found and all(r["match"]["strength"] == "weak" for r in found):
                notes.append("Product Hunt rated every match weak.")
    except HttpError as e:
        error = env.error(e.code, str(e))
    except CapReached as e:
        error, truncated = env.error("upstream_error", str(e)), "request_cap"
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        error = env.error("upstream_error", f"unexpected Product Hunt MCP {action} response shape")
    outcome = error["code"] if error else ("ok" if items else "no_results")
    attempt = env.attempt(BACKEND, outcome, client.requests_made, env.elapsed_ms(started))
    return env.envelope(CHANNEL, action, inputs, BACKEND, items, error=error,
                        meta={"notes": notes, "attempts": [attempt], "truncated": truncated})
