"""The rsrc JSON envelope every command prints: constructors and the exit-code rule."""

import time

ERROR_CODES = frozenset({"usage", "config", "auth_failed", "rate_limited", "blocked", "url_mismatch",
                         "not_found", "upstream_error", "network_error", "timeout"})
ITEM_KEYS = ("id", "url", "title", "author", "published_at", "text", "source", "engagement", "raw")
META_KEYS = ("attempts", "sources", "rejected", "failed", "truncated", "notes")


def item(**fields) -> dict:
    unknown = set(fields) - set(ITEM_KEYS)
    if unknown:
        raise ValueError(f"unknown item keys: {sorted(unknown)}")
    return {key: fields.get(key) for key in ITEM_KEYS}


def error(code, message) -> dict:
    if code not in ERROR_CODES:
        raise ValueError(f"unknown error code: {code!r}")
    return {"code": code, "message": message}


def attempt(backend, outcome, requests=None, ms=None) -> dict:
    return {"backend": backend, "outcome": outcome, "requests": requests, "ms": ms}


def elapsed_ms(started: float) -> int:
    """Whole milliseconds since `started` (a `time.monotonic()` reading): an attempt's `ms`."""
    return round((time.monotonic() - started) * 1000)


def envelope(channel, action, input, backend, items=(), *, meta=None, error=None) -> dict:
    merged = {"attempts": [], "sources": {}, "rejected": [], "failed": [], "truncated": None, "notes": []}
    if meta:
        unknown = set(meta) - set(META_KEYS)
        if unknown:
            raise ValueError(f"unknown meta keys: {sorted(unknown)}")
        merged.update(meta)
    return {
        "ok": error is None,
        "channel": channel,
        "action": action,
        "input": input,
        "backend": backend,
        "items": list(items),
        "meta": merged,
        "error": error,
    }


def usage(channel, action, message) -> dict:
    return envelope(channel, action, None, None, error=error("usage", message))


def exit_code(env) -> int:
    err = env["error"]
    if err is not None:
        return 2 if err["code"] in ("usage", "config") else 1
    return 0 if env["items"] else 66
