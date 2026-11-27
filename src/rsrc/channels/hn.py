"""Hacker News threads via Algolia, with one public page request per poll."""

import argparse
import json
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urlsplit

from rsrc import envelope as env
from rsrc.html_markdown import VOID
from rsrc.http import Client, HttpError, NotFound, raise_refusal, single_hop_transport, urllib_transport

CHANNEL = "hn"
BACKEND = "algolia"
SOURCE = "hackernews"
API = "https://hn.algolia.com/api/v1"
DISCUSSION = "https://news.ycombinator.com/item?id="
MAX_LIMIT = 100
MAX_COMMENTS = 1000

_ID = re.compile(r"[0-9]{1,15}")
_BLANK = re.compile(r"[\x00-\x20\x7f]")  # urlsplit silently drops tabs and newlines inside a URL
_DISPLAY_NONE = re.compile(r"(?:^|;)\s*display\s*:\s*none\s*(?:!\s*important\s*)?(?:;|$)", re.I)


def register(subparsers) -> None:
    parser = subparsers.add_parser("hn", help="Hacker News stories and threads via the Algolia HN API (no key)")
    actions = parser.add_subparsers(dest="hn_action", metavar="ACTION", required=True)

    search = actions.add_parser("search", help="search stories")
    search.add_argument("query", metavar="QUERY")
    search.add_argument("--since", type=_date, metavar="YYYY-MM-DD",
                        help="start date, inclusive (UTC); default: no date filter")
    search.add_argument("--until", type=_date, metavar="YYYY-MM-DD", help="end date, exclusive (UTC)")
    search.add_argument("--sort", choices=("relevance", "date"), default="relevance",
                        help="relevance (default) or date, newest first")
    search.add_argument("--limit", type=int, default=30, help=f"stories, 1-{MAX_LIMIT} (default 30)")
    search.set_defaults(run=run_search, action="search")

    read = actions.add_parser("read", help="one story, poll, or comment, then the comments below it")
    read.add_argument("item", metavar="URL_OR_ID", help="news.ycombinator.com item URL, or the numeric ID")
    read.add_argument("--comments", type=int, default=50,
                      help=f"comments, 0-{MAX_COMMENTS}, depth-first in posting order, not ranked (default 50)")
    read.set_defaults(run=run_read, action="read")


def _date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a YYYY-MM-DD date: {value}") from None


def run_search(args, ctx) -> dict:
    input_ = {"query": args.query, "since": _iso(args.since), "until": _iso(args.until),
              "sort": args.sort, "limit": args.limit}
    problem = _search_problem(args)
    if problem:
        return env.usage(CHANNEL, "search", problem)
    params = {"query": args.query, "tags": "story", "hitsPerPage": args.limit}
    bounds = [f"created_at_i{op}{_midnight(day)}" for op, day in ((">=", args.since), ("<", args.until)) if day]
    if bounds:
        params["numericFilters"] = ",".join(bounds)
    endpoint = "search_by_date" if args.sort == "date" else "search"
    attempts: list[dict] = []
    try:
        hits = fetch(ctx, f"{endpoint}?{urlencode(params)}", parse_hits, attempts)
    except HttpError as e:
        return env.envelope(CHANNEL, "search", input_, BACKEND, meta={"attempts": attempts},
                            error=env.error(e.code, str(e)))
    return env.envelope(CHANNEL, "search", input_, BACKEND, [_story_item(h) for h in hits[:args.limit]],
                        meta={"attempts": attempts})


def _search_problem(args) -> str | None:
    if not args.query.strip():
        return "QUERY is empty"
    if not 1 <= args.limit <= MAX_LIMIT:
        return f"--limit must be between 1 and {MAX_LIMIT}, got {args.limit}"
    if args.since and args.until and args.until <= args.since:
        return "--until must be later than --since"
    return None


