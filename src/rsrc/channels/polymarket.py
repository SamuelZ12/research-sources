"""Polymarket via the keyless gamma API: `search` (open events, each with its markets' current prices).

Through `rsrc.http.Client`. No cookie, no key, no retry.
"""

import json
import math
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlencode

from rsrc import envelope as env
from rsrc.http import Client, HttpError, raise_refusal

CHANNEL = "polymarket"
BACKEND = "gamma"
SOURCE = "polymarket"
API = "https://gamma-api.polymarket.com/public-search"
EVENT_URL = "https://polymarket.com/event/"
HEADERS = {"user-agent": "Mozilla/5.0"}
DEFAULT_LIMIT = 10
MAX_LIMIT = 25
MAX_REQUESTS = 5
MAX_LINES = 10

EVENT_FIELDS = ("id", "slug", "title", "description", "startDate", "endDate", "createdAt", "updatedAt", "active",
                "closed", "volume", "volume24hr", "volume1wk", "volume1mo", "liquidity", "commentCount")
MARKET_FIELDS = ("id", "question", "slug", "outcomes", "outcomePrices", "volume", "liquidity", "endDate", "active",
                 "closed", "lastTradePrice", "bestBid", "bestAsk", "oneDayPriceChange", "oneWeekPriceChange",
                 "oneMonthPriceChange")


def register(subparsers) -> None:
    parser = subparsers.add_parser("polymarket", help="Polymarket prediction markets via the gamma API (no key)")
    actions = parser.add_subparsers(dest="polymarket_action", metavar="ACTION", required=True)

    search = actions.add_parser("search", help="open events matching a query, each with its markets' prices")
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--limit", type=int, default=DEFAULT_LIMIT,
                        help=f"events, 1-{MAX_LIMIT} (default {DEFAULT_LIMIT}); the API pages them, and one "
                             f"command makes at most {MAX_REQUESTS} requests")
    search.set_defaults(run=run_search, action="search")


