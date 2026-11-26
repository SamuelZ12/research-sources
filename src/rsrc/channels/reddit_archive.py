"""Read a thread snapshot from Arctic Shift's public API."""

import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

from rsrc import envelope as env
from rsrc import http

BACKEND = "arctic-shift"
API = "https://arctic-shift.photon-reddit.com/api"
REDDIT = "https://www.reddit.com"
TREE_LIMIT = 200
ID = re.compile(r"[a-z0-9]+")
SUBREDDIT = re.compile(r"[A-Za-z0-9_]+")
CHANNEL = "reddit"


class _WrongPost(http.HttpError):
    code = "url_mismatch"


def _rows(client, path, **params):
    # Do not follow redirects or try another backend on an archive refusal.
    url = f"{API}/{path}?{urlencode(params)}"
    status, headers, body = client.request_with_headers("GET", url, {"user-agent": "research-sources"})
    if status == 429:
        seconds = headers.get("X-RateLimit-Reset") or headers.get("Retry-After")
        wait = f" (reset in {seconds} s)" if isinstance(seconds, str) and seconds.isdecimal() else ""
        raise http.RateLimited(f"Arctic Shift rate limit (HTTP 429){wait}. Stopped without retrying.")
    http.raise_refusal(status, body, "Arctic Shift")
    if status != 200:
        raise http.HttpError(f"Arctic Shift returned HTTP {status} for {path}")
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):  # a long reply chain can exceed the decoder's depth
        raise http.HttpError(f"Arctic Shift returned invalid JSON for {path}") from None
    if not isinstance(data, dict) or data.get("error") or not isinstance(data.get("data"), list):
        raise http.HttpError(f"Arctic Shift returned an unexpected response for {path}")
    return data["data"]


def _int(value):
    return value if type(value) is int else None


def _string(value):
    return value.strip() if isinstance(value, str) and value.strip() else None


def _body(value):
    """Markdown as archived: indentation on the first line is content."""
    return value.strip("\n").rstrip() if isinstance(value, str) and value.strip() else None


def _iso(epoch):
    if _int(epoch) is None:
        return None
    try:
        return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace("+00:00", "Z")
    except (OverflowError, OSError, ValueError):
        return None


def _age(seconds):
    if seconds >= 86400:
        days = seconds // 86400
        return f"{days} day{'s' if days != 1 else ''} old"
    if seconds >= 3600:
        return f"{seconds // 3600} hr {(seconds % 3600) // 60} min old"
    return f"{seconds // 60} min {seconds % 60} s old"


def _metadata(row):
    value = row.get("_meta")
    return value if isinstance(value, dict) else {}


def _snapshot_note(post):
    meta = _metadata(post)
    now = int(time.time())
    created = _int(post.get("created_utc")) or 1
    candidates = (meta.get("retrieved_2nd_on"), post.get("retrieved_on"))
    for value in sorted((n for n in candidates if _int(n) is not None), reverse=True):
        stamp = _iso(value)
        if stamp and max(1, created) <= value <= now:
            return (f"Post last captured at {stamp} ({_age(now - value)}). "
                    "Scores and comment counts are snapshots. Comments may have different capture times.")
    return "Post capture time and age are unavailable. Scores and comment counts are snapshots."


def _placeholder(row, text):
    marker = text.strip().lower() if text else None
    meta = _metadata(row)
    later = meta.get("was_deleted_later") is True
    reason = _string(row.get("removed_by_category")) or _string(row.get("removal_reason"))
    if later or meta.get("was_initially_deleted") is not True:
        reason = reason or _string(meta.get("removal_type"))
    if reason and reason.lower() in ("deleted", "author"):
        return "[deleted]"
    if marker in ("[deleted]", "[removed]"):
        return marker
    if marker == "[ removed by reddit ]":
        return "[removed]"
    if reason or later:
        return "[removed]"
    return None