def run_read(args, ctx) -> dict:
    input_ = {"item": args.item, "comments": args.comments}
    if not 0 <= args.comments <= MAX_COMMENTS:
        return env.usage(CHANNEL, "read", f"--comments must be between 0 and {MAX_COMMENTS}, got {args.comments}")
    item_id = parse_item(args.item)
    if item_id is None:
        return env.usage(CHANNEL, "read",
                         f"expected a news.ycombinator.com item URL or a numeric ID, got {args.item!r}")
    attempts: list[dict] = []
    try:
        root = fetch(ctx, f"items/{item_id}", _tree, attempts, missing=f"Hacker News has no item {item_id}")
    except HttpError as e:
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta={"attempts": attempts},
                            error=env.error(e.code, str(e)))
    meta = {"attempts": attempts}
    returned_id = str(root["id"])
    if returned_id != item_id:
        attempts[-1]["outcome"] = "url_mismatch"
        meta["rejected"] = [{"url": DISCUSSION + returned_id, "reason": "url_mismatch"}]
        return env.envelope(CHANNEL, "read", input_, BACKEND, meta=meta,
                            error=env.error("url_mismatch", f"asked for item {item_id}, the API returned {returned_id}"))
    items, total = _thread(root, args.comments)
    returned = len(items) - 1
    if returned < total:
        meta["notes"] = [f"{returned} of {total} comments returned"]
    error = None
    if root.get("type") == "poll":
        items[0]["raw"]["poll"], error = _poll_results(ctx, root, attempts)
    return env.envelope(CHANNEL, "read", input_, BACKEND, items, meta=meta, error=error)


def parse_item(value: str) -> str | None:
    """The item ID from a bare number or a `https://news.ycombinator.com/item?id=N` URL; None for anything else."""
    value = value.strip()
    if not _ID.fullmatch(value):
        if _BLANK.search(value):
            return None
        try:
            parts = urlsplit(value)
        except ValueError:
            return None
        ids = parse_qs(parts.query, keep_blank_values=True).get("id", [])
        if ((parts.scheme, parts.netloc.lower(), parts.path) != ("https", "news.ycombinator.com", "/item")
                or len(ids) != 1 or not _ID.fullmatch(ids[0])):
            return None
        value = ids[0]
    return str(int(value))


