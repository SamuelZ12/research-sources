# Output reference

Every command prints exactly one JSON object on stdout, on success and on failure. There is no other output format.

```jsonc
{
  "ok": true,
  "channel": "x",
  "action": "read",
  "input": {"posts": ["https://x.com/karpathy/status/1886192184808149383"]},
  "backend": "fxtwitter",
  "items": [
    {
      "id": "1886192184808149383",
      "url": "https://x.com/karpathy/status/1886192184808149383",
      "title": null,
      "author": "karpathy",
      "published_at": "2025-02-02T23:17:15Z",
      "text": "There's a new kind of coding I call \"vibe coding\", where…",
      "source": "fxtwitter",
      "engagement": {"likes": 38005, "reposts": 3837, "replies": 1501, "quotes": 2702, "views": 8305723},
      "raw": { /* the backend's own record */ }
    }
  ],
  "meta": {
    "attempts": [{"backend": "fxtwitter", "outcome": "ok", "requests": 1, "ms": 193}],
    "sources": {},             // `check` only: one outcome per source
    "rejected": [],            // items dropped as a block page or the wrong post
    "failed": [],              // inputs of a multi-input command that could not be read
    "truncated": null,         // "request_cap" when a request limit stopped the command early
    "notes": []
  },
  "error": null                // or {"code": "…", "message": "…"}
}
```

Every item has the same nine fields. Unavailable fields are `null`.

| Exit | Meaning | `error.code` |
|---|---|---|
| `0` | No error, at least one item | none |
| `66` | No error, zero items | none |
| `2` | Bad arguments or setup | `usage`, `config` |
| `1` | Runtime failure. Items read so far stay in `items` | `auth_failed`, `rate_limited`, `blocked`, `url_mismatch`, `not_found`, `upstream_error`, `network_error`, `timeout` |