def _item(row, post_id, thread_path, unavailable, *, depth=None):
    is_post = depth is None
    kind = "post" if is_post else "comment"
    id_ = ("t3_" if is_post else "t1_") + row["id"]
    path = thread_path if is_post else thread_path + "_/" + row["id"] + "/"
    text = _body(row.get("selftext" if is_post else "body"))
    placeholder = _placeholder(row, text)
    title = _string(row.get("title")) if is_post and not placeholder else None
    if not (title or text or placeholder):
        raise http.HttpError(f"Arctic Shift returned {kind} {id_} with no readable content")
    author = _string(row.get("author"))
    if placeholder or author == "[deleted]":
        author = None
    published_at = _iso(row.get("created_utc"))
    score = _int(row.get("score"))
    attrs = {"author": author, "score": str(score) if score is not None else None, "permalink": path}
    engagement = {"score": score}
    if is_post:
        count = _int(row.get("num_comments"))
        if count is not None and count < 0:
            count = None
        engagement["comments"] = count
        attrs.update({
            "id": id_, "post-title": title, "created-timestamp": published_at,
            "comment-count": str(count) if count is not None else None,
            "subreddit-prefixed-name": "r/" + row["subreddit"] if row.get("subreddit") else None,
            "post-type": _string(row.get("post_hint")) or ("text" if row.get("is_self") is True else None),
            "domain": _string(row.get("domain")) if not placeholder else None,
            "content-href": _string(row.get("url")) if not placeholder else None,
            "upvote-ratio": str(row["upvote_ratio"]) if type(row.get("upvote_ratio")) in (int, float) else None,
            "item-state": None, "post-language": None,
        })
    else:
        attrs.update({
            "thingid": id_, "parentid": row["parent_id"], "postid": post_id,
            "created": published_at, "depth": str(depth) if depth >= 0 else None,
            "collapsed": None,
            "is-mod-distinguished": "" if row.get("distinguished") == "moderator" else None,
        })

    missing = {k for k, v in {"author": author, "published_at": published_at}.items() if v is None}
    missing.update(f"engagement.{k}" for k, v in engagement.items() if v is None)
    missing.update(f"raw.attrs.{k}" for k, v in attrs.items() if v is None)
    if is_post and title is None:
        missing.add("title")
    if is_post and text is None and placeholder is None:
        missing.add("text")
    unavailable[id_] = kind, missing
    raw = {"kind": kind, "attrs": attrs, "text": placeholder or text}
    if not is_post and row.get("stickied") is True:
        raw["pinned"] = True
    if is_post and placeholder:
        raw["removal_notice"] = f"Arctic Shift marked this post as {placeholder}."
    return env.item(id=id_, url=REDDIT + path, author=author, published_at=published_at,
                    title=title, text=raw["text"], source=CHANNEL, engagement=engagement, raw=raw), placeholder


def _comments(rows, post_id, path, unavailable):
    # Flatten the candidate set without recursion, keeping the archive's parent IDs and depths.
    pending = [(row, post_id, 0) for row in reversed(rows)]
    seen = set()
    comments = []
    collapsed = False
    orphans = 0
    while pending:
        node, parent, depth = pending.pop()
        row = node.get("data") if isinstance(node, dict) else None
        if not isinstance(row, dict):
            raise http.HttpError("Arctic Shift returned a malformed tree node")
        if node.get("kind") == "more" and isinstance(row.get("children"), list):
            collapsed = True  # with or without listed IDs, the marker means replies were left out
            continue
        id_ = row.get("id")
        # The archive lists a reply at the top of the tree when the parent comment is not in it.
        orphan = (depth == 0 and row.get("parent_id") != parent and isinstance(row.get("parent_id"), str)
                  and re.fullmatch(r"t1_[a-z0-9]+", row["parent_id"]) is not None)
        if (node.get("kind") != "t1" or not isinstance(id_, str) or not ID.fullmatch(id_)
                or row.get("link_id") != post_id or id_ in seen
                or (row.get("parent_id") != parent and not orphan)):
            raise http.HttpError("Arctic Shift returned an invalid, duplicate or unrelated comment")
        if orphan:
            orphans += 1
            depth = -1  # unknown: the parent is outside the fetched tree
        seen.add(id_)
        comments.append(_item(row, post_id, path, unavailable, depth=depth))
        replies = row.get("replies")
        if replies in (None, ""):
            continue
        listing = replies.get("data") if isinstance(replies, dict) and replies.get("kind") == "Listing" else None
        if not isinstance(listing, dict) or not isinstance(listing.get("children"), list):
            raise http.HttpError("Arctic Shift returned malformed comment replies")
        pending.extend((child, "t1_" + id_, depth + 1 if depth >= 0 else -1)
                       for child in reversed(listing["children"]))
    return comments, collapsed, orphans


