"""Read the structured data on one public LinkedIn post page."""
import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from rsrc import envelope as env, http

BACKEND = "linkedin-public"
WAIT = "Wait before making another LinkedIn request."


class Failure(http.HttpError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def target(value):
    try:
        u = urlsplit(value)
        path = unquote(u.path).rstrip("/")
        host_ok = re.fullmatch(r"(?:(?:www|[a-z]{2})\.)?linkedin\.com", u.netloc, re.I)
        m = re.fullmatch(r"/posts/[\w-]*?(activity|ugcPost|share)-(\d{19})(?:-[\w-]+)?", path, re.ASCII)
        m = m or re.fullmatch(r"/feed/update/urn:li:(activity|ugcPost|share):(\d{19})", path)
        if u.scheme == "https" and host_ok and m:
            return m[2], "https://www.linkedin.com" + path + "/", m[1]
    except ValueError:
        pass
    raise Failure("usage", "This command reads posts. Supply a LinkedIn post or feed update URL.")


def register(subparsers):
    p = subparsers.add_parser("linkedin", help="read a LinkedIn post")
    actions = p.add_subparsers(dest="action", required=True)
    read = actions.add_parser("read")
    read.add_argument("url")
    read.set_defaults(run=run)


class PostPage(HTMLParser):
    def __init__(self):
        super().__init__()
        self.posts = []
        self.cards = []
        self.page_key = self.title = self.canonical = ""
        self.capture = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.capture = "json"
        elif tag == "title":
            self.capture = "title"
        elif tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = string(attrs.get("href"))
        elif tag == "meta" and attrs.get("name") == "pageKey":
            self.page_key = attrs.get("content") or ""
        elif tag == "article" and "main-feed-activity-card" in string(attrs.get("class")).split():
            self.cards.append(attrs)

    def handle_endtag(self, tag):
        if tag in ("script", "title"):
            self.capture = None

    def handle_data(self, data):
        if self.capture == "title":
            self.title += data
        if self.capture == "json":
            try:
                post = json.loads(data)
            except ValueError:
                return
            if isinstance(post, dict) and post.get("@type") in ("SocialMediaPosting", "VideoObject"):
                self.posts.append(post)


def string(value):
    return value.strip() if isinstance(value, str) else ""


def timestamp(record):
    value = string(record.get("datePublished"))
    try:
        if datetime.fromisoformat(value).utcoffset() is not None:
            return value
    except ValueError:
        pass
    return None


def author(record):
    person = record.get("author") or record.get("creator")
    return string(person.get("name")) if isinstance(person, dict) else None


def counts(record):
    fields = {"LikeAction": "reactions", "CommentAction": "comments", "ShareAction": "reposts"}
    stats = record.get("interactionStatistic")
    result = {}
    for stat in stats if isinstance(stats, list) else [stats]:
        if isinstance(stat, dict):
            field = fields.get(string(stat.get("interactionType")).rsplit("/", 1)[-1])
            count = stat.get("userInteractionCount")
            if field and type(count) is int and count >= 0:
                result[field] = count
    total = record.get("commentCount")
    if type(total) is int and total >= 0:
        result["comments"] = total
    return result


def parse(status, headers, body, identity):
    if status == 999:
        raise http.Blocked(f"LinkedIn refused access (HTTP 999). {WAIT}")
    http.raise_refusal(status, b"", "LinkedIn", WAIT)
    location = headers.get("Location", "").lower()
    if 300 <= status < 400:
        if "/signup/cold-join" in location:
            # Measured: LinkedIn sends a logged-out reader here for an ID with no public post.
            raise http.NotFound(f"LinkedIn has no public post with this ID: it redirected to sign-up (HTTP {status}). "
                                "The post is missing or not public.")
        if any(part in location for part in ("/login", "/authwall", "/uas/")):
            raise http.AuthError(f"LinkedIn redirected to sign-in (HTTP {status}). {WAIT}")
        raise http.Blocked(f"LinkedIn redirected the post request (HTTP {status}). {WAIT}")
    page = PostPage()
    page.feed(body.decode("utf-8", "replace"))
    page.close()
    wall_title = page.title if page.page_key != "d_public_post" else ""
    marker = (page.page_key + " " + wall_title).lower()
    if any(word in page.page_key.lower() for word in ("authwall", "login", "signup")) or re.match(
            r"(linkedin (?:login|sign in)|sign in|log in)", wall_title.strip(), re.I):
        raise http.AuthError(f"LinkedIn returned a sign-in wall. {WAIT}")
    if any(word in marker for word in ("checkpoint", "challenge", "security verification", "captcha")):
        raise http.Blocked(f"LinkedIn returned a challenge. {WAIT}")
    if status in (404, 410):
        raise http.NotFound(f"LinkedIn has no public post at this address (HTTP {status}).")
    if status != 200 or page.page_key != "d_public_post" or len(page.posts) != 1:
        if body.lstrip().startswith(b"<") and not page.posts:
            raise http.Blocked(f"LinkedIn returned a page without a readable public post (HTTP {status}). {WAIT}")
        raise http.HttpError(f"Expected one public LinkedIn post (HTTP {status}).")
    return post_items(page, identity)


def post_items(page, identity):
    post = page.posts[0]
    try:
        canonical_id, url, _ = target(page.canonical)
        record_id = target(string(post.get("@id")))[0]
    except Failure:
        raise http.HttpError("Post has no identifiable permalink.") from None
    if record_id != canonical_id:
        raise Failure("url_mismatch", "The post data identifies a different post from the page permalink.")
    featured = f"urn:li:activity:{canonical_id}"
    cards = [card for card in page.cards if card.get("data-featured-activity-urn") == featured
             and card.get("data-activity-urn") == featured]
    if len(cards) != 1:
        raise http.HttpError("The page lacks a matching featured post card.")
    if canonical_id != identity and cards[0].get("data-attributed-urn") not in (
            f"urn:li:ugcPost:{identity}", f"urn:li:share:{identity}"):
        raise Failure("url_mismatch", "The public page identifies a different post.")
    text = string(post.get("articleBody")) or string(post.get("description"))
    if not text or not author(post):
        raise http.HttpError("The public page lacks the post body or author.")
    raw = {"kind": "post", "type": post["@type"]}
    for key in ("duration", "transcript"):
        if string(post.get(key)):
            raw[key] = post[key]
    engagement = counts(post)
    items = [env.item(id=identity, url=url, author=author(post), text=text, title=string(post.get("headline")) or None,
                      published_at=timestamp(post), engagement=engagement, raw=raw, source="linkedin")]
    comments = post.get("comment")
    for row in comments if isinstance(comments, list) else [comments]:
        if isinstance(row, dict) and string(row.get("text")):
            items.append(env.item(text=string(row["text"]), author=author(row), published_at=timestamp(row),
                                  engagement=counts(row), source="linkedin", raw={"kind": "comment", "post_id": identity}))
    notes = []
    total = engagement.get("comments")
    if total is not None and len(items) - 1 < total:
        notes.append(f"The public page supplied {len(items) - 1} of {total} comments.")
    if not items[0]["published_at"]:
        notes.append("The page supplied no exact publication time.")
    return items, notes


def run(args, ctx):
    transport = http.single_hop_transport if ctx.transport is http.urllib_transport else ctx.transport
    client = http.Client(min_interval=3, max_requests=1, transport=transport)
    start = time.monotonic()
    items, notes, attempts, error = [], [], [], None
    try:
        identity, _, kind = target(args.url)
        # Read by ID: a /posts/ address answers 404 when its slug is inexact, while the ID still resolves.
        url = f"https://www.linkedin.com/feed/update/urn:li:{kind}:{identity}/"
        status, headers, body = client.request_with_headers("GET", url, {"User-Agent": "research-sources"})
        items, notes = parse(status, headers, body, identity)
    except http.HttpError as exc:
        error = env.error(exc.code, str(exc))
    if client.requests_made:
        attempts.append(env.attempt(BACKEND, error["code"] if error else "ok", 1, env.elapsed_ms(start)))
    return env.envelope("linkedin", "read", None, BACKEND if attempts else None, items, error=error,
                        meta={"attempts": attempts, "notes": notes})
