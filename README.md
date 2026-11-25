# research-sources (`rsrc`)

`rsrc` is a read-only command-line tool that lets an AI agent read eleven sources that web search covers poorly: X, Reddit, Hacker News, YouTube transcripts, GitHub, LinkedIn posts, Bluesky, Product Hunt, App Store reviews, Polymarket odds, and Ashby job boards. Every command prints one JSON object in the same shape, so the agent can tell real results from an empty search, a rate limit, or a block page. It needs no API keys.

```console
$ rsrc x read https://x.com/karpathy/status/1886192184808149383 | jq -c '.items[0] | [.author, .engagement.likes, .text[:56]]'
["karpathy",38005,"There's a new kind of coding I call \"vibe coding\", where"]

$ rsrc hn search "zqxvkpwj" > /dev/null; echo $?
66
```

Both outputs were recorded on 2026-11-25. The first command read an X post without a login. The second searched Hacker News for a word with no matches. An empty search exits `66`, not `0`.

`rsrc` is early software with one maintainer, tested on macOS only. The license is [MIT](LICENSE).

## Why use it

- **No API keys.** Nine sources and X post reads need no login. X search needs your own X session for now. Reddit search needs [Ego Lite](https://lite.ego.app/), a Chromium browser for agents.
- **Failures are not results.** `rsrc` rejects a login wall, a CAPTCHA page, or a different post than the one you asked for, and the output says why. On a rate limit, the command stops instead of retrying.
- **One output shape.** Eleven sources share one JSON envelope with 10 error codes and 4 exit codes.
- **No Python dependencies.** `rsrc` uses the standard library only. The tests run offline.
- **An agent skill ships with it.** [`SKILL.md`](skills/research-sources/SKILL.md) tells an agent which command answers which question and what to do on each error code.
- **Measured.** In a five-topic comparison, a research agent with an early version of `rsrc` cited at least four URLs per topic that the same agent with web search alone did not. See the [method and limits](docs/comparisons.md).

## How it compares

Use [last30days](https://github.com/mvanhorn/last30days-skill) when you want a finished brief. It searches many sites, scores the results and writes the report. Use [Agent-Reach](https://github.com/Panniantong/Agent-Reach) when you need its longer site list, which includes Chinese platforms such as Bilibili and Xiaohongshu.

`rsrc` does less. It gives an agent that already has web search each site's own results in one shape, and it reports a failure as a failure.

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/SamuelZ12/research-sources
rsrc check hn polymarket
```

The check sends one request to Hacker News and one to Polymarket. Read `meta.sources` in its output for each outcome.

Seven sources need nothing more. Four need extra setup for some commands:

| Source | Extra setup |
|---|---|
| YouTube | `yt-dlp` with its `yt-dlp-ejs` scripts. `youtube read` also needs `deno` 2.3.0 or newer |
| GitHub | A logged-in [GitHub CLI](https://cli.github.com/), only for private repositories and higher rate limits |
| Reddit | Ego Lite, for search and live threads. Without it, `reddit read` uses the public [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) archive |
| X | Your own X session, only for `x search`. FxTwitter, the public service `rsrc` searches X through, has had search down since 2026-11-19 |

On a Mac with Homebrew, `brew install yt-dlp deno gh` covers YouTube and GitHub. Homebrew's `yt-dlp` includes the scripts.

## Give it to your agent

`rsrc` is built for an agent to run. The [`research-sources` skill](skills/research-sources/SKILL.md) is the agent's manual. It maps questions to commands, explains each output field, and says when to stop. Install it with the [skills CLI](https://skills.sh/), which asks you which agents get the skill:

```sh
npx skills add SamuelZ12/research-sources
```

Without Node.js, copy the `skills/research-sources` folder into the folder your agent loads skills from. Then ask a question the skill covers, such as "Summarize this X post and its replies" or "What is Hacker News saying about Claude Code this month?"

## Commands

| Source | Commands |
|---|---|
| X | `x search`, `x read`, `x replies`, `x timeline` |
| Reddit | `reddit search`, `reddit read` |
| Hacker News | `hn search`, `hn read` |
| YouTube | `youtube search`, `youtube read` (details and transcript) |
| GitHub | `gh repo`, `gh search`, `gh issues` |
| LinkedIn | `linkedin read` (public post and comments) |
| Bluesky | `bluesky search`, `bluesky read`, `bluesky replies`, `bluesky timeline` |
| Product Hunt | `producthunt search`, `producthunt read`, `producthunt leaderboard` |
| App Store | `appstore search`, `appstore read` (listing and reviews) |
| Polymarket | `polymarket search` |
| Ashby | `ashby search`, `ashby read` |
| All | `check` |

Each command starts with `rsrc`, as in `rsrc gh repo OWNER/REPO`. Add `--help` to a command to list its flags. The [source reference](docs/sources.md) has the full syntax and what each source returns and omits.

## Output

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

## Signed-in sources

X and Reddit commands can act as your own account. Keep their volume low.

- **X search.** Use the X account signed in to Ego Lite, a cookie pair in `~/.config/rsrc/x.env`, or Google Chrome through `rsrc x connect`. The Chrome route is experimental. `x timeline` can use the same session. [X with your own session](docs/x-session.md) has the setup for each route.
- **Reddit.** `rsrc` loads Reddit pages in Ego Lite. When Ego Lite is signed in to Reddit, the commands act as that account.

## What it does not do

- **No writes.** `rsrc` never posts, likes, follows or trades.
- **No merging or ranking across sources.** Each list is that site's own results in that site's order.
- **No caching of results.** Every command asks the source again.
- **No web search.** `rsrc` covers what web search misses. Pair it with one.
- **No guarantees about the sources.** FxTwitter, X's web search, Reddit's pages, the Arctic Shift archive and YouTube through `yt-dlp` are not official APIs. They can change or block without notice. When one does, `rsrc` fails with an error code instead of an empty result. You are responsible for following each site's terms.

## Contributing

[`CONTRIBUTING.md`](CONTRIBUTING.md) has the setup, the rules a change must keep, and the manual Chrome tests.

## License

[MIT](LICENSE). The request shape for X's web search is ported from @steipete/bird v0.8.0 (MIT, Copyright (c) 2025 Peter Steinberger). Its notice is in [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
