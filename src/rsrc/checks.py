"""Content checks for single-resource reads: block or login pages, and URL/ID match.

Dependency-free on purpose: items are plain dicts with the envelope's item keys.
Never apply these to search-result lists, where the marker phrases can be real content.
"""

from __future__ import annotations

# Lowercase phrases; matched against the lowercased title + first TEXT_WINDOW chars of text.
BLOCK_MARKERS: tuple[str, ...] = (
    # Reddit network-security block page
    "you've been blocked by network security",
    "you\u2019ve been blocked by network security",
    # Generic login walls
    "log in to continue",
    "sign in to continue",
    "please log in",
    # Cloudflare interstitials
    "just a moment...",
    "checking your browser",
    "attention required! | cloudflare",
    # Access denied pages
    "access denied",
    # CAPTCHA prompts
    "verify you are human",
    "are you a robot",
)

TEXT_WINDOW = 2000


def is_block_page(title, text) -> bool:
    """True if the title or the first 2,000 characters of text contain a block marker."""
    haystack = f"{title or ''}\n{(text or '')[:TEXT_WINDOW]}".lower()
    return any(marker in haystack for marker in BLOCK_MARKERS)


def screen_read(item, requested_id, *, case_insensitive=False):
    """Screen a single-resource read.

    Returns (item, None) if the item passes, else (None, {"url", "reason"}) with reason
    "blocked_page" (checked first) or "url_mismatch".
    """
    if is_block_page(item.get("title"), item.get("text")):
        return None, {"url": item.get("url"), "reason": "blocked_page"}
    returned = "" if item.get("id") is None else str(item["id"])
    requested = "" if requested_id is None else str(requested_id)
    if case_insensitive:
        returned, requested = returned.casefold(), requested.casefold()
    if returned != requested:
        return None, {"url": item.get("url"), "reason": "url_mismatch"}
    return item, None
