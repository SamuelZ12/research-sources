# research-sources (`rsrc`)

`rsrc` is a command-line tool that lets an AI agent read the places web search reaches poorly: X, Reddit, Hacker News, YouTube transcripts, Polymarket odds and GitHub. Every command prints one JSON object in the same shape, so the agent can tell real results from an empty search, a rate limit or a block page. It is read-only and needs no API keys.

**Status:** early, with one maintainer. Developed and tested on macOS only. FxTwitter's search has failed since 2026-11-19, and FxTwitter's API specification now lists it as "Search posts (unavailable)". Until it returns, `rsrc x search` fails with `upstream_error` unless you search [with your own X session](#x-with-your-own-session-optional). `rsrc x read`, `rsrc x replies` and `rsrc x timeline` still work without a login.

- **No API keys.** X, Hacker News, Polymarket and YouTube work without a login. GitHub uses your `gh` login. Reddit runs in [Ego Lite](https://lite.ego.app/), a browser app on your machine, and acts as your Reddit account if that browser is logged in to Reddit.
- **One output shape.** Six sources, one JSON envelope, 10 error codes, 4 exit codes. An empty search exits `66`, not `0`.
- **Block pages are not results.** A login wall, a CAPTCHA page or a different post than the one asked for is rejected, and the envelope says why.
- **Stops on rate limits.** A 429 ends the command. It is never retried, and no other backend is tried to get around it.
- **No Python dependencies.** Standard library only, about 3,600 lines, with over 900 tests that run offline.
- **Ships with an agent skill.** [`SKILL.md`](SKILL.md) tells an agent which command answers which question and what to do on each error code.
- **Measured.** In a five-topic comparison, a research agent with an early version of `rsrc` cited at least four URLs per topic that the same agent with web search alone did not. [Method and limits](docs/comparisons.md).

```console
$ rsrc hn search "Claude Code" --since 2026-10-15 --until 2026-11-15 | jq -c '.items[:3][] | [.engagement.points, .title]'
[1616,"Claude Code sends your email address to websites"]
[1014,"Backdoor in Claude Code for VS Code extension on the Cursor Marketplace"]
[905,"Goldman Sachs is pulling Claude Code from its bankers and traders"]

$ rsrc hn read not-a-url | jq -c .error
{"code":"usage","message":"expected a news.ycombinator.com item URL or a numeric ID, got 'not-a-url'"}
$ rsrc hn read not-a-url > /dev/null; echo $?
2
```

The first output is replayed from the answer recorded on 2026-11-15 ([`tests/fixtures/hn_search.json`](tests/fixtures/hn_search.json)).

[Install](#install) · [Give it to your agent](#give-it-to-your-agent) · [Commands](#commands) · [The envelope](#the-envelope) · [X with your own session](#x-with-your-own-session-optional) · [What it does not do](#what-it-does-not-do) · [Contributing](#contributing)

---

## Install

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/).

```sh
uv tool install git+https://github.com/SamuelZ12/research-sources
rsrc check hn polymarket
```

That `check` makes one request to each of the two channels and reports each under `meta.sources`: `ok`, or the error code that stopped it. A bare `rsrc check` sends nothing to FxTwitter or Reddit. `--live-x` reads one public post and probes FxTwitter search only if the search order includes it, and `--live-reddit` loads one Reddit search page in Ego Lite.

`x`, `hn` and `polymarket` work right away. The other channels run a program you install yourself:

| Channel | Needs |
|---|---|
| `x` | Nothing |
| `hn` | Nothing |
| `polymarket` | Nothing |
| `youtube` | `yt-dlp`, installed with its `yt-dlp-ejs` scripts (Homebrew's `yt-dlp` has them). `youtube read` and `check youtube` also need `deno` 2.3.0 or newer |
| `gh` | The [`gh`](https://cli.github.com/) CLI, logged in with `gh auth login` |
| `reddit` | [Ego Lite](https://lite.ego.app/) running, with its `ego-browser` command on your `PATH` |

On a Mac with Homebrew: `brew install yt-dlp deno gh`.

Optional: [`vidscribe`](https://github.com/SamuelZ12/vidscribe) (Apple Silicon) and `ffmpeg`. The skill uses them to read the videos and GIFs attached to X posts, and `youtube read` tries `vidscribe` on a short video that has no captions at all.

## Give it to your agent

`rsrc` is built to be run by an agent. [`SKILL.md`](SKILL.md) is the agent's manual: it maps questions to commands, explains each field of the output, and says when to stop. Put it where your agent loads skills. For Claude Code:

```sh
mkdir -p ~/.claude/skills/research-sources
curl -fsSL https://raw.githubusercontent.com/SamuelZ12/research-sources/main/SKILL.md \
  -o ~/.claude/skills/research-sources/SKILL.md
```

Then ask a question the skill covers, such as "what are people on X and Hacker News saying about Claude Code this month?"

## Commands

| Channel | Commands |
|---|---|
| X | `rsrc x search QUERY [--since YYYY-MM-DD]`, `rsrc x read URL_OR_ID…`, `rsrc x replies URL_OR_ID`, `rsrc x timeline HANDLE_OR_URL` (profile and newest posts) |
| Reddit | `rsrc reddit search QUERY`, `rsrc reddit read URL_OR_ID` (a share link costs one plain HTTP request to reddit.com outside the browser, without cookies) |
| Hacker News | `rsrc hn search QUERY`, `rsrc hn read URL_OR_ID` |
| YouTube | `rsrc youtube search QUERY`, `rsrc youtube read URL_OR_ID` (details and transcript) |
| Polymarket | `rsrc polymarket search QUERY` |
| GitHub | `rsrc gh repo OWNER/REPO`, `rsrc gh search QUERY`, `rsrc gh issues QUERY` |
| All | `rsrc check [CHANNEL…] [--live-x] [--live-reddit]` |

`rsrc <channel> <action> --help` lists every flag. [`SKILL.md`](SKILL.md) has the details that matter when reading results: what each channel returns, how it orders results, and what it leaves out.

## The envelope

Every command prints exactly one JSON object on stdout, on success and on failure. There is no other output format.

```jsonc
{
  "ok": true,
  "channel": "hn",
  "action": "search",
  "input": {"query": "Claude Code", "since": "2026-10-15", "until": "2026-11-15", "sort": "relevance", "limit": 30},
  "backend": "algolia",
  "items": [
    {
      "id": "50385272",
      "url": "https://news.ycombinator.com/item?id=50385272",
      "title": "Claude Code sends your email address to websites",
      "author": "motoroco",
      "published_at": "2026-11-05T22:17:15Z",
      "text": null,
      "source": "hackernews",
      "engagement": {"points": 1616, "comments": 535},
      "raw": { /* the backend's own record */ }
    }
    // 29 more
  ],
  "meta": {
    "attempts": [ /* one per backend call: backend, outcome, requests, ms */ ],
    "sources": {},             // `check` only: one outcome per channel
    "rejected": [],            // items dropped as a block page or the wrong post
    "failed": [],              // inputs of a multi-input command that could not be read
    "truncated": null,         // "request_cap" when a request limit stopped the command early
    "notes": []
  },
  "error": null                // or {"code": "…", "message": "…"}
}
```

Every item has the same nine fields. A field the source lacks is `null`, never guessed.

| Exit | Meaning | `error.code` |
|---|---|---|
| `0` | At least one item | none |
| `66` | Ran fine, zero items | none |
| `2` | Bad arguments or setup | `usage`, `config` |
| `1` | Runtime failure; items read so far stay in `items` | `auth_failed`, `rate_limited`, `blocked`, `url_mismatch`, `not_found`, `upstream_error`, `network_error`, `timeout` |

## X with your own session (optional)

`rsrc x` uses [FxTwitter](https://github.com/FxEmbed/FxEmbed)'s public API by default and needs no login. The `x-graphql` backend runs a search on X's own web search with your session cookies. It is never used unless you name it in `--backend` or in `RSRC_X_SEARCH`.

Set `RSRC_X_SEARCH` in the environment or in `~/.config/rsrc/x.env` to choose the backends `rsrc x search` tries, in order. The environment takes precedence.

| Search order | When to use it |
|---|---|
| `fxtwitter` (default) | You want to search without logging in. FxTwitter's search is currently unavailable, so today this order fails with `upstream_error`. |
| `x-graphql,fxtwitter` | You want every search to start from your logged-in X account, with FxTwitter second. |
| `fxtwitter,x-graphql` | You prefer public search and want to use your account only if FxTwitter has an upstream or network error. |
| `x-graphql` | You want to search only from your account. |

Search tries the next backend only after `upstream_error` or `network_error`, within the same `--max-requests` budget. An empty result, a rate limit, or a cookie error ends the search. `meta.attempts` lists the backends that made requests in order. The result comes from the last backend that ran, unless it also failed with one of those two codes: then the first backend's error and posts are kept. `meta.notes` has the error of each other backend that ran. Pass `--backend fxtwitter` or `--backend x-graphql` to use only that backend for one search.

`RSRC_X_FALLBACK=x-graphql`, the earlier name for the third row, now stops the command with `config`: replace that line with `RSRC_X_SEARCH=fxtwitter,x-graphql`.

If the cookie file does not exist, create it with mode 600:

```sh
mkdir -p ~/.config/rsrc
install -m 600 /dev/null ~/.config/rsrc/x.env
```

Open the file in an editor. Add the `auth_token` and `ct0` cookies from a logged-in x.com browser session as two lines, `AUTH_TOKEN=…` and `CT0=…`. Typing the values into a shell command would leave them in your shell history.

- The `AUTH_TOKEN` and `CT0` environment variables win over the file. `RSRC_X_ENV` names another file.
- The file must be mode 600 or stricter, or the command stops with `config`.
- Cookie values never appear in output, error messages or files `rsrc` writes.
- X rotates the ID of its search query. `rsrc` caches the one that works in `~/.cache/rsrc/x-query-ids.json` (or under `$XDG_CACHE_HOME`). When every ID it knows is rejected, the error message says how to copy the current one from your browser.

The x-graphql backend acts as your logged-in account, and so does `reddit` when your Ego Lite profile is logged in to Reddit. Keep volume low: the risk to those accounts is yours.

## What it does not do

- **No writes.** It never posts, likes, follows or trades.
- **No merging or ranking across sources.** Each list is that site's own results in that site's order.
- **No caching of results.** Every command asks the source again.
- **No web search.** It covers what web search misses; pair it with one.
- **No guarantees about the sources.** FxTwitter, X's web search, Reddit's pages and YouTube through `yt-dlp` are not official APIs and can change or block without notice. `rsrc` is built to fail with an error code when one does, rather than look empty. You are responsible for following each site's terms.

## Contributing

```sh
git clone https://github.com/SamuelZ12/research-sources
cd research-sources
uv sync
uv run pytest -q
```

A change is welcome when it keeps these true:

- **Tests stay offline.** A guard in `tests/conftest.py` fails any test that opens a real network connection. New behaviour comes with a test built on a recorded answer in `tests/fixtures/`.
- **Runtime dependencies stay empty.** Programs such as `gh` and `yt-dlp` are run as subprocesses, never imported.
- **A rate limit stops the command.** A 429 is never retried and never falls back to another backend.
- **No secrets anywhere.** No cookie, token or account ID in source, fixtures, test output or commit messages. Run `gitleaks dir . --redact --no-banner` before you commit.
- **A failure never becomes an `ok` result.** No `except` that swallows an error, and a page `rsrc` does not recognize fails instead of reading as empty.
- **Fixtures hold public data only.** Before saving a recording, remove anything that identifies you: `yt-dlp`'s stream and caption addresses carry your IP address.
- **No skipped tests and no suppressions** (`# noqa`, `# type: ignore`, `pytest.mark.skip`).

## License

[MIT](LICENSE). The request shape for X's web search is ported from @steipete/bird v0.8.0 (MIT, Copyright (c) 2025 Peter Steinberger); its notice is in [`THIRD_PARTY_NOTICES`](THIRD_PARTY_NOTICES).