def fetch(ctx, path: str, parse, attempts: list, *, missing: str | None = None):
    """One GET of `API/path`: the JSON answer as `parse` returns it. Appends one attempt.

    `parse` raises ValueError for JSON of the wrong shape. `missing` is what a 404 means when the
    path names one item. Raises HttpError, whose `code` is the envelope code for the kind of
    failure; nothing is retried."""
    client = Client(min_interval=1, transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    try:
        status, body = client.request("GET", f"{API}/{path}")
        if status == 404 and missing:
            raise NotFound(f"{missing} (HTTP 404)")
        raise_refusal(status, body, "Hacker News API")
        if status != 200:
            raise HttpError(f"Hacker News API answered HTTP {status}")
        try:
            value = parse(json.loads(body))
        except (ValueError, RecursionError):  # RecursionError: nesting too deep for the JSON parser
            raise HttpError(f"Hacker News API answered HTTP {status} with malformed or unexpected JSON") from None
    except HttpError as e:
        attempts.append(env.attempt(BACKEND, e.code, client.requests_made, env.elapsed_ms(started)))
        raise
    attempts.append(env.attempt(BACKEND, "ok" if value else "no_results", client.requests_made,
                                env.elapsed_ms(started)))
    return value


def parse_hits(data) -> list[dict]:
    """The answer's hits, if each is an object with an ID and, when it has text, text that is a string."""
    hits = data.get("hits") if isinstance(data, dict) else None
    if not isinstance(hits, list) or not all(
            isinstance(h, dict) and (isinstance(h.get("objectID"), str) or _is_int(h.get("objectID")))
            and _is_text(h.get("story_text")) for h in hits):
        raise ValueError("no list of hits")
    return hits


def _story_item(hit: dict) -> dict:
    story_id = str(hit["objectID"])
    return env.item(
        id=story_id,
        url=DISCUSSION + story_id,
        title=hit.get("title"),
        author=hit.get("author"),
        published_at=_when(hit.get("created_at_i")),
        text=plain_text(hit.get("story_text")),
        source=SOURCE,
        engagement=_counts(points=hit.get("points"), comments=hit.get("num_comments")),
        raw=hit,
    )


def _tree(data) -> dict:
    """The item answer, if every node in it is an object with an integer `id`, a list of `children`
    and, when it has text, text that is a string."""
    stack = [data]
    while stack:
        node = stack.pop()
        if (not isinstance(node, dict) or not _is_int(node.get("id"))
                or not isinstance(node.get("children"), list) or not _is_text(node.get("text"))):
            raise ValueError("not an item tree")
        stack.extend(node["children"])
    return data


def _thread(root: dict, limit: int) -> tuple[list[dict], int]:
    """The requested item, then up to `limit` of the comments below it, depth-first with children in
    the API's order; and how many comments there are.

    That order is posting order (IDs and timestamps ascend at every level of a recorded thread), not
    Hacker News's ranking, and Hacker News publishes no comment scores: position says nothing of quality.

    A comment without text (deleted or dead) is neither returned nor counted; its replies are."""
    items = [_node_item(root, 0, plain_text(root.get("text")))]
    total = 0
    stack = [(child, 1) for child in reversed(root["children"])]
    while stack:
        node, depth = stack.pop()
        stack.extend((child, depth + 1) for child in reversed(node["children"]))
        text = plain_text(node.get("text"))
        if text is None:
            continue
        total += 1
        if total <= limit:
            items.append(_node_item(node, depth, text))
    return items, total


def _node_item(node: dict, depth: int, text: str | None) -> dict:
    node_id = str(node["id"])
    return env.item(
        id=node_id,
        url=DISCUSSION + node_id,
        title=node.get("title"),
        author=node.get("author"),
        published_at=_when(node.get("created_at_i")),
        text=text,
        source=SOURCE,
        engagement=_counts(points=node.get("points")),
        # The subtree would repeat every item below this one; the direct child IDs keep the structure.
        raw={"item": {**node, "children": [child["id"] for child in node["children"]]}, "depth": depth},
    )


def _poll_results(ctx, root: dict, attempts: list) -> tuple[dict, dict | None]:
    ids = root.get("options")
    if not isinstance(ids, list) or not ids or not all(_is_int(i) and i > 0 for i in ids):
        return {"status": "unavailable", "options": []}, None

    options = [{"id": i, "text": None, "points": None} for i in ids]
    transport = single_hop_transport if ctx.transport is urllib_transport else ctx.transport
    client = Client(min_interval=1, max_requests=1, transport=transport)
    started = time.monotonic()
    item_id = str(root["id"])
    try:
        status, body = client.request("GET", DISCUSSION + item_id)
        raise_refusal(status, b"", "Hacker News poll page")
        if status != 200:
            raise HttpError(f"Hacker News poll page answered HTTP {status}")
        page = _PollPage({str(i) for i in ids})
        try:
            page.feed(body.decode(errors="replace"))
            page.close()
        except (AssertionError, ValueError):
            raise HttpError("Hacker News poll page contained malformed HTML") from None
        if page.item_id != item_id:
            raise HttpError(f"Hacker News poll page did not contain submission {item_id}")
    except HttpError as e:
        attempts.append(env.attempt("hackernews", e.code, client.requests_made, env.elapsed_ms(started)))
        return {"status": "unavailable", "options": options}, env.error(e.code, str(e))

    for option in options:
        key = str(option["id"])
        text = " ".join("".join(page.labels.get(key, [])).split())
        score = "".join(page.scores.get(key, [])).strip()
        match = re.fullmatch(r"([0-9]{1,15})\s+points?", score)
        option["text"] = text or None
        option["points"] = int(match[1]) if text and match else None
    scored = sum(option["points"] is not None for option in options)
    status = "unavailable"
    if scored:
        status = "ok" if scored == len(options) else "partial"
    attempts.append(env.attempt("hackernews", "ok", client.requests_made, env.elapsed_ms(started)))
    return {"status": status, "options": options}, None


class _PollPage(HTMLParser):
    def __init__(self, option_ids: set[str]):
        super().__init__(convert_charrefs=True)
        self.option_ids = option_ids
        self.item_id = None
        self.labels: dict[str, list[str]] = {}
        self.scores: dict[str, list[str]] = {}
        self._label_id = None
        self._capture: tuple[str, list[str]] | None = None
        self._capture_depth = 0
        self._hidden: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if self._hidden or "hidden" in attrs or _DISPLAY_NONE.search(attrs.get("style") or ""):
            if tag not in VOID:
                self._hidden.append(tag)
            return
        if self._capture:
            if tag == self._capture[0]:
                self._capture_depth += 1
            if tag in ("br", "p"):
                self._capture[1].append(" ")
            return
        classes = set((attrs.get("class") or "").split())
        node_id = attrs.get("id") or ""
        if tag == "tr":
            if {"athing", "submission"} <= classes:
                self.item_id = node_id
            self._label_id = node_id if classes == {"athing"} and node_id in self.option_ids else None
        elif tag == "td" and self._label_id and "comment" in classes:
            self._capture = (tag, self.labels.setdefault(self._label_id, []))
        elif tag == "span" and classes == {"score"} and node_id.startswith("score_"):
            option_id = node_id.removeprefix("score_")
            if option_id in self.option_ids:
                self._capture = (tag, self.scores.setdefault(option_id, []))

    def handle_endtag(self, tag):
        if self._hidden:
            for index in range(len(self._hidden) - 1, -1, -1):
                if self._hidden[index] == tag:
                    del self._hidden[index:]
                    break
            return
        if self._capture:
            if tag == "p":
                self._capture[1].append(" ")
            if self._capture[0] == tag:
                if self._capture_depth:
                    self._capture_depth -= 1
                else:
                    self._capture = None
        if tag == "tr":
            self._label_id = None

    def handle_data(self, data):
        if self._capture and not self._hidden:
            self._capture[1].append(data)


def _iso(day: date | None) -> str | None:
    return day.isoformat() if day else None


def _midnight(day: date) -> int:
    """The day's first second in UTC, as Unix seconds."""
    return int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())


