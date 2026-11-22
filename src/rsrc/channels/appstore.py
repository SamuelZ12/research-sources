"""App Store software listings and storefront-specific customer reviews."""

import json
import re
import time
from datetime import datetime, timezone
from html import unescape
from urllib.parse import parse_qs, urlencode, urlsplit

from rsrc import envelope as env
from rsrc.http import AuthError, CapReached, Client, HttpError, NotFound, RateLimited

BACKEND = "itunes"
ROOT = "https://itunes.apple.com"
NATIVE = ROOT + "/WebObjects/MZStore.woa/wa/"
APP_PATH = re.compile(r"/([a-z]{2})/app/(?:[^/]+/)?id([0-9]+)/?")
SORT = {"recent": 4, "helpful": 1}
HEADERS = {"X-Apple-Store-Front": ",32"}  # JSON storefront format; cc selects the country


class Mismatch(HttpError):
    code = "url_mismatch"


class Ambiguous(HttpError):
    code = "usage"


class Blocked(HttpError):
    code = "blocked"


def register(subparsers):
    actions = subparsers.add_parser("appstore", help="Apple App Store apps and customer reviews").add_subparsers(
        dest="action", required=True)
    for action, help_, default in [("search", "find apps by name", 10), ("read", "read reviews by ID, URL or name", 30)]:
        p = actions.add_parser(action, help=help_)
        p.add_argument("query", metavar="NAME" if action == "search" else "ID_OR_URL_OR_NAME")
        p.add_argument("--country", help="two-letter storefront (default: URL country or us)")
        p.add_argument("--limit", type=int, default=default)
        p.set_defaults(run=run)
        if action == "read":
            p.add_argument("--sort", choices=SORT, default="recent")
            p.add_argument("--max-requests", type=int, default=8, help="total requests including app lookup (1-20)")


def get(client, url, headers=None):
    status, body = client.request("GET", url, headers=headers)
    if status == 429:
        raise RateLimited("Apple rate limit (HTTP 429)")
    if status == 403:
        raise Blocked("Apple refused access (HTTP 403)")
    if status == 401:
        raise AuthError("Apple requires authentication (HTTP 401)")
    if status != 200:
        raise HttpError(f"Apple answered HTTP {status}")
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise HttpError("Apple returned invalid JSON (HTTP 200)") from None