def _score_order(entry):
    item, _ = entry
    score = item["engagement"]["score"]
    return score is None, -(score or 0), item["published_at"] or "9999", item["id"]


def _validate_comment_post(row, post_id, comment_id):
    other = row.get("link_id")
    if not isinstance(other, str) or not re.fullmatch(r"t3_[a-z0-9]+", other):
        raise http.HttpError(f"Arctic Shift returned t1_{comment_id} without a valid post ID")
    if other != "t3_" + post_id:
        raise _WrongPost(f"Arctic Shift lists t1_{comment_id} under {other}, expected t3_{post_id}")


def _comment_by_id(client, post_id, comment_id):
    matches = _rows(client, "comments/ids", ids=comment_id)
    if not matches:
        raise http.NotFound(f"Arctic Shift has no comment t1_{comment_id}")
    row = matches[0] if len(matches) == 1 else None
    if not isinstance(row, dict) or row.get("id") != comment_id:
        raise http.HttpError(f"Arctic Shift did not return the requested comment t1_{comment_id}")
    _validate_comment_post(row, post_id, comment_id)
    return row


def _focused_tree(client, post_id, comment_id, with_replies):
    """(tree rows rooted at the comment, whether the archive had a reply tree for it)."""
    if not with_replies:
        return [{"kind": "t1", "data": _comment_by_id(client, post_id, comment_id)}], True

    comment = "t1_" + comment_id
    rows = _rows(client, "comments/tree", link_id=post_id, limit=TREE_LIMIT, parent_id=comment)
    if not rows:
        # The archive has no branch for this comment. The lookup by ID still returns the comment itself.
        return [{"kind": "t1", "data": _comment_by_id(client, post_id, comment_id)}], False
    node = rows[0] if len(rows) == 1 and isinstance(rows[0], dict) else {}
    root = node.get("data")
    if node.get("kind") != "t1" or not isinstance(root, dict) or root.get("id") != comment_id:
        raise http.HttpError(f"Arctic Shift did not return a tree rooted at {comment}")
    _validate_comment_post(root, post_id, comment_id)
    return rows, True