def _when(seconds) -> str | None:
    """Unix seconds as ISO 8601 UTC (`2026-11-13T18:02:11Z`); None if the API gave no integer a date can hold."""
    if not _is_int(seconds):
        return None
    try:
        return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (OverflowError, OSError, ValueError):
        return None


def _counts(**counts) -> dict | None:
    """Only the integer counts the API reported; None if there are none."""
    kept = {k: v for k, v in counts.items() if _is_int(v)}
    return kept or None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_text(value) -> bool:
    return value is None or isinstance(value, str)


class _PlainText(HTMLParser):
    """Hacker News comment HTML as paragraphs: `<p>` starts one, a link is its `href`,
    any other tag is dropped and its content kept. Entities arrive decoded."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.paragraphs = [""]
        self._in_link = False

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.paragraphs.append("")
            self._in_link = False  # an unclosed link ends with its paragraph
        elif tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.paragraphs[-1] += href
                self._in_link = True  # the visible text repeats the URL, shortened

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False

    def handle_data(self, data):
        if not self._in_link:
            self.paragraphs[-1] += data


def plain_text(html: str | None) -> str | None:
    """Hacker News HTML as plain text, paragraphs separated by a blank line; None if nothing is left."""
    if not html:
        return None
    parser = _PlainText()
    parser.feed(html)
    parser.close()
    # Newlines and trailing spaces go; leading spaces stay, since they indent a code block's first line.
    paragraphs = [p.strip("\n").rstrip() for p in parser.paragraphs]
    return "\n\n".join(p for p in paragraphs if p.strip()) or None