def string(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value


def number(value):
    if type(value) not in (int, str) or not re.fullmatch(r"[0-9]+", str(value)):
        raise ValueError
    return int(value)


def utc(value):
    parsed = datetime.fromisoformat(string(value))
    if parsed.tzinfo is None:
        raise ValueError
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def catalog(client, country, *, term=None, app_id=None, limit=25):
    params = {"entity": "software", "country": country}
    if app_id:
        path = "lookup"
        params["id"] = app_id
    else:
        path = "search"
        params.update(term=term, limit=limit)
    data = get(client, f"{ROOT}/{path}?{urlencode(params)}")
    rows = data["results"]
    if not isinstance(rows, list) or type(data["resultCount"]) is not int or data["resultCount"] != len(rows):
        raise ValueError
    apps = []
    for row in rows:
        ident = str(number(row["trackId"]))
        url = urlsplit(row["trackViewUrl"])
        match = APP_PATH.fullmatch(url.path)
        if (url.scheme != "https" or url.netloc != "apps.apple.com" or not match
                or match.groups() != (country, ident) or app_id and ident != app_id):
            raise Mismatch("App Store listing has a different ID or storefront")
        if row["kind"] != "software":
            raise ValueError
        raw = {k: row.get(k) for k in ("primaryGenreName", "averageUserRating", "userRatingCount", "version",
                                       "currentVersionReleaseDate")}
        apps.append(env.item(id=ident, url=row["trackViewUrl"], title=string(row["trackName"]),
                             author=string(row["artistName"]), published_at=utc(raw["currentVersionReleaseDate"]),
                             source="appstore", raw={**raw, "country": country, "kind": "app"}))
    return apps


def review(app, ident, title, author, date, body, stars, votes, total_votes, version):
    stars = number(stars)
    if not 1 <= stars <= 5:
        raise ValueError
    return env.item(id=str(number(ident)), title=string(title), author=string(author), published_at=utc(date),
                    text=string(body), url=app["url"].split("?")[0] + "?see-all=reviews", source="appstore",
                    engagement={"helpful_votes": number(votes), "total_votes": number(total_votes)}, raw={"kind": "review", "app_id": app["id"],
                    "country": app["raw"]["country"], "rating": stars, "version": version})


def rss(client, app, page, sort):
    country, ident = app["raw"]["country"], app["id"]
    url = f"{ROOT}/{country}/rss/customerreviews/page={page}/id={ident}/sortby=most{sort}/json"
    feed = get(client, url, headers=HEADERS)["feed"]
    if feed["id"]["label"] != url:
        raise Mismatch("Apple RSS feed identifies a different app, country, page or sort")
    if feed["title"]["label"] != "iTunes Store: Customer Reviews":
        raise ValueError
    links = {link["attributes"]["rel"]: link["attributes"]["href"] for link in feed["link"]}
    if links.get("self") != url:
        raise Mismatch("Review feed self link differs from the requested URL")
    if "entry" not in feed and (links["first"] or links["last"] or links["next"]):
        raise ValueError
    rows = feed.get("entry", [])
    if not isinstance(rows, list):
        raise ValueError
    items = []
    for r in rows:
        link = urlsplit(r["link"]["attributes"]["href"])
        if (link.netloc != "itunes.apple.com" or link.path != f"/{country}/review"
                or parse_qs(link.query).get("id") != [ident]):
            raise Mismatch("Review links to a different app or storefront")
        items.append(review(app, r["id"]["label"], r["title"]["label"], r["author"]["name"]["label"],
                            r["updated"]["label"], r["content"]["label"], r["im:rating"]["label"],
                            r["im:voteSum"]["label"], r["im:voteCount"]["label"], string(r["im:version"]["label"])))
    return items


def native_count(client, app, params):
    data = get(client, NATIVE + "customerReviews?" + urlencode({**params, "dataOnly": "true"}), HEADERS)
    # This returned URL identifies both the app and the resolved storefront. Never request it.
    scope = parse_qs(urlsplit(data["writeUserReviewUrl"]).query)
    if (str(data["adamId"]) != app["id"] or scope.get("cc") != [params["cc"]]
            or scope.get("id") != [app["id"]]):
        raise Mismatch("Apple review count identifies a different app or storefront")
    if data["kindId"] != 11 or data["userReviewsRowUrl"] != NATIVE + "userReviewsRow":
        raise ValueError
    return number(data["totalNumberOfReviews"])


def read_reviews(client, app, args, items, meta):
    country = app["raw"]["country"]
    params = {"cc": country, "id": app["id"], "displayable-kind": 11, "sort": SORT[args.sort]}
    seen = set()
    page, offset, total = 1, 0, None
    meta["notes"].append("Apple's public review window can lag and completeness is unverified.")
    while len(seen) < args.limit:
        if total is None and page <= 10:
            batch = rss(client, app, page, args.sort)
            page += 1
            if batch:
                offset += len(batch)
        else:
            batch = []
        if not batch:
            if total is None:
                total = native_count(client, app, params)
                app["raw"]["storefront_review_count"] = total
                if offset > total:
                    raise HttpError("Apple's review count contradicts its RSS feed")
                meta["notes"].append(f"Apple reports {total} written reviews in {country.upper()}.")
                if offset == total:
                    if len(seen) < offset:
                        meta["notes"].append("Overlapping pages reduced the distinct review count.")
                    break
                meta["notes"].append("Storefront rows omit app version. Pages from the two feeds may overlap or shift.")
            bounds = {"startIndex": offset, "endIndex": min(total, offset + 50)}
            data = get(client, NATIVE + "userReviewsRow?" + urlencode({**params, **bounds}), HEADERS)
            rows = data["userReviewList"]
            if not isinstance(rows, list) or not rows or len(rows) > 50:
                raise HttpError("Apple's review list contradicts its reported count")
            for r in rows:
                if urlsplit(r["viewUsersUserReviewsUrl"]).path != f"/{country}/reviews":
                    raise Mismatch("Review row identifies a different storefront")
                batch.append(review(app, r["userReviewId"], unescape(string(r["title"])),
                                    unescape(string(r["name"])), r["date"], unescape(string(r["body"])),
                                    r["rating"], r["voteSum"], r["voteCount"], None))
            offset += len(rows)
        new = []
        for row in batch:
            if row["id"] not in seen:
                new.append(row)
                seen.add(row["id"])
        if not new:
            raise HttpError("Apple repeated a review page")
        items.extend(new[:args.limit - (len(items) - 1)])
        if total is not None and offset >= total:
            break
    if not seen:
        items.clear()
        meta["notes"].append(f'{app["title"]} ({app["id"]}) has no written reviews in {country.upper()}.')


def run(args, ctx):
    q = args.query.strip()
    read = args.action == "read"
    country = args.country.lower() if args.country is not None else "us"
    ident = q if read and re.fullmatch(r"[0-9]+", q) else None
    if read and ("://" in q or q.startswith("apps.apple.com/")):
        try:
            url = urlsplit(q if "://" in q else "https://" + q)
            match = APP_PATH.fullmatch(url.path)
        except ValueError:
            match = None
        if not match or (url.scheme, url.netloc) != ("https", "apps.apple.com"):
            return env.usage("appstore", "read", "Expected an apps.apple.com app URL")
        ident = match[2]
        if args.country is None:
            country = match[1]
    cap = getattr(args, "max_requests", 1)
    if (not q or not re.fullmatch("[a-z]{2}", country) or args.limit < 1 or not 1 <= cap <= 20
            or not read and args.limit > 200):
        return env.usage("appstore", args.action, "Expected a query, two-letter country, positive limit "
                         "(search maximum 200) and a request cap between 1 and 20")
    inputs = {"query": q, "country": country, "limit": args.limit}
    if read:
        inputs.update(sort=args.sort, max_requests=cap)
    meta, items, error = {"notes": []}, [], None
    client = Client(max_requests=cap, transport=ctx.transport, sleep=ctx.sleep)
    started = time.monotonic()
    try:
        apps = catalog(client, country, term=q, app_id=ident, limit=50 if read else args.limit)
        if ident and not apps:
            reference = "gb" if country == "us" else "us"
            other = catalog(client, reference, app_id=ident)
            if other:
                raise NotFound(f'{other[0]["title"]} ({ident}) is sold in {reference.upper()} but unavailable in {country.upper()}')
            raise NotFound(f"App {ident} not found in {country.upper()} or {reference.upper()}; worldwide availability unknown")
        if not read:
            items.extend(apps[:args.limit])
        elif apps:
            if not ident:
                normalized = re.sub(r"[\W_]", "", q.casefold())
                close = [a for a in apps if normalized and normalized in re.sub(r"[\W_]", "", a["title"].casefold())]
                if len(close) != 1 or (len(apps) > 1 and close[0]["title"].casefold() != q.casefold()):
                    items.extend(apps)
                    raise Ambiguous("No unique name match; rerun read with one of the returned app IDs")
                apps = close
            if len(apps) != 1:
                raise Mismatch("Lookup returned multiple apps for one ID")
            items.append(apps[0])
            read_reviews(client, apps[0], args, items, meta)
    except CapReached:
        meta["truncated"] = "request_cap"
        meta["notes"].append(f"Request cap reached after {cap} calls; returned {max(0, len(items) - 1)} of "
                             f"{args.limit} requested reviews.")
        if len(items) <= 1:
            error = env.error("usage", "Request cap reached before the answer could be verified; increase --max-requests")
    except HttpError as e:
        error = env.error(e.code, str(e))
    except (ValueError, KeyError, TypeError, AttributeError, OverflowError):
        error = env.error("upstream_error", "Apple returned an unexpected listing or review format")
    if read and args.sort == "recent" and len(items) > 1 and items[1]["raw"]["kind"] == "review":
        items[1:] = sorted(items[1:], key=lambda r: r["published_at"], reverse=True)
    outcome = error["code"] if error else "ok" if items else "no_results"
    meta["attempts"] = [env.attempt(BACKEND, outcome, client.requests_made, env.elapsed_ms(started))]
    return env.envelope("appstore", args.action, inputs, BACKEND, items, meta=meta, error=error)
