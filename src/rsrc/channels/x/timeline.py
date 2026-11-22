"""Read a user's own posts and reposts using the configured X web session."""
import json
from pathlib import Path
from urllib.parse import urlencode

from rsrc.channels.x import search
from rsrc.http import AuthError, HttpError, RateLimited


class TimelineBlocked(HttpError):
    code = "blocked"


def timeline(client, cookies, handle, limit):
    ops = json.loads(Path(__file__).with_name("timeline_ops.json").read_text())
    headers = search.headers(cookies)

    def read(name, variables):
        op = ops[name]
        query = urlencode({k: json.dumps(v) for k, v in (
            ("variables", variables), ("features", op["features"]), ("fieldToggles", op["toggles"]))})
        status, raw = client.request("GET", f"{search.API}/{op['id']}/{name}?{query}", headers)
        search.check_status(status, b"")
        try:
            doc = json.loads(raw)
            if doc.get("errors"):
                codes = {str(e.get("code")) for e in doc["errors"]}
                if "88" in codes:
                    raise RateLimited("X rate limited this account")
                if codes & {"32", "89", "215"}:
                    raise AuthError("X rejected the configured session")
                raise TimelineBlocked(f"X rejected {name}; stop here")
            result = doc["data"]["user"]["result"]
            if not isinstance(result, dict):
                raise HttpError(f"X returned an unrecognized {name} response")
            return result
        except (ValueError, KeyError, TypeError, AttributeError):
            raise HttpError(f"X returned an unrecognized {name} response") from None

    user = read("UserByScreenName", {"screen_name": handle, "withSafetyModeUserFields": True})
    core, legacy = user.get("core") or {}, user.get("legacy") or {}
    name = core.get("screen_name") or legacy.get("screen_name")
    if not user.get("rest_id") or (name or "").lower() != handle.lower():
        raise HttpError("X returned a different or unavailable account")
    account = dict(screen_name=name, name=core.get("name") or legacy.get("name"),
                   description=search.expand_links(legacy.get("description"),
                       (legacy.get("entities") or {}).get("description", {}).get("urls") or []),
                   followers=legacy.get("followers_count"),
                   following=legacy.get("friends_count"), statuses=legacy.get("statuses_count"),
                   joined=core.get("created_at") or legacy.get("created_at"))
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
