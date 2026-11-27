"""The one output shape every command emits. Unknown values are None, never guessed."""

import math

FIELDS = ("id", "url", "author", "created_at", "text", "likes", "reposts", "replies", "quotes",
          "views", "lang", "reply_to_id", "conversation_id", "quoted", "media", "article", "poll", "text_truncated", "source")
MEDIA_TYPES = ("photo", "video", "gif")
LEGACY_TYPES = {"photo": "photo", "video": "video", "animated_gif": "gif"}


def make_record(**fields) -> dict:
    unknown = fields.keys() - set(FIELDS)
    if unknown:
        raise TypeError(f"unknown record fields: {sorted(unknown)}")
    record = dict.fromkeys(FIELDS)
    record.update(fields)
    if record["url"] is None and record["author"] and record["id"]:
        record["url"] = f"https://x.com/{record['author']}/status/{record['id']}"
    return record


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def media_entry(kind, url, duration) -> dict | None:
    """One media record, or None when `kind` is not a media type or `url` is not an http(s) address.
    `duration` (seconds) is kept only on a video or GIF, and only when it is a finite number."""
    if kind not in MEDIA_TYPES or not isinstance(url, str) or not url.startswith(("https://", "http://")):
        return None
    return {"type": kind, "url": url, "duration": finite(duration) if kind != "photo" else None}


def legacy_media(entries) -> list[dict]:
    """Media records from X's own media entities (x-graphql `extended_entities.media`, syndication
    `mediaDetails`): a photo's file, or a video's or GIF's highest-bitrate mp4 that has an address.
    An entry of another type, or without a usable address, is left out."""
    media = []
    for entry in entries if isinstance(entries, list) else []:
        kind = entry.get("type") if isinstance(entry, dict) else None
        kind = LEGACY_TYPES.get(kind) if isinstance(kind, str) else None
        if not kind:
            continue
        url, duration = entry.get("media_url_https"), None
        if kind != "photo":
            info = entry.get("video_info") if isinstance(entry.get("video_info"), dict) else {}
            variants = info.get("variants") if isinstance(info.get("variants"), list) else []
            mp4s = [v for v in variants if isinstance(v, dict) and v.get("content_type") == "video/mp4"
                    and media_entry(kind, v.get("url"), None)]
            url = max(mp4s, key=lambda v: finite(v.get("bitrate")) or 0).get("url") if mp4s else None
            millis = finite(info.get("duration_millis"))
            duration = millis / 1000 if millis is not None else None
        record = media_entry(kind, url, duration)
        if record:
            media.append(record)
    return media


def finite(value) -> int | float | None:
    """`value` if it is a finite number, else None (JSON such as `1e309` parses to infinity)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:  # an integer no float holds
        return None
