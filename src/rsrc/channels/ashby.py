"""Read listed roles or one published posting from Ashby's public board API."""

import json
import re
import time
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote, unquote, urlsplit

from rsrc import envelope as env
from rsrc.http import Client, HttpError, single_hop_transport, urllib_transport

BACKEND = "ashby-posting-api"
API = "https://api.ashbyhq.com/posting-api/job-board/"
BOARD = re.compile(r"[\w .-]+")
ROLE = re.compile(r"[a-zA-Z0-9_-]+")
GUIDANCE = "Use the board name from the jobs.ashbyhq.com link on the company's careers page."


def register(subparsers):
    parser = subparsers.add_parser("ashby", help="open roles on a company's Ashby job board")
    actions = parser.add_subparsers(required=True)
    search = actions.add_parser("search", help="list open roles in API order")
    search.add_argument("board", help="board name or jobs.ashbyhq.com URL")
    search.add_argument("--title", help="words in the title (all must match)")
    search.add_argument("--team", help="substring in team or department")
    search.add_argument("--location", help="substring in primary or secondary location")
    search.add_argument("--since", type=date.fromisoformat, metavar="YYYY-MM-DD",
                        help="last published on or after this UTC date")
    search.add_argument("--limit", type=int, help="maximum roles to return (default: all)")
    search.set_defaults(action="search", run=run)
    read = actions.add_parser("read", help="read a job URL or a role ID with --board")
    read.add_argument("role")
    read.add_argument("--board", help="board for a bare role ID")
    read.set_defaults(action="read", run=run)


def board_role(value):
    if value.lower().startswith("jobs.ashbyhq.com/"):
        value = "https://" + value
    url = urlsplit(value)
    if url.scheme or url.netloc:
        if url.scheme not in ("https", "http") or url.netloc.lower() != "jobs.ashbyhq.com":
            raise ValueError(GUIDANCE)
        parts = url.path.strip("/").split("/")
        board = unquote(parts[0])
        role = parts[1] if len(parts) > 1 and ROLE.fullmatch(parts[1]) else None
    else:
        board, role = value.strip(), None
    if not BOARD.fullmatch(board) or board in (".", ".."):
        raise ValueError(GUIDANCE)
    return board, role


class Description(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag == "li":
            self.parts.append("\n• ")
        elif tag in ("p", "div", "br", "ul", "ol") or re.fullmatch("h[1-6]", tag):
            if self.parts[-1:] != ["\n• "]:
                self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li", "ul", "ol") or re.fullmatch("h[1-6]", tag):
            self.parts.append("\n")

    def handle_data(self, data):
        self.parts.append(re.sub(r"\s+", " ", data))

    def render(self, html):
        self.feed(html)
        self.close()
        return "\n".join(line.strip() for line in "".join(self.parts).splitlines() if line.strip())


def fetch(board, ctx):
    transport = single_hop_transport if ctx.transport is urllib_transport else ctx.transport
    client = Client(max_requests=1, transport=transport)
    status, data = client.request("GET", API + quote(board, safe="") + "?includeCompensation=true")
    if status == 404:
        raise Failure("not_found", f"Ashby board {board!r} was not found. {GUIDANCE}")
    if status == 429:
        raise Failure("rate_limited", "Ashby returned HTTP 429")
    if status in (401, 403):
        raise Failure("blocked", f"Ashby returned HTTP {status} for board {board!r}. {GUIDANCE}")
    if status != 200:
        raise HttpError(f"Ashby returned HTTP {status}")
    try:
        data = json.loads(data)
    except (UnicodeError, ValueError):
        raise HttpError("Ashby returned invalid JSON") from None
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        raise HttpError("Ashby response lacks a jobs list")
    return data["jobs"]


class Failure(HttpError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def posting(job, board):
    if not isinstance(job, dict) or not isinstance(job.get("jobUrl"), str):
        raise HttpError("Ashby posting lacks a job URL")
    url = job["jobUrl"]
    try:
        actual, role = board_role(url)
        if not role or urlsplit(url).path.rstrip("/").count("/") != 2 or not url.startswith("https://"):
            raise ValueError()
        if actual.casefold() != board.casefold() or job.get("id", role) != role:
            raise Failure("url_mismatch", "Ashby returned a posting for a different board or role")
        stamp = job.get("publishedAt")
        if stamp is not None:
            stamp = datetime.fromisoformat(stamp)
            if stamp.tzinfo is None:
                raise ValueError()
            stamp = stamp.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        if not isinstance(job.get("title"), str) or not job["title"].strip():
            raise ValueError()
        if not isinstance(job.get("isListed"), bool):
            raise ValueError()
    except (TypeError, ValueError):
        raise HttpError("Ashby returned a malformed posting") from None
    return env.item(id=role, title=job["title"], url=url, published_at=stamp, source="ashby", raw=job)


def matches(item, args):
    job = item["raw"]
    if any(word not in item["title"].casefold() for word in (args.title or "").casefold().split()):
        return False
    teams = [job.get("team"), job.get("department")]
    locations = [job.get("location")] + [loc.get("location") for loc in job.get("secondaryLocations", [])]
    for needle, values in ((args.team, teams), (args.location, locations)):
        if needle and not any(needle.casefold() in (value or "").casefold() for value in values):
            return False
    return not args.since or bool(item["published_at"] and item["published_at"][:10] >= args.since.isoformat())


def run(args, ctx):
    read = args.action == "read"
    try:
        if read and args.board:
            board, _ = board_role(args.board)
            role = args.role
        else:
            board, role = board_role(args.role if read else args.board)
        if read and not ROLE.fullmatch(role or ""):
            raise ValueError("Pass a job URL, or a role ID with --board BOARD.")
        if not read and args.limit is not None and args.limit < 1:
            raise ValueError("--limit must be positive")
    except ValueError as exc:
        return env.usage("ashby", args.action, str(exc))
    inputs = {"board": board}
    if read:
        inputs["role"] = role
    else:
        inputs.update(title=args.title, team=args.team, location=args.location,
                      since=args.since.isoformat() if args.since else None, limit=args.limit)
    started = time.monotonic()
    meta = {}
    items = []
    error = None
    try:
        for job in fetch(board, ctx):
            item = posting(job, board)
            if read and item["id"] == role:
                text = Description().render(job.get("descriptionHtml") or "") or job.get("descriptionPlain")
                if not isinstance(text, str) or not text.strip():
                    raise HttpError("Ashby posting lacks a readable description")
                item["text"] = text
                items.append(item)
            elif not read and job["isListed"] and matches(item, args):
                item["raw"] = {k: v for k, v in job.items() if k not in ("descriptionHtml", "descriptionPlain")}
                items.append(item)
        if read and not items:
            raise Failure("not_found", f"Ashby board {board!r} has no published role {role!r}")
        if not read and args.limit and len(items) > args.limit:
            items = items[:args.limit]
            meta["truncated"] = True
        if not items:
            meta["notes"] = [f"No listed roles match on Ashby board {board!r}."]
    except HttpError as exc:
        items = []
        error = env.error(exc.code, str(exc))
    outcome = error["code"] if error else ("ok" if items else "no_results")
    meta["attempts"] = [env.attempt(BACKEND, outcome, 1, env.elapsed_ms(started))]
    return env.envelope("ashby", args.action, inputs, BACKEND, items, meta=meta, error=error)
