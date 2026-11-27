import json
from pathlib import Path
from urllib.parse import urlencode

from rsrc.channels.x import search
from rsrc.http import AuthError, HttpError, RateLimited


class TimelineBlocked(HttpError):
    code = "blocked"


def profile_record(user):
    core, legacy = user.get("core") or {}, user.get("legacy") or {}
    name = core.get("screen_name") or legacy.get("screen_name")
    entities = legacy.get("entities") or {}
    websites = (entities.get("url") or {}).get("urls") or []
    website = next((u for u in websites if u.get("url") == legacy.get("url")), {})
    return dict(id=user["rest_id"], screen_name=name, url=f"https://x.com/{name}", type="profile",
                name=core.get("name") or legacy.get("name"),
                description=search.expand_links(legacy.get("description"),
                    (entities.get("description") or {}).get("urls") or []),
                location=(user.get("location") or {}).get("location") or legacy.get("location"),
                followers=legacy.get("followers_count"), following=legacy.get("friends_count"),
                statuses=legacy.get("statuses_count"), likes=legacy.get("favourites_count"),
                media_count=legacy.get("media_count"), joined=core.get("created_at") or legacy.get("created_at"),
                protected=(user.get("privacy") or {}).get("protected", legacy.get("protected")),
                avatar_url=(user.get("avatar") or {}).get("image_url") or legacy.get("profile_image_url_https"),
                banner_url=legacy.get("profile_banner_url"),
                website={"url": website["expanded_url"], "display_url": website.get("display_url")}
                        if website.get("expanded_url") else None)


def timeline(client, cookies, handle, limit):
    ops = json.loads(Path(__file__).with_name("timeline_ops.json").read_text())
    headers = search.headers(cookies)

    def read(name, variables):
        op = ops[name]
        query = urlencode({k: json.dumps(v) for k, v in (
            ("variables", variables), ("features", op["features"]), ("fieldToggles", op["toggles"]))})
        status, raw = client.request("GET", f"{search.API}/{op['id']}/{name}?{query}", headers)
        if 300 <= status < 400:
            raise TimelineBlocked(f"X redirected {name} (HTTP {status}); stop here")
        if raw.lstrip().startswith(b"<") and status not in (401, 403, 429):
            raise TimelineBlocked(f"X returned a page instead of {name} data (HTTP {status}); stop here")
        search.check_status(status, b"")
        try:
            doc = json.loads(raw)
            if doc.get("errors"):
                search.check_graphql_codes(doc["errors"])
                raise TimelineBlocked(f"X rejected {name}; stop here")
            result = doc["data"]["user"]["result"]
            if not isinstance(result, dict):
                raise HttpError(f"X returned an unrecognized {name} response")
            return result
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HttpError(f"X returned an unrecognized {name} response") from None

    user = read("UserByScreenName", {"screen_name": handle, "withSafetyModeUserFields": True})
    if not user.get("rest_id"):
        raise HttpError("X returned an unavailable account")
    account = profile_record(user)
    name = account["screen_name"]
    if (name or "").lower() != handle.lower():
        raise HttpError("X returned a different or unavailable account")
    posts = {}
    cursor = None
    while len(posts) < limit:
        if posts and client.requests_made >= client.max_requests:
            return account, list(posts.values()), True
        variables = dict(userId=user["rest_id"], count=min(100, limit - len(posts)), withVoice=True,
                         includePromotedContent=False, withQuickPromoteEligibilityTweetFields=True)
        if cursor:
            variables["cursor"] = cursor
        result = read("UserTweets", variables)
        instructions = ((result.get("timeline_v2") or result.get("timeline") or {})
                        .get("timeline", {}).get("instructions", []))
        batches = [i.get("entries") for i in instructions if i.get("type") == "TimelineAddEntries"]
        if not batches or any(not isinstance(batch, list) for batch in batches):
            raise HttpError("X returned an unrecognized UserTweets timeline")
        next_cursor = None
        for entries in batches:
            for entry in entries:
                content = entry.get("content") or {}
                if content.get("cursorType"):
                    if content["cursorType"] == "Bottom":
                        next_cursor = content.get("value")
                    continue
                if "items" in content:
                    items = [i.get("item", {}).get("itemContent") or {} for i in content["items"]]
                else:
                    items = [content.get("itemContent") or {}]
                for item in items:
                    if item.get("itemType") == "TimelineUser":
                        continue
                    if item.get("promotedMetadata"):
                        continue
                    tweet = search._unwrap((item.get("tweet_results") or {}).get("result"))
                    wrapper = search.to_record(tweet)
                    if not wrapper or wrapper["author"].lower() != handle.lower():
                        raise HttpError("X returned an unreadable or unrelated timeline entry")
                    repost = (tweet.get("legacy", {}).get("retweeted_status_result") or {}).get("result")
                    record = search.to_record(repost) if repost else wrapper
                    if not record:
                        raise HttpError("X returned an unreadable repost")
                    if repost:
                        record["reposted_by"] = name
                    posts[record["id"]] = record
                    if len(posts) == limit:
                        return account, list(posts.values()), False
        if not next_cursor:
            if not posts and account["statuses"] != 0:
                raise HttpError("X returned no readable timeline posts for this account")
            break
        if next_cursor == cursor:
            raise HttpError("X repeated its timeline cursor")
        cursor = next_cursor
    return account, list(posts.values()), False