def read(ctx, input_, post_id):
    """Read the post and a bounded tree. Look up the comment if its focused tree is empty."""
    started = time.monotonic()
    transport = http.single_hop_transport if ctx.transport is http.urllib_transport else ctx.transport
    comment_id = input_.get("comment_id")
    client = http.Client(min_interval=0, max_requests=3 if comment_id else 2, transport=transport, sleep=ctx.sleep)
    items, notes = [], ["This is an Arctic Shift archive copy of Reddit."]
    unavailable = {}  # item ID -> (kind, null fields), reported only for items that are returned
    removed = deleted = 0
    failure = None
    try:
        rows = _rows(client, "posts/ids", ids=post_id)
        if not rows:
            raise http.NotFound(f"Arctic Shift has no post for t3_{post_id}")
        if len(rows) != 1 or not isinstance(rows[0], dict) or rows[0].get("id") != post_id:
            raise http.HttpError(f"Arctic Shift did not return the requested post t3_{post_id}")
        post = rows[0]
        subreddit = post.get("subreddit")
        if not isinstance(subreddit, str) or not SUBREDDIT.fullmatch(subreddit):
            subreddit = None
        path = f"/r/{subreddit}/comments/{post_id}/" if subreddit else f"/comments/{post_id}/"
        post = {**post, "subreddit": subreddit}
        notes.append(_snapshot_note(post))
        post_item, placeholder = _item(post, "t3_" + post_id, path, unavailable)
        items.append(post_item)
        removed, deleted = int(placeholder == "[removed]"), int(placeholder == "[deleted]")
        if input_["comments"] or comment_id:
            if comment_id:
                rows, has_tree = _focused_tree(client, post_id, comment_id, with_replies=input_["comments"] > 1)
            else:
                rows = _rows(client, "comments/tree", link_id=post_id, limit=TREE_LIMIT)
            comments, collapsed, orphans = _comments(rows, post_item["id"], path, unavailable)
            count = post_item["engagement"]["comments"]
            if not comments and (collapsed or (count is not None and count > 0)):
                raise http.HttpError("Arctic Shift returned no comment records despite evidence of comments")
            if comment_id:
                linked, *replies = comments
                linked[0]["raw"]["linked"] = True
                selected = [linked, *sorted(replies, key=_score_order)[:max(0, input_["comments"] - 1)]]
                notes.append(f"Focused on comment t1_{comment_id}.")
                notes.append(f"Returned {len(selected)} of {len(comments)} comment records from the fetched branch.")
                if not has_tree:
                    notes.append("Arctic Shift returned no reply tree for the comment, so its replies are not included.")
            else:
                selected = sorted(comments, key=_score_order)[:input_["comments"]]
                notes.append("Comments are sorted by descending archived score within the fetched tree, "
                             "then oldest first for ties (unknown scores last).")
            if orphans:
                noun = "reply" if orphans == 1 else "replies"
                notes.append(f"The fetched tree has {orphans} {noun} whose parent comment is not in it. "
                             "Their depth, and that of replies under them, is null.")
            if collapsed:
                notes.append(f"Arctic Shift collapsed parts of the tree in the bounded {TREE_LIMIT}-comment fetch.")
            if not comment_id:
                reported = f"{count} comments reported by the post snapshot" if count is not None else "an unknown snapshot count"
                noun = "comment" if len(selected) == 1 else "comments"
                notes.append(f"Returned {len(selected)} {noun}. The fetched tree supplied {len(comments)} "
                             f"comment records against {reported}.")
            items.extend(item for item, _ in selected)
            removed += sum(marker == "[removed]" for _, marker in selected)
            deleted += sum(marker == "[deleted]" for _, marker in selected)
        else:
            notes.append("Comments were not requested.")
    except http.HttpError as exc:
        failure = env.error(exc.code, str(exc))
        if isinstance(exc, _WrongPost):
            # The link names another post's comment, so the post read so far is not what was asked for.
            items, removed, deleted = [], 0, 0

    if removed or deleted:
        notes.append(f"Arctic Shift marked {deleted} returned items as deleted and {removed} as removed. "
                     "Their contents are shown as placeholders.")
    nulls = {"post": set(), "comment": set()}
    for item in items:
        kind, missing = unavailable[item["id"]]
        nulls[kind].update(missing)
    for kind, fields in nulls.items():
        if fields:
            notes.append(f"One or more {kind} records have null values for {', '.join(sorted(fields))}.")
    if len(items) > 1:
        notes.append("Comment titles are null because Reddit comments have no title.")
    outcome = failure["code"] if failure else "ok"
    meta = {"attempts": [env.attempt(BACKEND, outcome, client.requests_made, env.elapsed_ms(started))], "notes": notes}
    return env.envelope(CHANNEL, "read", input_, BACKEND, items, meta=meta, error=failure)