def run_search(args, ctx) -> dict:
    input_ = {"query": args.query, "limit": args.limit}
    if not args.query.strip():
        return env.usage(CHANNEL, "search", "QUERY is empty")
    if not 1 <= args.limit <= MAX_LIMIT:
        return env.usage(CHANNEL, "search", f"--limit must be between 1 and {MAX_LIMIT}, got {args.limit}")
    client = Client(min_interval=1, transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    found: dict[str, dict] = {}  # event ID -> event, in the API's order
    meta = {}
    failure = None
    try:
        for page in range(1, MAX_REQUESTS + 1):
            events, more = fetch_page(client, args.query, page, args.limit)
            before = len(found)
            for event in events:
                found.setdefault(str(event["id"]), event)
            if len(found) >= args.limit or len(found) == before or not more:
                break
        else:
            meta["truncated"] = "request_cap"
    except HttpError as e:  # the events of earlier pages are kept
        failure = e
    items = [_item(event) for event in list(found.values())[:args.limit]]
    outcome = failure.code if failure else "ok" if items else "no_results"
    meta["attempts"] = [env.attempt(BACKEND, outcome, client.requests_made, env.elapsed_ms(started))]
    return env.envelope(CHANNEL, "search", input_, BACKEND, items, meta=meta,
                        error=env.error(failure.code, str(failure)) if failure else None)


def fetch_page(client: Client, query: str, page: int, limit: int | None = None) -> tuple[list[dict], bool]:
    """One GET of a page of open events matching `query`: the events, and whether more pages may follow
    (true unless the answer says otherwise).

    Raises HttpError, whose `code` is the envelope code for the kind of failure; nothing is retried."""
    params = {"q": query, "page": page, "events_status": "active", "keep_closed_markets": 0}
    if limit is not None:
        params["limit_per_type"] = limit
    status, body = client.request("GET", f"{API}?{urlencode(params)}", headers=HEADERS)
    raise_refusal(status, body, "Polymarket API")
    if status != 200:
        raise HttpError(f"Polymarket API answered HTTP {status}")
    try:
        data = json.loads(body)
        events = _events(data)
    except (ValueError, RecursionError):  # RecursionError: nesting too deep for the JSON parser
        raise HttpError(f"Polymarket API answered HTTP {status} with malformed or unexpected JSON") from None
    pagination = data.get("pagination")
    more = not (isinstance(pagination, dict) and pagination.get("hasMore") is False)
    return events, more


def _events(data) -> list[dict]:
    """The answer's events, if each is an object with an ID, a slug and, when it has markets, a list of
    market objects: what an item cannot be built without.

    A search that matches nothing is answered without `events` and with `pagination.totalResults` 0."""
    if not isinstance(data, dict):
        raise ValueError("not an object")
    if "events" not in data:
        pagination = data.get("pagination")
        total = pagination.get("totalResults") if isinstance(pagination, dict) else None
        if type(total) is int and total == 0:
            return []
    events = data.get("events")
    if not isinstance(events, list) or not all(_is_event(e) for e in events):
        raise ValueError("no list of events")
    return events


def _is_event(event) -> bool:
    if not isinstance(event, dict):
        return False
    event_id, slug, markets = event.get("id"), event.get("slug"), event.get("markets")
    if isinstance(event_id, bool) or not isinstance(event_id, (str, int)) or event_id == "":
        return False
    if not isinstance(slug, str) or not slug:
        return False
    return markets is None or isinstance(markets, list) and all(isinstance(m, dict) for m in markets)


def _item(event: dict) -> dict:
    raw = record(event)
    started = _utc(raw["startDate"])
    return env.item(
        id=str(raw["id"]),
        url=EVENT_URL + quote(str(raw["slug"]), safe=""),
        title=raw["title"],
        published_at=f"{started:%Y-%m-%dT%H:%M:%SZ}" if started else None,
        text=text(raw),
        source=SOURCE,
        engagement=_engagement(raw),
        raw=raw,
    )


def _engagement(raw: dict) -> dict | None:
    """The event's volume and liquidity in whole US dollars and its comment count; only those it has."""
    kept = {}
    for name in ("volume", "liquidity"):
        dollars = _number(raw[name])
        if dollars is not None:
            kept[name] = round(dollars)
    comments = raw["commentCount"]
    if isinstance(comments, int) and not isinstance(comments, bool):
        kept["comments"] = comments
    return kept or None


def record(event: dict) -> dict:
    """An item's `raw`: the event's chosen fields under the API's names, then every market it came with.

    A field the API lacks is None. Values pass through as sent, except a market's `outcomes` and
    `outcomePrices`, which the API sends as JSON text."""
    markets = event.get("markets")
    return {**{name: event.get(name) for name in EVENT_FIELDS},
            "markets": None if markets is None else [_market(m) for m in markets]}


def _market(market: dict) -> dict:
    return {**{name: market.get(name) for name in MARKET_FIELDS},
            "outcomes": _json_list(market.get("outcomes"), _name),
            "outcomePrices": _json_list(market.get("outcomePrices"), _number)}


def text(raw: dict) -> str | None:
    """One line per open market of a record, highest volume first: `<question>: <outcome> <price>%, … (ends
    YYYY-MM-DD)`. At most MAX_LINES, then a count of the open markets left out. None without an open market.

    A market is open unless it is closed or inactive; one without a question has nothing to show. The
    filter is needed: the API returns closed markets of an open event despite `keep_closed_markets=0`
    (19 of 67 in the two recorded pages)."""
    markets = [m for m in raw["markets"] or []
               if m["closed"] is not True and m["active"] is not False
               and isinstance(m["question"], str) and m["question"].strip()]
    markets.sort(key=_volume, reverse=True)  # stable: equal volumes keep the API's order
    lines = [_line(m) for m in markets[:MAX_LINES]]
    if len(markets) > MAX_LINES:
        lines.append(f"{len(markets) - MAX_LINES} more markets")
    return "\n".join(lines) or None


def _line(market: dict) -> str:
    outcomes, prices = market["outcomes"], market["outcomePrices"]
    question = " ".join(market["question"].split())  # one market, one line
    if not outcomes or not prices or len(outcomes) != len(prices):
        return question
    line = f"{question}: " + ", ".join(f"{name} {price * 100:.1f}%" for name, price in zip(outcomes, prices))
    ends = _utc(market["endDate"])
    return f"{line} (ends {ends:%Y-%m-%d})" if ends else line


def _volume(market: dict) -> float:
    """A market's volume for sorting; one without a number sorts last."""
    volume = _number(market["volume"])
    return -math.inf if volume is None else volume


def _utc(value) -> datetime | None:
    """An ISO 8601 time from the API (`2026-11-30T23:59:00Z`) in UTC; None if it is missing or not one."""
    if not isinstance(value, str):
        return None
    try:
        moment = datetime.fromisoformat(value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None


def _json_list(value, convert) -> list | None:
    """A list the API sent as JSON text, each element through `convert`; None unless every element converts."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):  # RecursionError: nesting too deep for the JSON parser
            return None
    if not isinstance(value, list):
        return None
    converted = [convert(element) for element in value]
    return None if any(element is None for element in converted) else converted


def _name(value) -> str | None:
    return value if isinstance(value, str) else None


def _number(value) -> float | None:
    """A finite number the API sent as a number or as decimal text; None for anything else."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):  # OverflowError: an integer too large for a float
        return None
    return number if math.isfinite(number) else None
